from __future__ import annotations

import os
import socket
import threading
from threading import BoundedSemaphore
import time
import uuid
from dataclasses import dataclass
from hashlib import sha256
from typing import List, Optional, Tuple

from .arxiv import ArxivClient
from .article_pipeline import ArticlePipeline
from .config import Settings
from .db import Store
from .feishu import FeishuClient, progress_emoji_type
from .models import FeishuEvent, PaperRef
from .openai_client import OpenAIClient
from .pipeline import MaxReadPipeline
from .sources import WebRef
from .web_article import WebArticleClient
from .visual_qa import VisualQAController


@dataclass
class QueueItem:
    kind: str
    source_id: str
    source_url: str
    label: str


class QueueManager:
    def __init__(self, settings: Settings, no_openai: bool = False):
        self.settings = settings
        self.no_openai = no_openai
        self.stop_event = threading.Event()
        self.threads: List[threading.Thread] = []
        self.manager_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.llm_sem = BoundedSemaphore(max(1, settings.batch_llm_concurrency))
        self.feishu_sem = BoundedSemaphore(max(1, settings.batch_feishu_concurrency))
        self._last_recover_at = 0.0

    def start_background_workers(self) -> None:
        if self.threads:
            return
        store = Store(self.settings.db_path)
        try:
            recovered_dead = store.recover_dead_worker_queue_jobs(socket.gethostname(), _pid_is_alive)
            recovered_stale = store.recover_stale_queue_jobs(self.settings.queue_stale_minutes)
            if recovered_dead:
                store.add_job_event(0, "recover_dead_worker", str(recovered_dead))
            if recovered_stale:
                store.add_job_event(0, "recover_stale", str(recovered_stale))
        finally:
            store.close()
        for index in range(max(1, self.settings.queue_workers)):
            thread = threading.Thread(target=self.worker_loop, name=f"maxread-worker-{index + 1}", daemon=True)
            thread.start()
            self.threads.append(thread)

    def worker_loop(self) -> None:
        while not self.stop_event.is_set():
            store = Store(self.settings.db_path)
            try:
                self._recover_stale_if_due(store)
                worker_id = f"{self.manager_id}:{threading.current_thread().name}"
                job = store.claim_next_queue_job(worker_id=worker_id)
            except Exception:
                store.close()
                time.sleep(2)
                continue
            if not job:
                store.close()
                time.sleep(2)
                continue
            try:
                self._process_job(store, job, worker_id)
            finally:
                store.close()

    def _recover_stale_if_due(self, store: Store) -> None:
        now = time.time()
        if now - self._last_recover_at < 60:
            return
        self._last_recover_at = now
        recovered_dead = store.recover_dead_worker_queue_jobs(socket.gethostname(), _pid_is_alive)
        recovered_stale = store.recover_stale_queue_jobs(self.settings.queue_stale_minutes)
        if recovered_dead:
            store.add_job_event(0, "recover_dead_worker", str(recovered_dead))
        if recovered_stale:
            store.add_job_event(0, "recover_stale", str(recovered_stale))

    def _process_job(self, store: Store, job, worker_id: str) -> None:
        source_kind = job["source_kind"]
        source_id = job["source_id"]
        source_url = job["source_url"]
        job_id = int(job["id"])
        raw_feishu = FeishuClient(self.settings.lark_cli, self.settings.feishu_as)

        def progress(text: str, event_type: str, prefix: str) -> None:
            if not store.update_queue_job_stage(job_id, event_type, worker_id=worker_id):
                return
            _notify_watchers_progress(store, raw_feishu, job_id, text, event_type, prefix)

        feishu = _LimitedFeishu(
            raw_feishu,
            self.feishu_sem,
            lambda: progress(f"[敲键盘] 在写飞书文档：{source_id}", "writing", "job-writing"),
        )
        llm = None if self.no_openai or not self.settings.openai_api_key else _LimitedLLM(
            OpenAIClient(
                self.settings.openai_api_key,
                self.settings.model,
                timeout=self.settings.openai_timeout,
                base_url=self.settings.openai_base_url,
                sub_module=self.settings.openai_sub_module,
                reasoning_effort=self.settings.openai_reasoning_effort,
                api_mode=self.settings.openai_api_mode,
            ),
            self.llm_sem,
            lambda: progress(f"[在做了] 正在读{'论文' if source_kind == 'paper' else '文章'}：{source_id}", "reading", "job-reading"),
            lambda: progress(f"[审阅中] 正在审阅/修订：{source_id}", "reviewing", "job-reviewing"),
        )
        title = ""
        store.add_job_event(job_id, "start", source_id)
        store.update_queue_job_stage(job_id, "downloading", worker_id=worker_id)
        heartbeat_stop, heartbeat_thread = self._start_job_heartbeat(job_id, worker_id)
        _notify_watchers_started(store, raw_feishu, job_id, source_id)
        try:
            if source_kind == "paper":
                pipeline = MaxReadPipeline(
                    store,
                    ArxivClient(
                        self.settings.workdir,
                        timeout=self.settings.arxiv_timeout,
                        parallel_streams=self.settings.arxiv_parallel_streams,
                        parallel_min_bytes=self.settings.arxiv_parallel_min_bytes,
                    ),
                    feishu,
                    llm,
                    require_source=self.settings.require_source,
                    review_reasoning_effort=self.settings.openai_review_reasoning_effort,
                    visual_qa=VisualQAController.from_settings(self.settings, llm=llm),
                    generation_repair_rounds=self.settings.generation_repair_rounds,
                    quality_repair_rounds=self.settings.quality_repair_rounds,
                    on_workflow_event=lambda event, detail="": store.transition_queue_job(
                        job_id, event, detail, expected_worker_id=worker_id
                    ),
                )
                result = pipeline.process_ref(
                    PaperRef(source_id, source_url),
                    event=None,
                    send_progress=False,
                    # A completed paper is the idempotency boundary. Retrying
                    # a queue finalization must not create another document.
                    force=False,
                    resume_published_url=str(job.get("doc_url") or ""),
                    resume_published_checkpoint=str(job.get("checkpoint_json") or ""),
                )
                record = store.get_paper(source_id)
                title = record.title if record else ""
            else:
                pipeline = ArticlePipeline(
                    store,
                    WebArticleClient(self.settings.workdir, timeout=self.settings.arxiv_timeout),
                    feishu,
                    llm,
                    review_reasoning_effort=self.settings.openai_review_reasoning_effort,
                    visual_qa=VisualQAController.from_settings(self.settings, llm=llm),
                    quality_repair_rounds=self.settings.quality_repair_rounds,
                    on_workflow_event=lambda event, detail="": store.transition_queue_job(
                        job_id, event, detail, expected_worker_id=worker_id
                    ),
                )
                result = pipeline.process_ref(
                    WebRef(source_url),
                    event=None,
                    send_progress=False,
                    resume_published_url=str(job.get("doc_url") or ""),
                    resume_published_checkpoint=str(job.get("checkpoint_json") or ""),
                )
                record = store.get_document(result.article_id)
                title = record.title if record else ""
            if result.error:
                if store.fail_queue_job(int(job["id"]), result.error, worker_id=worker_id):
                    _notify_watchers(store, feishu, int(job["id"]), source_id, "", title, result.error)
            elif result.doc_url:
                if store.complete_queue_job(int(job["id"]), result.doc_url, title=title, worker_id=worker_id):
                    _notify_watchers(store, feishu, int(job["id"]), source_id, result.doc_url, title, "")
            else:
                error = result.error or "unknown processing error"
                if store.fail_queue_job(int(job["id"]), error, worker_id=worker_id):
                    _notify_watchers(store, feishu, int(job["id"]), source_id, "", title, error)
        except Exception as exc:
            error = str(exc)
            if store.fail_queue_job(int(job["id"]), error, worker_id=worker_id):
                _notify_watchers(store, feishu, int(job["id"]), source_id, "", title, error)
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=2)

    def _start_job_heartbeat(self, job_id: int, worker_id: str):
        stop = threading.Event()
        interval = max(5, int(getattr(self.settings, "queue_heartbeat_seconds", 15)))

        def run() -> None:
            while not stop.wait(interval):
                # The worker already initialized the schema. Heartbeats must
                # stay lightweight and must not contend on repeated DDL.
                hb_store = Store(self.settings.db_path, initialize=False)
                try:
                    if not hb_store.heartbeat_queue_job(job_id, worker_id):
                        stop.set()
                        break
                except Exception:
                    pass
                finally:
                    hb_store.close()

        thread = threading.Thread(target=run, name=f"maxread-heartbeat-{job_id}", daemon=True)
        thread.start()
        return stop, thread


