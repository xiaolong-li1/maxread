from __future__ import annotations

import os
import json
import traceback
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Optional

from .article_prompts import ARTICLE_SYSTEM_PROMPT, build_article_user_prompt
from .db import Store
from .feishu import FeishuClient, doc_token_from_url
from .models import ArticleBundle, FeishuEvent
from .openai_client import OpenAIClient
from .publishing import publish_marker_image
from .quality import PrePublishQualityError, blocking_quality_warnings, verify_published_docx
from .quality_repair import QualityRepairResult, repair_until_quality_passes
from .render import markdown_to_docx_xml, polish_markdown
from .review import review_markdown_with_report
from .sources import WebRef
from .web_article import WebArticleClient
from .visual_qa import VisualQAController
from .workflow import PublishedCheckpoint, WorkflowEvent


@dataclass
class ArticleProcessResult:
    article_id: str
    doc_url: str
    cached: bool
    error: str = ""


class ArticlePipeline:
    def __init__(
        self,
        store: Store,
        web: WebArticleClient,
        feishu: FeishuClient,
        llm: Optional[OpenAIClient],
        review_reasoning_effort: str = "",
        visual_qa: Optional[VisualQAController] = None,
        quality_repair_rounds: int = 3,
        on_workflow_event=None,
    ):
        self.store = store
        self.web = web
        self.feishu = feishu
        self.llm = llm
        self.review_reasoning_effort = review_reasoning_effort
        self.visual_qa = visual_qa
        self.quality_repair_rounds = max(0, int(quality_repair_rounds))
        self.on_workflow_event = on_workflow_event

    def process_ref(
        self,
        ref: WebRef,
        event: Optional[FeishuEvent] = None,
        send_progress: bool = True,
        resume_published_url: str = "",
        resume_published_checkpoint: str = "",
    ) -> ArticleProcessResult:
        article_id = sha256(ref.url.encode("utf-8")).hexdigest()[:16]
        record = self.store.get_document(article_id)
        if record and record.status == "done" and record.doc_url:
            if event and send_progress:
                self._reply(event, f"哥，之前的文档在这里 {record.doc_url}", "cached", article_id)
            return ArticleProcessResult(article_id, record.doc_url, cached=True)
        try:
            if event and send_progress:
                self._reply(event, "[了解] 收到了：网页文章", "start", article_id)
                self._reply(event, f"[下载中] 正在抓取网页：{_host(ref.url)}", "downloading", article_id)
            published_url = str(resume_published_url or "").strip()
            if not published_url and record and record.status == "quality_failed":
                published_url = record.doc_url
            checkpoint = PublishedCheckpoint.from_json(resume_published_checkpoint, fallback_url=published_url)
            if checkpoint:
                return self._resume_published_doc(ref, record, article_id, checkpoint)
            self._workflow(WorkflowEvent.FETCH_STARTED, ref.url)
            self.store.upsert_document(article_id, "fetching", kind="article", source_url=ref.url)
            bundle = self.web.fetch(ref.url)
            self._workflow(WorkflowEvent.SOURCE_READY, ref.url)
            self.store.upsert_document(article_id, "summarizing", kind="article", source_url=ref.url, title=bundle.title)
            if event and send_progress:
                self._reply(event, f"[在做了] 正在读文章：{_clip(bundle.title, 40)}", "reading", article_id)
            self._workflow(WorkflowEvent.GENERATION_STARTED, article_id)

            try:
                if not self.llm:
                    raise RuntimeError("OPENAI_API_KEY not configured or --no-openai was used")
                image_inserts = _image_placeholders(bundle)
                bundle.text = _replace_article_image_markers(bundle.text, image_inserts)
                markdown = self.llm.responses_text(ARTICLE_SYSTEM_PROMPT, build_article_user_prompt(bundle, image_inserts))
                self._workflow(WorkflowEvent.DRAFT_READY, article_id)
                markdown = polish_markdown(markdown)
                markers = [marker for marker, _path, _caption, _source_index in image_inserts]
                review_warnings = []
                if event and send_progress:
                    self._reply(event, f"[审阅中] 正在审阅/修订：{_clip(bundle.title or '网页文章', 40)}", "reviewing", article_id)
                try:
                    review = review_markdown_with_report(self.llm, markdown, markers, kind="article", reasoning_effort=self.review_reasoning_effort)
                    markdown = review.markdown
                    self.store.add_review_issues("article", article_id, review.issues)
                    for issue in review.issues:
                        review_warnings.append(f"review:{issue.category}:{issue.severity}:{issue.detail}")
                except Exception as review_exc:
                    review_warnings.append(f"Review pass failed: {review_exc}")
                self._workflow(WorkflowEvent.REVIEW_COMPLETED, article_id)
                quality_result = repair_until_quality_passes(
                    self.llm,
                    markdown,
                    markers,
                    render_xml=markdown_to_docx_xml,
                    normalize_markdown=polish_markdown,
                    max_repair_rounds=self.quality_repair_rounds,
                    kind="article",
                    reasoning_effort=self.review_reasoning_effort,
                    on_workflow_event=self._workflow,
                )
                _write_article_quality_artifacts(image_inserts, quality_result)
                markdown = quality_result.markdown
                xml = quality_result.xml
                missing_markers = [marker for marker in markers if marker not in markdown]
                publish_warnings = (
                    review_warnings
                    + quality_result.repair_warnings
                    + [f"missing-marker:{marker}" for marker in missing_markers]
                )
                expected_image_count = sum(1 for marker in markers if marker in markdown)
                expected_latex_count = xml.count("<latex>")
                expected_table_count = xml.count("<table>")
                publish_warnings.extend(quality_result.warnings)
                if quality_result.blocking_warnings:
                    self._workflow(WorkflowEvent.QUALITY_REJECTED, "; ".join(quality_result.blocking_warnings))
                    raise PrePublishQualityError("; ".join(quality_result.blocking_warnings))
                self._workflow(WorkflowEvent.QUALITY_PASSED, article_id)
            except PrePublishQualityError as exc:
                message = f"文章已读完，但发布前格式质检未通过，未发布文档：{exc}"
                self.store.upsert_document(article_id, "quality_failed", error=message)
                if event and send_progress:
                    self._reply(event, f"这篇已读完，但发布前格式质检未通过：{ref.url}\n原因：{message}", "quality-fail", article_id)
                return ArticleProcessResult(article_id, "", cached=False, error=message)
            except Exception as exc:
                message = f"文章总结模型调用失败，未发布文档：{exc}"
                self._workflow(WorkflowEvent.FAIL, message)
                self.store.upsert_document(article_id, "summary_failed", error=message)
                if event and send_progress:
                    self._reply(event, f"这篇我没读成：{ref.url}\n原因：{message}", "summary-fail", article_id)
                return ArticleProcessResult(article_id, "", cached=False, error=message)

            self.store.upsert_document(article_id, "writing_doc")
            if event and send_progress:
                self._reply(event, "[敲键盘] 在写飞书文档", "writing", article_id)
            doc = self.feishu.create_docx(bundle.title or ref.url)
            self.feishu.overwrite_docx_xml(doc["url"], xml)
            warnings = list(publish_warnings)
            for marker, image_path, caption, _source_index in image_inserts:
                if marker not in markdown:
                    continue
                publish_result = publish_marker_image(self.feishu, doc["url"], image_path, caption, marker)
                warnings.extend(publish_result.warnings)
            self.feishu.publish_docx(doc["token"])
            self._workflow(
                WorkflowEvent.PUBLISH_SUCCEEDED,
                PublishedCheckpoint(
                    doc_url=doc["url"],
                    expected_title=bundle.title or ref.url,
                    expected_image_min=expected_image_count,
                    expected_latex_min=expected_latex_count,
                    expected_table_min=expected_table_count,
                ).to_json(),
            )
            post_publish_warnings = verify_published_docx(
                self.feishu,
                doc["url"],
                expected_title=bundle.title or ref.url,
                expected_image_min=expected_image_count,
                expected_latex_min=expected_latex_count,
                expected_table_min=expected_table_count,
            )
            if self.visual_qa:
                self._workflow(WorkflowEvent.VISUAL_QA_STARTED, doc["url"])
                visual_result = self.visual_qa.run(
                    self.feishu,
                    doc["url"],
                    initial_warnings=post_publish_warnings,
                    source_id=article_id,
                    expected_image_min=expected_image_count,
                    expected_formula_min=expected_latex_count,
                    expected_table_min=expected_table_count,
                    on_workflow_event=self._workflow,
                )
                _write_article_visual_qa_artifact(image_inserts, visual_result)
                warnings.extend(visual_result.warnings)
                if visual_result.changed:
                    warnings.extend(
                        verify_published_docx(
                            self.feishu,
                            doc["url"],
                            expected_title=bundle.title or ref.url,
                            expected_image_min=expected_image_count,
                            expected_latex_min=expected_latex_count,
                            expected_table_min=expected_table_count,
                        )
                    )
                else:
                    warnings.extend(post_publish_warnings)
            else:
                warnings.extend(post_publish_warnings)
            post_publish_blocking = blocking_quality_warnings(warnings)
            if post_publish_blocking:
                message = "文档已生成，但发布后质检失败，暂不交付：" + "; ".join(post_publish_blocking)
                self._workflow(WorkflowEvent.QUALITY_REJECTED, message)
                self.store.upsert_document(
                    article_id,
                    "quality_failed",
                    doc_url=doc["url"],
                    doc_token=doc["token"],
                    error=message,
                )
                if event and send_progress:
                    self._reply(event, f"这篇发布后质检未通过：{ref.url}\n原因：{message}", "quality-fail", article_id)
                return ArticleProcessResult(article_id, doc["url"], cached=False, error=message)
            self.store.upsert_document(article_id, "done", doc_url=doc["url"], doc_token=doc["token"], error="; ".join(warnings))
            self._workflow(WorkflowEvent.COMPLETE, doc["url"])
            if event and send_progress:
                self._reply(event, f"哥，读完了：{doc['url']}", "done", article_id)
            return ArticleProcessResult(article_id, doc["url"], cached=False)
        except Exception as exc:
            error = f"{exc}\n{traceback.format_exc()}"
            self._workflow(WorkflowEvent.FAIL, _clip(str(exc), 500))
            self.store.upsert_document(article_id, "failed", error=error)
            if event and send_progress:
                self._reply(event, f"这篇我没读成：{ref.url}\n原因：{_clip(str(exc), 500)}", "fail", article_id)
            return ArticleProcessResult(article_id, "", cached=False, error=str(exc))

    def _workflow(self, event: WorkflowEvent, detail: str = "") -> None:
        if self.on_workflow_event is not None:
            self.on_workflow_event(event, detail)

    def _resume_published_doc(self, ref: WebRef, record, article_id: str, checkpoint: PublishedCheckpoint) -> ArticleProcessResult:
        """Recheck an existing published article document without another model call."""
        doc_url = checkpoint.doc_url
        doc_token = record.doc_token if record else doc_token_from_url(doc_url)
        expected_title = checkpoint.expected_title or (record.title if record else "")
        try:
            self._workflow(WorkflowEvent.RESUME_PUBLISHED, doc_url)
            warnings = list(
                verify_published_docx(
                    self.feishu,
                    doc_url,
                    expected_title=expected_title,
                    expected_image_min=checkpoint.expected_image_min,
                    expected_latex_min=checkpoint.expected_latex_min,
                    expected_table_min=checkpoint.expected_table_min,
                )
            )
            if self.visual_qa:
                self._workflow(WorkflowEvent.VISUAL_QA_STARTED, doc_url)
                visual_result = self.visual_qa.run(
                    self.feishu,
                    doc_url,
                    initial_warnings=warnings,
                    source_id=article_id,
                    expected_image_min=checkpoint.expected_image_min,
                    expected_formula_min=checkpoint.expected_latex_min,
                    expected_table_min=checkpoint.expected_table_min,
                    on_workflow_event=self._workflow,
                )
                warnings.extend(visual_result.warnings)
                if visual_result.changed:
                    warnings.extend(
                        verify_published_docx(
                            self.feishu,
                            doc_url,
                            expected_title=expected_title,
                            expected_image_min=checkpoint.expected_image_min,
                            expected_latex_min=checkpoint.expected_latex_min,
                            expected_table_min=checkpoint.expected_table_min,
                        )
                    )
            blocking = blocking_quality_warnings(warnings)
            if blocking:
                message = "文档已生成，但发布后质检失败，暂不交付：" + "; ".join(blocking)
                self._workflow(WorkflowEvent.QUALITY_REJECTED, message)
                self.store.upsert_document(
                    article_id,
                    "quality_failed",
                    doc_url=doc_url,
                    doc_token=doc_token,
                    error=message,
                )
                return ArticleProcessResult(article_id, doc_url, cached=False, error=message)
            self.store.upsert_document(
                article_id,
                "done",
                doc_url=doc_url,
                doc_token=doc_token,
                error="; ".join(warnings),
            )
            self._workflow(WorkflowEvent.COMPLETE, doc_url)
            return ArticleProcessResult(article_id, doc_url, cached=False)
        except Exception as exc:
            message = f"发布后复检失败，未重新生成文档：{_clip(str(exc), 500)}"
            try:
                self._workflow(WorkflowEvent.FAIL, message)
            except Exception:
                pass
            self.store.upsert_document(article_id, "quality_failed", error=message)
            return ArticleProcessResult(article_id, doc_url, cached=False, error=message)

    def _reply(self, event: FeishuEvent, text: str, prefix: str, article_id: str) -> None:
        stage = _progress_stage(prefix)
        if stage:
            try:
                self.feishu.react_progress(event.message_id, stage)
            except Exception:
                pass
            return
        key = sha256(f"article:{prefix}:{event.event_id}:{article_id}".encode("utf-8")).hexdigest()[:32]
        try:
            self.feishu.reply_text(event.message_id, _clip(text, 900), idempotency_key=key)
        except Exception:
            pass


