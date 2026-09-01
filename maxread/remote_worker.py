from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from threading import BoundedSemaphore
from types import SimpleNamespace

from .arxiv import ArxivClient
from .cache_cleanup import cleanup_source_cache
from .db import Store
from .feishu import FeishuClient
from .job_queue import (
    _LimitedFeishu,
    _LimitedLLM,
    _notify_watchers,
    _notify_watchers_started,
    auto_retry_queue_job,
)
from .models import PaperRef
from .openai_client import OpenAIClient
from .pipeline import MaxReadPipeline
from .visual_qa import VisualQAController
from .workflow import InvalidWorkflowTransition, WorkflowEvent


def coordinator_claim(settings, store: Store, payload: dict) -> dict:
    worker_id = _worker_id(payload)
    job = store.claim_next_queue_job(worker_id=worker_id, source_kinds=("paper",))
    if job is None:
        return {"ok": True, "job": None}
    store.add_job_event(int(job["id"]), "remote_worker_claim", worker_id)
    try:
        _notify_watchers_started(
            store,
            FeishuClient(settings.lark_cli, settings.feishu_as),
            int(job["id"]),
            str(job["source_id"]),
            suppress_progress_notifications=bool(job.get("suppress_progress_notifications")),
        )
    except Exception as exc:
        store.add_job_event(int(job["id"]), "remote_start_notify_failed", str(exc)[:500])
    paper = store.get_paper(str(job["source_id"]))
    return {"ok": True, "job": job, "paper": paper.__dict__ if paper else None}


def coordinator_heartbeat(_settings, store: Store, payload: dict) -> dict:
    worker_id = _worker_id(payload)
    ok = store.heartbeat_queue_job(int(payload.get("job_id") or 0), worker_id)
    return {"ok": bool(ok), "lease": "active" if ok else "lost"}


def coordinator_transition(_settings, store: Store, payload: dict) -> dict:
    worker_id = _worker_id(payload)
    job_id = int(payload.get("job_id") or 0)
    event = WorkflowEvent(str(payload.get("event") or ""))
    detail = str(payload.get("detail") or "")[:4000]
    if event is WorkflowEvent.COMPLETE:
        current = store.get_queue_job(job_id)
        if not current or str(current.get("worker_id") or "") != worker_id:
            return {"ok": False, "lease": "lost"}
        store.add_job_event(job_id, "remote_pipeline_complete", detail)
        return {"ok": True, "deferred_to_finish": True}
    try:
        ok = store.transition_queue_job(job_id, event, detail, expected_worker_id=worker_id)
    except (InvalidWorkflowTransition, ValueError):
        current = store.get_queue_job(job_id)
        if current and str(current.get("worker_id") or "") == worker_id and str(current.get("last_event") or "") == event.value:
            ok = True
        else:
            raise
    return {"ok": bool(ok)}


def coordinator_event(_settings, store: Store, payload: dict) -> dict:
    worker_id = _worker_id(payload)
    job_id = int(payload.get("job_id") or 0)
    job = store.get_queue_job(job_id)
    if not job or str(job.get("worker_id") or "") != worker_id or str(job.get("status") or "") != "running":
        return {"ok": False, "lease": "lost"}
    event_type = "remote_" + str(payload.get("event_type") or "event").strip()[:80]
    store.add_job_event(job_id, event_type, str(payload.get("detail") or "")[:4000])
    return {"ok": True}