class _LimitedLLM:
    def __init__(self, inner: OpenAIClient, sem: BoundedSemaphore, on_call=None, on_review=None):
        self.inner = inner
        self.sem = sem
        self.on_call = on_call
        self.on_review = on_review
        self._announced = False
        self._announced_review = False

    def responses_text(self, system: str, user: str, **kwargs) -> str:
        if self.on_review and _is_review_prompt(system) and not self._announced_review:
            self._announced_review = True
            self.on_review()
        elif self.on_call and not self._announced:
            self._announced = True
            self.on_call()
        with self.sem:
            return self.inner.responses_text(system, user, **kwargs)

    def responses_image_text(self, system: str, user: str, image_path) -> str:
        if self.on_call and not self._announced:
            self._announced = True
            self.on_call()
        with self.sem:
            return self.inner.responses_image_text(system, user, image_path)


def _is_review_prompt(system: str) -> bool:
    return "发布前质量检查员" in str(system) or "待检查 Markdown" in str(system)


class _LimitedFeishu:
    def __init__(self, inner: FeishuClient, sem: BoundedSemaphore, on_write=None):
        self.inner = inner
        self.sem = sem
        self.on_write = on_write
        self._announced = False

    def __getattr__(self, name: str):
        attr = getattr(self.inner, name)
        if name in {"create_docx", "overwrite_docx", "overwrite_docx_xml", "insert_image", "remove_text", "publish_docx", "fetch_docx", "block_replace"}:
            def wrapped(*args, **kwargs):
                if self.on_write and not self._announced:
                    self._announced = True
                    self.on_write()
                with self.sem:
                    return attr(*args, **kwargs)
            return wrapped
        return attr

