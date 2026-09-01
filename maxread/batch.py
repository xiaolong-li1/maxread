from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from hashlib import sha256
from threading import BoundedSemaphore
from typing import List

from .arxiv import ArxivClient
from .article_pipeline import ArticlePipeline
from .config import Settings
from .db import Store
from .document_source import DocumentSourceClient
from .feishu import FeishuClient
from .models import FeishuEvent, PaperRef
from .openai_client import OpenAIClient
from .pipeline import MaxReadPipeline
from .sources import WebRef
from .web_article import WebArticleClient
from .visual_qa import VisualQAController


@dataclass
class BatchItemResult:
    label: str
    doc_url: str
    error: str = ""


class BatchProcessor:
    def __init__(self, settings: Settings, no_openai: bool = False):
        self.settings = settings
        self.no_openai = no_openai
        self.llm_sem = BoundedSemaphore(settings.batch_llm_concurrency)
        self.feishu_sem = BoundedSemaphore(settings.batch_feishu_concurrency)

    def process(self, paper_refs: List[PaperRef], web_refs: List[WebRef], event: FeishuEvent) -> List[BatchItemResult]:
        items = [("paper", ref) for ref in paper_refs] + [("article", ref) for ref in web_refs]
        max_items = min(len(items), self.settings.batch_max_items)
        items = items[:max_items]
        total = len(items)
        feishu = FeishuClient(self.settings.lark_cli, self.settings.feishu_as)
        _react(feishu, event, "start")
        _reply(feishu, event, _queue_message(items, self.settings.batch_workers), "batch-start")
        _reply(feishu, event, f"[下载中] 正在抓取/下载，最多并行 {self.settings.batch_workers} 篇；飞书写入会限流为 {self.settings.batch_feishu_concurrency} 篇", "batch-download")
        results: List[BatchItemResult] = []
        with ThreadPoolExecutor(max_workers=self.settings.batch_workers) as executor:
            futures = [executor.submit(self._process_one, kind, ref) for kind, ref in items]
            done = 0
            for future in as_completed(futures):
                done += 1
                result = future.result()
                results.append(result)
                status = "完成" if result.doc_url else "失败"
                _reply(feishu, event, f"[在做了] 已处理 {done}/{total}：{result.label}（{status}）", f"batch-progress-{done}")
        _reply(feishu, event, _summary(results), "batch-done")
        return results

    def _process_one(self, kind: str, ref) -> BatchItemResult:
        store = Store(self.settings.db_path)
        feishu = _LimitedFeishu(FeishuClient(self.settings.lark_cli, self.settings.feishu_as), self.feishu_sem)
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
        )
        try:
            if kind == "paper":
                arxiv = ArxivClient(
                    self.settings.workdir,
                    timeout=self.settings.arxiv_timeout,
                    parallel_streams=self.settings.arxiv_parallel_streams,
                    parallel_min_bytes=self.settings.arxiv_parallel_min_bytes,
                )
                pipeline = MaxReadPipeline(
                    store,
                    arxiv,
                    feishu,
                    llm,
                    require_source=self.settings.require_source,
                    review_reasoning_effort=self.settings.openai_review_reasoning_effort,
                    visual_qa=VisualQAController.from_settings(self.settings, llm=llm),
                    generation_repair_rounds=self.settings.generation_repair_rounds,
                    sectional_generation_enabled=self.settings.sectional_generation_enabled,
                    sectional_generation_workers=self.settings.sectional_generation_workers,
                    quality_repair_rounds=self.settings.quality_repair_rounds,
                    document_client=DocumentSourceClient(
                        self.settings.workdir,
                        arxiv=arxiv,
                        timeout=self.settings.arxiv_timeout,
                    ),
                )
                result = pipeline.process_ref(ref, send_progress=False)
                return BatchItemResult(ref.paper_id, result.doc_url, result.error)
            pipeline = ArticlePipeline(
                store,
                WebArticleClient(self.settings.workdir, timeout=self.settings.arxiv_timeout),
                feishu,
                llm,
                review_reasoning_effort=self.settings.openai_review_reasoning_effort,
                visual_qa=VisualQAController.from_settings(self.settings, llm=llm),
                quality_repair_rounds=self.settings.quality_repair_rounds,
            )
            result = pipeline.process_ref(ref, send_progress=False)
            return BatchItemResult(ref.url, result.doc_url, result.error)
        finally:
            store.close()


class _LimitedLLM:
    def __init__(self, inner: OpenAIClient, sem: BoundedSemaphore):
        self.inner = inner
        self.sem = sem

    def responses_text(self, system: str, user: str, **kwargs) -> str:
        with self.sem:
            return self.inner.responses_text(system, user, **kwargs)


class _LimitedFeishu:
    def __init__(self, inner: FeishuClient, sem: BoundedSemaphore):
        self.inner = inner
        self.sem = sem

    def __getattr__(self, name: str):
        attr = getattr(self.inner, name)
        if name in {"create_docx", "overwrite_docx", "overwrite_docx_xml", "insert_image", "remove_text", "publish_docx", "fetch_docx", "block_replace"}:
            def wrapped(*args, **kwargs):
                with self.sem:
                    return attr(*args, **kwargs)
            return wrapped
        return attr


def _reply(feishu: FeishuClient, event: FeishuEvent, text: str, prefix: str) -> None:
    stage = _batch_progress_stage(prefix)
    if stage:
        _react(feishu, event, stage)
        return
    key = sha256(f"batch:{prefix}:{event.event_id}".encode("utf-8")).hexdigest()[:32]
    try:
        feishu.reply_text(event.message_id, text[:900], idempotency_key=key)
    except Exception:
        pass


def _react(feishu: FeishuClient, event: FeishuEvent, stage: str) -> None:
    try:
        feishu.react_progress(event.message_id, stage)
    except Exception:
        pass


def _batch_progress_stage(prefix: str) -> str:
    if prefix == "batch-download":
        return "downloading"
    if prefix.startswith("batch-progress-"):
        return "reading"
    return ""


def _summary(results: List[BatchItemResult]) -> str:
    done = [r for r in results if r.doc_url]
    failed = [r for r in results if not r.doc_url]
    lines = [f"读完了 {len(done)}/{len(results)} 篇：", ""]
    for index, result in enumerate(done, start=1):
        lines.append(f"{index}. {result.label}")
        lines.append(result.doc_url)
    if failed:
        lines.extend(["", f"失败 {len(failed)} 篇："])
        for result in failed:
            reason = result.error.replace("\n", " ")[:180]
            lines.append(f"- {result.label}：{reason}")
    return "\n".join(lines)


def _queue_message(items, workers: int) -> str:
    total = len(items)
    workers = max(1, workers)
    waves = (total + workers - 1) // workers
    wait_minutes = max(0, waves - 1) * 2
    finish_minutes = max(3, waves * 4)
    lines = [
        f"收到 {total} 篇，开始并行处理",
        f"并发数：{workers}；预计首批 3-6 分钟内完成，全部约 {finish_minutes}-{finish_minutes + 4} 分钟。",
        "排队顺序：",
    ]
    for index, (kind, ref) in enumerate(items, start=1):
        label = ref.paper_id if kind == "paper" else ref.url
        wave = (index - 1) // workers + 1
        wait = 0 if wave == 1 else (wave - 1) * 2
        lines.append(f"{index}. {label}（第 {wave} 批，预计等待约 {wait} 分钟）")
    if wait_minutes:
        lines.append(f"最后一批预计等待约 {wait_minutes} 分钟后开始。")
    return "\n".join(lines)
