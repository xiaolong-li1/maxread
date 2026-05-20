from __future__ import annotations

import traceback
from dataclasses import dataclass
from hashlib import sha256
from typing import Optional

from .arxiv import ArxivClient
from .db import Store
from .feishu import FeishuClient
from .models import ArxivMetadata, FeishuEvent, PaperBundle, PaperRef
from .openai_client import OpenAIClient
from .prompts import FINAL_SYSTEM_PROMPT, build_final_user_prompt
from .publishing import publish_marker_image
from .render import ensure_priority_figure_markers, figure_placeholders, markdown_to_docx_xml, polish_markdown, prepare_key_figures, remove_false_material_warning
from .review import review_markdown_with_report


@dataclass
class ProcessResult:
    paper_id: str
    doc_url: str
    cached: bool
    error: str = ""


class MaxReadPipeline:
    def __init__(
        self,
        store: Store,
        arxiv: ArxivClient,
        feishu: FeishuClient,
        llm: Optional[OpenAIClient],
        require_source: bool = True,
    ):
        self.store = store
        self.arxiv = arxiv
        self.feishu = feishu
        self.llm = llm
        self.require_source = require_source

    def process_ref(self, ref: PaperRef, event: Optional[FeishuEvent] = None, send_progress: bool = True) -> ProcessResult:
        record = self.store.get_paper(ref.paper_id)
        if record and record.status == "done" and record.doc_url:
            if event and send_progress:
                self._reply(event, f"哥，之前的文档在这里 {record.doc_url}", "cached", ref.paper_id)
            return ProcessResult(ref.paper_id, record.doc_url, cached=True)

        if event and send_progress:
            self.store.add_job(event.event_id, event.message_id, event.chat_id, ref.paper_id, "started")

        try:
            if event and send_progress:
                self._reply(event, f"[了解] 收到了：{ref.paper_id}", "start", ref.paper_id)
            self.store.upsert_paper(ref.paper_id, "fetching", arxiv_url=ref.url)
            try:
                if event and send_progress:
                    self._reply(event, f"[下载中] 正在下载论文：{ref.paper_id}", "downloading", ref.paper_id)
                bundle = self.arxiv.fetch(ref.paper_id)
            except Exception as exc:
                bundle = _limited_bundle(ref.paper_id, str(exc))
            self.store.upsert_paper(
                ref.paper_id,
                "summarizing",
                title=bundle.metadata.title,
                authors=", ".join(bundle.metadata.authors),
                arxiv_url=bundle.metadata.abs_url,
                pdf_path=str(bundle.pdf_path or ""),
                source_path=str(bundle.source_path or ""),
            )
            if self.require_source and not bundle.source_text:
                message = _source_required_message(ref.paper_id, bundle.parse_warnings)
                self.store.upsert_paper(ref.paper_id, "needs_source", error=message)
                if event and send_progress:
                    self._reply(event, message, "need-source", ref.paper_id)
                return ProcessResult(ref.paper_id, "", cached=False, error=message)

            if event and send_progress:
                self._reply(event, f"[在做了] 正在读论文：{ref.paper_id}", "reading", ref.paper_id)

            try:
                if not self.llm:
                    raise RuntimeError("OPENAI_API_KEY not configured or --no-openai was used")
                figures = prepare_key_figures(bundle)
                figure_inserts = figure_placeholders(figures)
                markdown = self.llm.responses_text(FINAL_SYSTEM_PROMPT, build_final_user_prompt(bundle, figure_inserts))
                markdown = polish_markdown(markdown)
                markdown = remove_false_material_warning(markdown, bundle)
                markdown = ensure_priority_figure_markers(markdown, figure_inserts)
                markers = [marker for marker, _path, _caption in figure_inserts]
                review_warnings = []
                if event and send_progress:
                    self._reply(event, f"[审阅中] 正在审阅/修订：{ref.paper_id}", "reviewing", ref.paper_id)
                try:
                    review = review_markdown_with_report(self.llm, markdown, markers, kind="paper")
                    markdown = review.markdown
                    self.store.add_review_issues("paper", ref.paper_id, review.issues)
                    for issue in review.issues:
                        review_warnings.append(f"review:{issue.category}:{issue.severity}:{issue.detail}")
                except Exception as review_exc:
                    review_warnings.append(f"Review pass failed: {review_exc}")
                markdown = polish_markdown(markdown)
                markdown = remove_false_material_warning(markdown, bundle)
                markdown = ensure_priority_figure_markers(markdown, figure_inserts)
                missing_markers = [marker for marker in markers if marker not in markdown]
                publish_warnings = review_warnings + [f"missing-marker:{marker}" for marker in missing_markers]
                xml = markdown_to_docx_xml(markdown)
            except Exception as exc:
                message = f"总结模型调用失败，未发布文档：{exc}"
                self.store.upsert_paper(ref.paper_id, "summary_failed", error=message)
                if event and send_progress:
                    self._reply(event, f"这篇我没读成：{ref.paper_id}\n原因：{message}", "summary-fail", ref.paper_id)
                return ProcessResult(ref.paper_id, "", cached=False, error=message)

            self.store.upsert_paper(ref.paper_id, "writing_doc")
            if event and send_progress:
                self._reply(event, f"[敲键盘] 在写飞书文档：{ref.paper_id}", "writing", ref.paper_id)
            doc = self.feishu.create_docx(bundle.metadata.title or ref.paper_id)
            self.feishu.overwrite_docx_xml(doc["url"], xml)
            figure_warnings = list(publish_warnings)
            for marker, image_path, caption in figure_inserts:
                if marker not in markdown:
                    continue
                publish_result = publish_marker_image(self.feishu, doc["url"], image_path, caption, marker)
                figure_warnings.extend(publish_result.warnings)
            self.feishu.publish_docx(doc["token"])
            self.store.upsert_paper(ref.paper_id, "done", doc_url=doc["url"], doc_token=doc["token"], error="; ".join(figure_warnings))
            if event and send_progress:
                self._reply(event, f"哥，读完了：{doc['url']}", "done", ref.paper_id)
            return ProcessResult(ref.paper_id, doc["url"], cached=False)
        except Exception as exc:
            error = f"{exc}\n{traceback.format_exc()}"
            self.store.upsert_paper(ref.paper_id, "failed", error=error)
            if event and send_progress:
                self._reply(event, f"这篇我没读成：{ref.paper_id}\n原因：{_short_error(exc)}", "fail", ref.paper_id)
            return ProcessResult(ref.paper_id, "", cached=False, error=str(exc))

    def _reply(self, event: FeishuEvent, text: str, prefix: str, paper_id: str) -> None:
        stage = _progress_stage(prefix)
        if stage:
            try:
                self.feishu.react_progress(event.message_id, stage)
            except Exception:
                pass
            return
        key = sha256(f"{prefix}:{event.event_id}:{paper_id}".encode("utf-8")).hexdigest()[:32]
        text = _clip_reply(text)
        try:
            self.feishu.reply_text(event.message_id, text, idempotency_key=key)
        except Exception:
            try:
                self.feishu.reply_text(event.message_id, text)
            except Exception:
                pass