def enqueue_event_items(
    settings: Settings,
    store: Store,
    feishu: FeishuClient,
    event: FeishuEvent,
    paper_refs: List[PaperRef],
    web_refs: List[WebRef],
    *,
    retry_requested: bool = False,
) -> None:
    items = [QueueItem("paper", ref.paper_id, ref.url, ref.paper_id) for ref in paper_refs]
    items.extend(QueueItem("article", ref.url, ref.url, ref.url) for ref in web_refs)
    if not items:
        return
    _react(feishu, event.message_id, "start")
    action = "已重新加入全局队列" if retry_requested else "已加入全局队列"
    lines = [f"收到 {len(items)} 篇，{action}。"]
    for item in items:
        cached = _cached_doc(store, item)
        if cached:
            usage_id = store.add_usage_event(event.event_id, event.message_id, event.chat_id, event.chat_type, event.sender_id, item.kind, item.source_id, item.source_url, status="done")
            store.update_usage_event(usage_id, "done", doc_url=cached[0], title=cached[1])
            lines.append(f"- {item.label}：已有缓存 {cached[0]}")
            continue
        usage_id = store.add_usage_event(event.event_id, event.message_id, event.chat_id, event.chat_type, event.sender_id, item.kind, item.source_id, item.source_url, status="queued")
        queued = store.enqueue_job(item.kind, item.source_id, item.source_url, event.event_id, event.message_id, event.chat_id, event.chat_type, event.sender_id, usage_id)
        pos = store.queue_position(int(queued["job_id"]))
        if queued["created"]:
            duration = store.recent_job_duration_seconds(item.kind)
            lines.append(f"- {item.label}：{_queue_eta_text(pos, settings.queue_workers, duration)}")
        else:
            store.update_usage_event(usage_id, "watching")
            lines.append(f"- {item.label}：已经在队列/处理中，完成后会通知你。")
    _reply(feishu, event.message_id, "\n".join(lines), f"queue:{event.event_id}")


def _queue_eta_text(position: int, workers: int, recent_duration_seconds: int = 300) -> str:
    pos = max(1, int(position or 1))
    worker_count = max(1, int(workers or 1))
    batch_no = (pos - 1) // worker_count + 1
    duration = max(60, int(recent_duration_seconds or 300))
    wait = max(0, batch_no - 1) * duration
    complete = batch_no * duration
    if batch_no == 1:
        return (
            f"队列第 {pos} 位，并发槽位内；预计等待约 0 分钟，"
            f"预计生成约 {_duration_text(duration)}，预计完成约 {_duration_text(complete)}。"
        )
    return (
        f"队列第 {pos} 位，约第 {batch_no} 批开始；"
        f"预计等待约 {_duration_text(wait)}，预计生成约 {_duration_text(duration)}，"
        f"预计完成约 {_duration_text(complete)}。"
    )