def _progress_stage(prefix: str) -> str:
    return prefix if prefix in {"start", "downloading", "reading", "reviewing", "writing"} else ""


def _image_placeholders(bundle: ArticleBundle):
    inserts = []
    index = 0
    max_images = int(os.environ.get("MAXREAD_ARTICLE_MAX_IMAGES", "32"))
    for image in bundle.images:
        if image.local_path and image.local_path.exists():
            index += 1
            marker = f"[MaxReadFigure:{index}:{image.local_path.stem}]"
            inserts.append((marker, image.local_path, image.caption or image.alt or image.url, image.source_index))
    return inserts[:max_images]


def _replace_article_image_markers(text: str, image_inserts) -> str:
    markers_by_source = {str(source_index): (marker, caption) for marker, _path, caption, source_index in image_inserts}

    def repl(match):
        source_index = match.group(1)
        fallback_caption = match.group(2).strip()
        item = markers_by_source.get(source_index)
        if not item:
            return fallback_caption
        marker, real_caption = item
        return f"{marker}\n**图：{real_caption or fallback_caption}**"

    return __import__("re").sub(r"\[ArticleImage:(\d+)\]\s*(.*)", repl, text)


def _write_article_quality_artifacts(image_inserts, result: QualityRepairResult) -> None:
    image_paths = [Path(path) for _marker, path, _caption, _source_index in image_inserts if path]
    if not image_paths:
        return
    root = image_paths[0].parent
    if root.name in {"images", "rendered"}:
        root = root.parent
    try:
        artifact_dir = root / "pipeline_artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        for attempt in result.attempts:
            round_label = f"round-{attempt.round_index}"
            _atomic_write(artifact_dir / f"05-quality-{round_label}.md", attempt.markdown)
            _atomic_write(artifact_dir / f"06-quality-{round_label}.xml", attempt.xml)
            _atomic_write(
                artifact_dir / f"07-quality-{round_label}.json",
                json.dumps(
                    {
                        "round": attempt.round_index,
                        "warnings": attempt.warnings,
                        "blocking_warnings": attempt.blocking_warnings,
                        "repair_warnings": attempt.repair_warnings,
                        "changed": attempt.changed,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            if attempt.model_response:
                _atomic_write(
                    artifact_dir / f"05-quality-{round_label}-response.txt",
                    attempt.model_response,
                )
    except Exception:
        return


def _write_article_visual_qa_artifact(image_inserts, result) -> None:
    image_paths = [Path(path) for _marker, path, _caption, _source_index in image_inserts if path]
    if not image_paths:
        return
    root = image_paths[0].parent
    if root.name in {"images", "rendered"}:
        root = root.parent
    try:
        artifact_dir = root / "pipeline_artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            artifact_dir / "09-visual-qa.json",
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
        )
    except Exception:
        return


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(str(content or ""), encoding="utf-8")
    temporary.replace(path)


def _host(url: str) -> str:
    return url.split("/", 3)[2] if "://" in url else url


def _clip(text: str, max_chars: int) -> str:
    text = str(text)
    return text if len(text) <= max_chars else text[:max_chars] + "..."