def _progress_stage(prefix: str) -> str:
    return prefix if prefix in {"start", "downloading", "reading", "reviewing", "writing"} else ""


def _limited_bundle(paper_id: str, reason: str) -> PaperBundle:
    metadata = ArxivMetadata(
        paper_id=paper_id,
        title=f"arXiv {paper_id}",
        authors=[],
        summary="arXiv 当前对本机限流，暂时无法获取论文正文。本文档用于占位和稍后重试。",
        published="",
        updated="",
        categories=[],
        pdf_url=f"https://arxiv.org/pdf/{paper_id}.pdf",
        abs_url=f"https://arxiv.org/abs/{paper_id}",
    )
    return PaperBundle(
        metadata=metadata,
        pdf_path=None,
        source_path=None,
        source_dir=None,
        source_text="",
        pdf_text="",
        parse_warnings=[f"arXiv fetch failed: {reason}"],
    )


def _source_required_message(paper_id: str, warnings) -> str:
    details = "；".join(str(w) for w in warnings if "source" in str(w).lower() or "tex" in str(w).lower())
    if not details:
        details = "TeX source unavailable"
    return (
        f"这篇我先不生成完整文档：{paper_id}\n"
        f"原因：需要 TeX source 才能稳定解析公式、图片和图文对应；当前没有拿到 source。\n"
        f"细节：{details}\n"
        f"你可以在 arXiv 网页点 Download source 下载源码包，然后本地执行：\n"
        f"cd /Users/xiaolong/projects/maxread && python3 -m maxread.cli import-source {paper_id} /path/to/source.tar"
    )


def _clip_reply(text: str, max_chars: int = 900) -> str:
    text = str(text)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."


def _short_error(exc: Exception, max_chars: int = 500) -> str:
    return _clip_reply(str(exc), max_chars)