def coordinator_finish(settings, store: Store, payload: dict) -> dict:
    worker_id = _worker_id(payload)
    job_id = int(payload.get("job_id") or 0)
    job = store.get_queue_job(job_id)
    if job is None:
        return {"ok": False, "error": "unknown job"}
    if str(job.get("status") or "") == "done":
        return {"ok": True, "status": "done", "doc_url": str(job.get("doc_url") or "")}
    if str(job.get("worker_id") or "") != worker_id:
        return {"ok": False, "error": "worker lease lost"}

    source_id = str(job.get("source_id") or "")
    paper = payload.get("paper") if isinstance(payload.get("paper"), dict) else {}
    paper_status = str(paper.get("status") or ("failed" if payload.get("error") else "done"))
    paper_fields = {
        key: paper.get(key)
        for key in ("title", "project_summary", "doc_url", "doc_token", "error")
        if paper.get(key) is not None
    }
    store.upsert_paper(source_id, paper_status, **paper_fields)
    for issue in payload.get("review_issues") or []:
        if isinstance(issue, dict):
            store.add_review_issue(
                "paper",
                source_id,
                str(issue.get("category") or "other"),
                str(issue.get("severity") or "low"),
                str(issue.get("detail") or ""),
            )

    error = str(payload.get("error") or "")
    title = str(paper.get("title") or job.get("title") or "")
    doc_url = str(payload.get("doc_url") or paper.get("doc_url") or "")
    feishu = FeishuClient(settings.lark_cli, settings.feishu_as)
    suppress = bool(job.get("suppress_progress_notifications"))
    if error:
        if auto_retry_queue_job(settings, store, job, error, worker_id):
            return {"ok": True, "status": "requeued"}
        if store.fail_queue_job(job_id, error, worker_id=worker_id, doc_url=doc_url):
            _notify_watchers(
                store,
                feishu,
                job_id,
                source_id,
                "",
                title,
                error,
                published_doc_url=doc_url,
                notify_failure=not suppress,
            )
        return {"ok": True, "status": "failed", "doc_url": doc_url}

    if not doc_url:
        error = "remote paper worker returned no document URL"
        store.fail_queue_job(job_id, error, worker_id=worker_id)
        _notify_watchers(store, feishu, job_id, source_id, "", title, error, notify_failure=not suppress)
        return {"ok": True, "status": "failed"}
    if store.complete_queue_job(job_id, doc_url, title=title, worker_id=worker_id):
        _notify_watchers(
            store,
            feishu,
            job_id,
            source_id,
            doc_url,
            title,
            "",
            recovery_reason=str(job.get("recovery_reason") or ""),
            notify_success=not suppress,
        )
        cleanup = payload.get("cleanup") if isinstance(payload.get("cleanup"), dict) else {}
        if cleanup:
            store.add_job_event(
                job_id,
                "remote_cache_cleanup",
                json.dumps(cleanup, ensure_ascii=False, sort_keys=True),
            )
    return {"ok": True, "status": "done", "doc_url": doc_url}


class CoordinatorClient:
    def __init__(self, base_url: str, token: str, timeout: int = 30):
        if not base_url or not token:
            raise RuntimeError("MAXREAD_WORKER_COORDINATOR_URL and MAXREAD_WORKER_TOKEN are required")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = max(5, int(timeout))

    def claim(self, worker_id: str) -> dict:
        return self._post("/api/worker/claim", {"worker_id": worker_id})

    def heartbeat(self, job_id: int, worker_id: str) -> bool:
        return bool(self._post("/api/worker/heartbeat", {"job_id": job_id, "worker_id": worker_id}).get("ok"))

    def transition(self, job_id: int, worker_id: str, event: WorkflowEvent, detail: str = "") -> bool:
        return bool(self._post("/api/worker/transition", {
            "job_id": job_id,
            "worker_id": worker_id,
            "event": event.value,
            "detail": detail,
        }).get("ok"))

    def event(self, job_id: int, worker_id: str, event_type: str, detail) -> bool:
        value = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False, sort_keys=True)
        return bool(self._post("/api/worker/event", {
            "job_id": job_id,
            "worker_id": worker_id,
            "event_type": event_type,
            "detail": value,
        }).get("ok"))

    def finish(self, payload: dict) -> dict:
        return self._post("/api/worker/finish", payload)

    def _post(self, path: str, payload: dict) -> dict:
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        last_error = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    result = json.loads(response.read().decode("utf-8"))
                if not isinstance(result, dict):
                    raise RuntimeError("coordinator returned invalid JSON")
                return result
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")[:500]
                last_error = RuntimeError(f"coordinator HTTP {exc.code}: {body}")
                if exc.code < 500:
                    raise last_error from exc
            except Exception as exc:
                last_error = exc
            time.sleep(min(2 ** attempt, 8))
        raise RuntimeError(f"coordinator unavailable: {last_error}")