def _duration_text(seconds: int) -> str:
    minutes = max(1, round(max(0, int(seconds)) / 60))
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, remaining = divmod(minutes, 60)
    return f"{hours} 小时" if not remaining else f"{hours} 小时 {remaining} 分钟"



def run_worker_forever(settings: Settings, no_openai: bool = False) -> None:
    manager = QueueManager(settings, no_openai=no_openai)
    manager.start_background_workers()
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        manager.stop_event.set()


def _cached_doc(store: Store, item: QueueItem) -> Optional[Tuple[str, str]]:
    if item.kind == "paper":
        record = store.get_paper(item.source_id)
        if record and record.status == "done" and record.doc_url:
            return record.doc_url, record.title
    else:
        import hashlib

        doc_id = hashlib.sha256(item.source_url.encode("utf-8")).hexdigest()[:16]
        record = store.get_document(doc_id)
        if record and record.status == "done" and record.doc_url:
            return record.doc_url, record.title
    return None





def _notify_watchers_progress(store: Store, feishu: FeishuClient, job_id: int, text: str, event_type: str, prefix: str) -> None:
    watchers = store.get_job_watchers(job_id)
    store.add_job_event(job_id, event_type, text)
    for watcher in watchers:
        try:
            _react(feishu, watcher["message_id"], event_type)
            store.add_job_event(job_id, f"react_{event_type}", str(watcher.get("usage_event_id", "")))
        except Exception as exc:
            store.add_job_event(job_id, f"react_{event_type}_failed", str(exc)[:500])


def _notify_watchers_started(store: Store, feishu: FeishuClient, job_id: int, source_id: str) -> None:
    watchers = store.get_job_watchers(job_id)
    for watcher in watchers:
        usage_id = int(watcher.get("usage_event_id") or 0)
        if usage_id:
            store.update_usage_event(usage_id, "running")
        try:
            _react(feishu, watcher["message_id"], "downloading")
            store.add_job_event(job_id, "react_running", str(watcher.get("usage_event_id", "")))
        except Exception as exc:
            store.add_job_event(job_id, "react_running_failed", str(exc)[:500])

def _notify_watchers(store: Store, feishu: FeishuClient, job_id: int, source_id: str, doc_url: str, title: str, error: str) -> None:
    watchers = store.get_job_watchers(job_id)
    for watcher in watchers:
        usage_id = int(watcher.get("usage_event_id") or 0)
        if doc_url:
            if usage_id:
                store.update_usage_event(usage_id, "done", doc_url=doc_url, title=title)
            text = f"哥，读完了：{doc_url}"
            prefix = f"job-done:{job_id}:{watcher['id']}"
        else:
            if usage_id:
                store.update_usage_event(usage_id, "failed", title=title, error=error)
            reason = str(error).replace("\n", " ")[:500]
            text = (
                f"这篇我没读成：{source_id}\n原因：{reason}\n"
                "需要再试时，直接在本话题回复「重试」；也可以回复「重试 + 论文 ID」。"
            )
            prefix = f"job-fail:{job_id}:{watcher['id']}"
        try:
            _reply(feishu, watcher["message_id"], text, prefix)
            store.mark_watcher_notified(int(watcher["id"]))
            store.add_job_event(job_id, "notify_done" if doc_url else "notify_failed", str(watcher.get("usage_event_id", "")))
        except Exception as exc:
            store.add_job_event(job_id, "notify_error", str(exc)[:500])


def _reply(feishu: FeishuClient, message_id: str, text: str, prefix: str) -> None:
    key = sha256(prefix.encode("utf-8")).hexdigest()[:32]
    feishu.reply_text(message_id, text[:900], idempotency_key=key)


def _react(feishu: FeishuClient, message_id: str, stage: str) -> None:
    if not progress_emoji_type(stage):
        return
    try:
        feishu.set_progress_reaction(message_id, stage)
    except Exception:
        return


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