class RemotePaperWorker:
    def __init__(self, settings):
        self.settings = settings
        name = settings.worker_name or socket.gethostname()
        self.worker_id_prefix = f"remote:{name}:{os.getpid()}"
        self.client = CoordinatorClient(settings.worker_coordinator_url, settings.worker_token)
        self.stop = threading.Event()
        self.llm_sem = BoundedSemaphore(max(1, int(settings.batch_llm_concurrency)))
        self.feishu_sem = BoundedSemaphore(max(1, int(settings.batch_feishu_concurrency)))

    def run_forever(self) -> None:
        workers = []
        for slot in range(max(1, int(self.settings.queue_workers))):
            thread = threading.Thread(
                target=self._worker_loop,
                args=(slot + 1,),
                name=f"remote-paper-worker-{slot + 1}",
                daemon=True,
            )
            thread.start()
            workers.append(thread)
        while not self.stop.wait(1):
            if any(thread.is_alive() for thread in workers):
                continue
            return

    def _worker_loop(self, slot: int) -> None:
        worker_id = f"{self.worker_id_prefix}:{slot}"
        store_path = Path(self.settings.workdir) / "remote-worker-db" / f"slot-{slot}.sqlite3"
        local_store = Store(store_path)
        try:
            self._claim_loop(worker_id, local_store)
        finally:
            local_store.close()

    def _claim_loop(self, worker_id: str, local_store: Store) -> None:
        while not self.stop.is_set():
            try:
                payload = self.client.claim(worker_id)
            except Exception:
                self.stop.wait(self.settings.worker_poll_seconds)
                continue
            job = payload.get("job")
            if not isinstance(job, dict):
                self.stop.wait(self.settings.worker_poll_seconds)
                continue
            self._process(job, payload.get("paper"), worker_id, local_store)

    def _process(self, job: dict, paper: dict | None, worker_id: str, local_store: Store) -> None:
        job_id = int(job["id"])
        source_id = str(job["source_id"])
        if paper:
            fields = {key: paper.get(key) for key in (
                "title", "project_summary", "doc_url", "doc_token", "error"
            ) if paper.get(key) is not None}
            local_store.upsert_paper(source_id, str(paper.get("status") or "queued"), **fields)
        issue_row = local_store.conn.execute(
            "select coalesce(max(id), 0) as id from review_issues where source_kind='paper' and source_id=?",
            (source_id,),
        ).fetchone()
        review_start_id = int(issue_row["id"] if issue_row else 0)

        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(job_id, worker_id, heartbeat_stop),
            name=f"remote-heartbeat-{job_id}",
            daemon=True,
        )
        heartbeat.start()
        result = None
        try:
            llm = _LimitedLLM(
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
                review_timeout=self.settings.openai_review_timeout,
                on_timing=lambda event_type, detail: self.client.event(
                    job_id, worker_id, event_type, detail
                ),
            )
            pipeline = MaxReadPipeline(
                local_store,
                ArxivClient(
                    self.settings.workdir,
                    timeout=self.settings.arxiv_timeout,
                    parallel_streams=self.settings.arxiv_parallel_streams,
                    parallel_min_bytes=self.settings.arxiv_parallel_min_bytes,
                ),
                _LimitedFeishu(
                    FeishuClient(self.settings.lark_cli, self.settings.feishu_as),
                    self.feishu_sem,
                ),
                llm,
                require_source=self.settings.require_source,
                review_reasoning_effort=self.settings.openai_review_reasoning_effort,
                visual_qa=VisualQAController.from_settings(self.settings, llm=llm),
                generation_repair_rounds=self.settings.generation_repair_rounds,
                sectional_generation_enabled=self.settings.sectional_generation_enabled,
                sectional_generation_workers=self.settings.sectional_generation_workers,
                quality_repair_rounds=self.settings.quality_repair_rounds,
                on_workflow_event=lambda event, detail="": self.client.transition(
                    job_id, worker_id, event, detail
                ),
            )
            result = pipeline.process_ref(
                PaperRef(source_id, str(job.get("source_url") or f"https://arxiv.org/abs/{source_id}")),
                event=None,
                send_progress=False,
                force=False,
                resume_published_url=str(job.get("doc_url") or ""),
                resume_published_checkpoint=str(job.get("checkpoint_json") or ""),
                force_rebuild=bool(job.get("rebuild_pipeline")),
                retry_feedback=str(job.get("retry_feedback") or ""),
            )
            record = local_store.get_paper(source_id)
            issues = [
                issue for issue in local_store.list_review_issues(100, "paper", source_id)
                if int(issue.get("id") or 0) > review_start_id
            ][:20]
            cleanup_payload = {}
            if not result.error and result.doc_url:
                cleanup = cleanup_source_cache(self.settings.workdir, "paper", source_id)
                cleanup_payload = {"files": cleanup.files_removed, "bytes": cleanup.bytes_removed}
            finish_payload = {
                "job_id": job_id,
                "worker_id": worker_id,
                "doc_url": result.doc_url,
                "error": result.error,
                "paper": record.__dict__ if record else {},
                "review_issues": issues,
                "cleanup": cleanup_payload,
            }
            finish = self.client.finish(finish_payload)
        except Exception as exc:
            record = local_store.get_paper(source_id)
            try:
                self.client.finish({
                    "job_id": job_id,
                    "worker_id": worker_id,
                    "doc_url": getattr(result, "doc_url", "") if result else "",
                    "error": f"remote-worker: {type(exc).__name__}: {str(exc)[:1000]}",
                    "paper": record.__dict__ if record else {},
                    "review_issues": [],
                })
            except Exception:
                pass
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=2)

    def _heartbeat_loop(self, job_id: int, worker_id: str, stop: threading.Event) -> None:
        interval = max(5, int(self.settings.queue_heartbeat_seconds))
        while not stop.wait(interval):
            try:
                if not self.client.heartbeat(job_id, worker_id):
                    return
            except Exception:
                continue


def run_remote_paper_worker(settings) -> None:
    RemotePaperWorker(settings).run_forever()


def _worker_id(payload: dict) -> str:
    value = str(payload.get("worker_id") or "").strip()[:200]
    if not value.startswith("remote:"):
        raise ValueError("invalid remote worker id")
    return value
