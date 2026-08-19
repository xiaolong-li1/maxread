from __future__ import annotations

import traceback
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Callable, Optional

from .arxiv import ArxivClient
from .db import Store
from .feishu import FeishuClient, doc_token_from_url
from .models import ArxivMetadata, FeishuEvent, PaperBundle, PaperRef
from .openai_client import OpenAIClient
from .prompts import FINAL_SYSTEM_PROMPT, build_final_user_prompt
from .publishing import publish_marker_image
from .quality import PrePublishQualityError, blocking_quality_warnings, paper_markdown_completeness_errors, verify_published_docx
from .quality_repair import QualityRepairResult, repair_until_quality_passes
from .repository import find_repository_url
from .render import ensure_priority_figure_markers, ensure_referenced_figure_markers, figure_placeholders, markdown_to_docx_xml, polish_markdown, prepare_key_figures, remove_false_material_warning
from .review import review_markdown_with_report
from .visual_qa import VisualQAController
from .workflow import PublishedCheckpoint, WorkflowEvent


@dataclass
class ProcessResult:
    paper_id: str
    doc_url: str
    cached: bool
    error: str = ""


class IncompleteGenerationError(RuntimeError):
    """The model returned text, but it did not satisfy the document contract."""

    def __init__(self, errors, attempts: int):
        self.errors = list(errors)
        self.attempts = attempts
        super().__init__(", ".join(self.errors) or "unknown-format-error")


class MaxReadPipeline:
    def __init__(
        self,
        store: Store,
        arxiv: ArxivClient,
        feishu: FeishuClient,
        llm: Optional[OpenAIClient],
        require_source: bool = True,
        review_reasoning_effort: str = "",
        visual_qa: Optional[VisualQAController] = None,
        quality_repair_rounds: int = 3,
        on_workflow_event=None,
    ):
        self.store = store
        self.arxiv = arxiv
        self.feishu = feishu
        self.llm = llm
        self.require_source = require_source
        self.review_reasoning_effort = review_reasoning_effort
        self.visual_qa = visual_qa
        self.quality_repair_rounds = max(0, int(quality_repair_rounds))
        self.on_workflow_event = on_workflow_event

    def process_ref(
        self,
        ref: PaperRef,
        event: Optional[FeishuEvent] = None,
        send_progress: bool = True,
        force: bool = False,
        resume_published_url: str = "",
        resume_published_checkpoint: str = "",
    ) -> ProcessResult:
        record = self.store.get_paper(ref.paper_id)
        if not force and record and record.status == "done" and record.doc_url:
            if event and send_progress:
                self._reply(event, f"哥，之前的文档在这里 {record.doc_url}", "cached", ref.paper_id)
            return ProcessResult(ref.paper_id, record.doc_url, cached=True)

        if event and send_progress:
            self.store.add_job(event.event_id, event.message_id, event.chat_id, ref.paper_id, "started")

        try:
            if event and send_progress:
                self._reply(event, f"[了解] 收到了：{ref.paper_id}", "start", ref.paper_id)
            published_url = str(resume_published_url or "").strip()
            if not published_url and record and record.status == "quality_failed":
                published_url = record.doc_url
            checkpoint = PublishedCheckpoint.from_json(resume_published_checkpoint, fallback_url=published_url)
            if checkpoint:
                return self._resume_published_doc(ref, record, checkpoint)
            self._workflow(WorkflowEvent.FETCH_STARTED, ref.paper_id)
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
            _write_paper_artifact(
                bundle,
                "00-source-summary.json",
                json.dumps(
                    {
                        "paper_id": ref.paper_id,
                        "title": bundle.metadata.title,
                        "source_chars": len(bundle.source_text or ""),
                        "pdf_chars": len(bundle.pdf_text or ""),
                        "figures": len(bundle.source_figures),
                        "tables": len(bundle.source_tables),
                        "parse_warnings": list(bundle.parse_warnings),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            _remove_paper_artifact(bundle, "08-failure.txt")
            if self.require_source and not bundle.source_text:
                message = _source_required_message(ref.paper_id, bundle.parse_warnings)
                self._workflow(WorkflowEvent.SOURCE_MISSING, message)
                self.store.upsert_paper(ref.paper_id, "needs_source", error=message)
                if event and send_progress:
                    self._reply(event, message, "need-source", ref.paper_id)
                return ProcessResult(ref.paper_id, "", cached=False, error=message)

            if event and send_progress:
                self._reply(event, f"[在做了] 正在读论文：{ref.paper_id}", "reading", ref.paper_id)
            self._workflow(WorkflowEvent.SOURCE_READY, ref.paper_id)
            self._workflow(WorkflowEvent.GENERATION_STARTED, ref.paper_id)

            try:
                if not self.llm:
                    raise RuntimeError("OPENAI_API_KEY not configured or --no-openai was used")
                figures = prepare_key_figures(bundle)
                _require_renderable_source_figures(bundle, figures)
                figure_inserts = figure_placeholders(figures)
                figure_visuals, figure_visual_warnings = _describe_figures_for_prompt(self.llm, figure_inserts)
                macro_kwargs = _paper_macro_kwargs(bundle)
                markers = [marker for marker, _path, _caption in figure_inserts]
                repository_url = find_repository_url(bundle)
                generation_run = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                markdown = _generate_complete_paper_markdown(
                    self.llm,
                    build_final_user_prompt(bundle, figure_inserts, figure_visuals),
                    markers,
                    paper_id=bundle.metadata.paper_id,
                    title=bundle.metadata.title,
                    attempt_writer=lambda attempt, raw, errors: _write_generation_attempt(
                        bundle, generation_run, attempt, raw, errors
                    ),
                )
                self._workflow(WorkflowEvent.DRAFT_READY, ref.paper_id)
                _write_paper_artifact(bundle, "01-generated.md", markdown)
                markdown = _sanitize_repository_markdown(markdown, repository_url)
                markdown = polish_markdown(markdown, **macro_kwargs)
                markdown = remove_false_material_warning(markdown, bundle)
                markdown = ensure_priority_figure_markers(markdown, figure_inserts, visual_descriptions=figure_visuals)
                _write_paper_artifact(bundle, "02-polished.md", markdown)
                review_warnings = list(figure_visual_warnings)
                if event and send_progress:
                    self._reply(event, f"[审阅中] 正在审阅/修订：{ref.paper_id}", "reviewing", ref.paper_id)
                try:
                    review = review_markdown_with_report(self.llm, markdown, markers, kind="paper", reasoning_effort=self.review_reasoning_effort)
                    _write_paper_artifact(bundle, "03-review-response.txt", review.raw)
                    markdown = review.markdown
                    _write_paper_artifact(bundle, "04-reviewed.md", markdown)
                    self.store.add_review_issues("paper", ref.paper_id, review.issues)
                    for issue in review.issues:
                        review_warnings.append(f"review:{issue.category}:{issue.severity}:{issue.detail}")
                except Exception as review_exc:
                    review_warnings.append(f"Review pass failed: {review_exc}")
                self._workflow(WorkflowEvent.REVIEW_COMPLETED, ref.paper_id)
                def normalize_for_quality(candidate: str) -> str:
                    candidate = _sanitize_repository_markdown(candidate, repository_url)
                    candidate = polish_markdown(candidate, **macro_kwargs)
                    candidate = remove_false_material_warning(candidate, bundle)
                    candidate = ensure_priority_figure_markers(
                        candidate,
                        figure_inserts,
                        visual_descriptions=figure_visuals,
                    )
                    return ensure_referenced_figure_markers(
                        candidate,
                        figure_inserts,
                        visual_descriptions=figure_visuals,
                    )

                quality_result = repair_until_quality_passes(
                    self.llm,
                    markdown,
                    markers,
                    render_xml=lambda candidate: markdown_to_docx_xml(
                        candidate,
                        latex_macros=bundle.source_latex_macros,
                        latex_arg_macros=bundle.source_latex_arg_macros,
                    ),
                    normalize_markdown=normalize_for_quality,
                    max_repair_rounds=self.quality_repair_rounds,
                    kind="paper",
                    reasoning_effort=self.review_reasoning_effort,
                    completeness_check=lambda candidate: paper_markdown_completeness_errors(candidate, markers),
                    on_workflow_event=self._workflow,
                )
                _write_quality_repair_artifacts(bundle, quality_result)
                markdown = quality_result.markdown
                xml = quality_result.xml
                _write_paper_artifact(bundle, "05-final.md", markdown)
                _write_paper_artifact(bundle, "06-document.xml", xml)
                missing_markers = [marker for marker in markers if marker not in markdown]
                publish_warnings = (
                    review_warnings
                    + quality_result.repair_warnings
                    + [f"missing-marker:{marker}" for marker in missing_markers]
                )
                expected_image_count = sum(1 for marker in markers if marker in markdown)
                expected_latex_count = xml.count("<latex>")
                expected_table_count = xml.count("<table>")
                _write_paper_artifact(
                    bundle,
                    "07-quality.json",
                    json.dumps(
                        {
                            "passed": quality_result.passed,
                            "warnings": quality_result.warnings,
                            "blocking_warnings": quality_result.blocking_warnings,
                            "repair_warnings": quality_result.repair_warnings,
                            "rounds": len(quality_result.attempts) - 1,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
                publish_warnings.extend(quality_result.warnings)
                if quality_result.blocking_warnings:
                    self._workflow(WorkflowEvent.QUALITY_REJECTED, "; ".join(quality_result.blocking_warnings))
                    raise PrePublishQualityError("; ".join(quality_result.blocking_warnings))
                self._workflow(WorkflowEvent.QUALITY_PASSED, ref.paper_id)
            except PrePublishQualityError as exc:
                message = f"论文已读完，但发布前格式质检未通过，未发布文档：{exc}"
                _write_paper_artifact(bundle, "08-failure.txt", message)
                self.store.upsert_paper(ref.paper_id, "quality_failed", error=message)
                if event and send_progress:
                    self._reply(event, f"这篇已读完，但发布前格式质检未通过：{ref.paper_id}\n原因：{message}", "quality-fail", ref.paper_id)
                return ProcessResult(ref.paper_id, "", cached=False, error=message)
            except IncompleteGenerationError as exc:
                message = f"生成格式不完整，未发布文档：{exc}"
                self._workflow(WorkflowEvent.GENERATION_INCOMPLETE, message)
                _write_paper_artifact(bundle, "08-failure.txt", message)
                self.store.upsert_paper(ref.paper_id, "summary_incomplete", error=message)
                if event and send_progress:
                    self._reply(event, f"这篇我没读成：{ref.paper_id}\n原因：{message}", "summary-fail", ref.paper_id)
                return ProcessResult(ref.paper_id, "", cached=False, error=message)
            except Exception as exc:
                message = f"总结模型调用失败，未发布文档：{exc}"
                self._workflow(WorkflowEvent.FAIL, message)
                _write_paper_artifact(bundle, "08-failure.txt", message)
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
            self._workflow(
                WorkflowEvent.PUBLISH_SUCCEEDED,
                PublishedCheckpoint(
                    doc_url=doc["url"],
                    expected_title=bundle.metadata.title or ref.paper_id,
                    expected_image_min=expected_image_count,
                    expected_latex_min=expected_latex_count,
                    expected_table_min=expected_table_count,
                ).to_json(),
            )
            post_publish_warnings = verify_published_docx(
                self.feishu,
                doc["url"],
                expected_title=bundle.metadata.title or ref.paper_id,
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
                    source_id=ref.paper_id,
                    expected_image_min=expected_image_count,
                    expected_formula_min=expected_latex_count,
                    expected_table_min=expected_table_count,
                    on_workflow_event=self._workflow,
                )
                _write_visual_qa_artifact(bundle, visual_result)
                figure_warnings.extend(visual_result.warnings)
                if visual_result.changed:
                    figure_warnings.extend(
                        verify_published_docx(
                            self.feishu,
                            doc["url"],
                            expected_title=bundle.metadata.title or ref.paper_id,
                            expected_image_min=expected_image_count,
                            expected_latex_min=expected_latex_count,
                            expected_table_min=expected_table_count,
                        )
                    )
                else:
                    figure_warnings.extend(post_publish_warnings)
            else:
                figure_warnings.extend(post_publish_warnings)
            post_publish_blocking = blocking_quality_warnings(figure_warnings)
            if post_publish_blocking:
                message = "文档已生成，但发布后质检失败，暂不交付：" + "; ".join(post_publish_blocking)
                self._workflow(WorkflowEvent.QUALITY_REJECTED, message)
                self.store.upsert_paper(
                    ref.paper_id,
                    "quality_failed",
                    doc_url=doc["url"],
                    doc_token=doc["token"],
                    error=message,
                )
                if event and send_progress:
                    self._reply(event, f"这篇发布后质检未通过：{ref.paper_id}\n原因：{message}", "quality-fail", ref.paper_id)
                return ProcessResult(ref.paper_id, doc["url"], cached=False, error=message)
            self.store.upsert_paper(ref.paper_id, "done", doc_url=doc["url"], doc_token=doc["token"], error="; ".join(figure_warnings))
            self._workflow(WorkflowEvent.COMPLETE, doc["url"])
            if event and send_progress:
                self._reply(event, f"哥，读完了：{doc['url']}", "done", ref.paper_id)
            return ProcessResult(ref.paper_id, doc["url"], cached=False)
        except Exception as exc:
            error = f"{exc}\n{traceback.format_exc()}"
            self._workflow(WorkflowEvent.FAIL, _short_error(exc))
            self.store.upsert_paper(ref.paper_id, "failed", error=error)
            if event and send_progress:
                self._reply(event, f"这篇我没读成：{ref.paper_id}\n原因：{_short_error(exc)}", "fail", ref.paper_id)
            return ProcessResult(ref.paper_id, "", cached=False, error=str(exc))

    def _workflow(self, event: WorkflowEvent, detail: str = "") -> None:
        if self.on_workflow_event is not None:
            self.on_workflow_event(event, detail)

    def _resume_published_doc(self, ref: PaperRef, record, checkpoint: PublishedCheckpoint) -> ProcessResult:
        """Recheck and repair an existing published document without rerunning the LLM."""
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
                    source_id=ref.paper_id,
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
                self.store.upsert_paper(
                    ref.paper_id,
                    "quality_failed",
                    doc_url=doc_url,
                    doc_token=doc_token,
                    error=message,
                )
                return ProcessResult(ref.paper_id, doc_url, cached=False, error=message)
            self.store.upsert_paper(
                ref.paper_id,
                "done",
                doc_url=doc_url,
                doc_token=doc_token,
                error="; ".join(warnings),
            )
            self._workflow(WorkflowEvent.COMPLETE, doc_url)
            return ProcessResult(ref.paper_id, doc_url, cached=False)
        except Exception as exc:
            message = f"发布后复检失败，未重新生成文档：{_short_error(exc)}"
            try:
                self._workflow(WorkflowEvent.FAIL, message)
            except Exception:
                pass
            self.store.upsert_paper(ref.paper_id, "quality_failed", error=message)
            return ProcessResult(ref.paper_id, doc_url, cached=False, error=message)

    def _reply(self, event: FeishuEvent, text: str, prefix: str, paper_id: str) -> None:
        stage = _progress_stage(prefix)
        if stage:
            try:
                self.feishu.set_progress_reaction(event.message_id, stage)
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


def _paper_macro_kwargs(bundle: PaperBundle) -> dict:
    return {
        "custom_macros": bundle.source_macros,
        "latex_macros": bundle.source_latex_macros,
        "latex_arg_macros": bundle.source_latex_arg_macros,
    }


def _require_renderable_source_figures(bundle: PaperBundle, figures) -> None:
    if bundle.source_figures and not figures:
        raise PrePublishQualityError("quality:figure:source:high:no-renderable-source-figure")


def _write_paper_artifact(bundle: PaperBundle, name: str, content: str) -> None:
    """Persist pipeline stages without making diagnostics part of the happy path."""
    candidates = [bundle.pdf_path, bundle.source_path]
    root = next(
        (
            Path(path).parent
            for path in candidates
            if path and Path(path).is_absolute() and Path(path).parent.exists()
        ),
        None,
    )
    if root is None:
        return
    try:
        artifact_dir = root / "pipeline_artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        target = artifact_dir / Path(name).name
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(str(content or ""), encoding="utf-8")
        temporary.replace(target)
    except Exception:
        return


def _remove_paper_artifact(bundle: PaperBundle, name: str) -> None:
    candidates = [bundle.pdf_path, bundle.source_path]
    root = next(
        (
            Path(path).parent
            for path in candidates
            if path and Path(path).is_absolute() and Path(path).parent.exists()
        ),
        None,
    )
    if root is None:
        return
    try:
        (root / "pipeline_artifacts" / Path(name).name).unlink(missing_ok=True)
    except Exception:
        return


def _write_generation_attempt(bundle: PaperBundle, run_id: str, attempt: int, markdown: str, errors) -> None:
    _write_paper_artifact(bundle, f"01-{run_id}-attempt-{attempt}.md", markdown)
    _write_paper_artifact(
        bundle,
        f"01-{run_id}-attempt-{attempt}.json",
        json.dumps({"attempt": attempt, "errors": list(errors)}, ensure_ascii=False, indent=2),
    )


def _write_quality_repair_artifacts(bundle: PaperBundle, result: QualityRepairResult) -> None:
    for attempt in result.attempts:
        round_label = f"round-{attempt.round_index}"
        _write_paper_artifact(bundle, f"05-quality-{round_label}.md", attempt.markdown)
        _write_paper_artifact(bundle, f"06-quality-{round_label}.xml", attempt.xml)
        _write_paper_artifact(
            bundle,
            f"07-quality-{round_label}.json",
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
            _write_paper_artifact(
                bundle,
                f"05-quality-{round_label}-response.txt",
                attempt.model_response,
            )


def _write_visual_qa_artifact(bundle: PaperBundle, result) -> None:
    _write_paper_artifact(
        bundle,
        "09-visual-qa.json",
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
    )


def _generate_complete_paper_markdown(
    llm,
    user_prompt: str,
    markers,
    attempts: int = 2,
    paper_id: str = "",
    title: str = "",
    attempt_writer: Optional[Callable[[int, str, list[str]], None]] = None,
) -> str:
    attempt_count = max(1, attempts)
    contract = _markdown_generation_contract(paper_id)
    prompt = user_prompt + contract
    last_errors: list[str] = []
    last_markdown = ""
    last_exception = ""
    had_model_output = False
    for attempt in range(attempt_count):
        try:
            raw_markdown = llm.responses_text(FINAL_SYSTEM_PROMPT, prompt)
        except Exception as exc:
            last_exception = str(exc)
            if attempt_writer:
                attempt_writer(attempt + 1, "", [f"model-call:{last_exception}"])
            continue
        had_model_output = True
        markdown = _unwrap_outer_markdown_fence(raw_markdown)
        errors = paper_markdown_completeness_errors(markdown, markers)
        if attempt_writer:
            attempt_writer(attempt + 1, raw_markdown, errors)
        if not errors:
            return markdown
        last_markdown = markdown
        last_errors = errors
        prompt = user_prompt + contract + (
            "\n\n上一次输出未通过结构检查，问题为：" + ", ".join(errors) +
            "。请从第一行重新输出完整文档：第一行必须是 `# ` 开头的 H1；"
            "不得有前置解释、JSON、YAML 或包住全文的代码围栏；必须包含第 1 至第 7 章，"
            "并只选择 3-5 张关键图片嵌入对应论述。"
        )
    if had_model_output and last_errors == ["missing-h1"]:
        repaired = _repair_missing_h1(last_markdown, paper_id, title)
        repaired_errors = paper_markdown_completeness_errors(repaired, markers)
        if not repaired_errors:
            if attempt_writer:
                attempt_writer(attempt_count + 1, repaired, ["deterministic-repair:missing-h1"])
            return repaired
        last_errors = repaired_errors
    if not had_model_output:
        raise RuntimeError("paper generation failed: " + (last_exception or "no model output"))
    raise IncompleteGenerationError(last_errors, attempts=attempt_count)


def _markdown_generation_contract(paper_id: str) -> str:
    heading_hint = f"# [{paper_id}] ..." if paper_id else "# ..."
    return (
        "\n\n输出协议（硬约束）：\n"
        f"- 第一个非空行必须是 Markdown H1，形如 `{heading_hint}`。\n"
        "- 直接输出最终 Markdown；不要输出前置说明、JSON、YAML 或代码围栏。\n"
        "- 不要用 ```markdown``` 包住全文；正文中的 marker 必须逐字保留。"
    )


def _unwrap_outer_markdown_fence(markdown: str) -> str:
    text = str(markdown or "").strip().lstrip("\ufeff")
    match = re.fullmatch(r"```(?:markdown|md)?\s*\n(.*?)\n```", text, flags=re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip() + "\n"
    return text + ("\n" if text else "")


def _repair_missing_h1(markdown: str, paper_id: str, title: str) -> str:
    text = _unwrap_outer_markdown_fence(markdown).strip()
    lines = text.splitlines()
    in_fence = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and re.match(r"^#\s+\S", stripped):
            heading = stripped
            body = lines[:index] + lines[index + 1 :]
            return f"{heading}\n\n{chr(10).join(body).strip()}\n"
    clean_title = re.sub(r"\s+", " ", str(title or "")).strip()
    if not clean_title:
        return text + ("\n" if text else "")
    prefix = f"# [{paper_id}] {clean_title}" if paper_id else f"# {clean_title}"
    return f"{prefix}\n\n{text}\n"


def _sanitize_repository_markdown(markdown: str, repository_url: str) -> str:
    text = str(markdown or "")
    value = str(repository_url or "").strip()
    table_pattern = re.compile(r"(?m)^\|\s*仓库\s*\|[^\n]*\|\s*$")
    line_pattern = re.compile(r"(?m)^\s*仓库\s*[:：][^\n]*$")
    if value:
        text = table_pattern.sub(f"| 仓库 | {value} |", text)
        text = line_pattern.sub(f"仓库：{value}", text)
    else:
        text = table_pattern.sub("", text)
        text = line_pattern.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


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


FIGURE_VISION_SYSTEM_PROMPT = """你是论文图像审阅员。只描述图片中看得见的结构、图表类型、坐标/模块/箭头/子图关系；结合用户给的 caption，但不要凭文件名猜测。输出中文一句话，60 字以内。"""


def _describe_figures_for_prompt(llm, figure_inserts):
    if os.environ.get("MAXREAD_SKIP_FIGURE_VISION", "").lower() in {"1", "true", "yes", "on"}:
        return {}, []
    if not hasattr(llm, "responses_image_text"):
        return {}, []
    descriptions = {}
    warnings = []
    for marker, path, caption in figure_inserts[:8]:
        image_path = Path(path)
        if not image_path.exists():
            warnings.append(f"figure-vision-missing:{image_path.name}")
            continue
        try:
            prompt = (
                f"marker: {marker}\n"
                f"caption: {caption or '[无 caption]'}\n"
                "请读图并说明这张图实际展示什么；如果 caption 与图像不一致，优先指出图像内容。"
            )
            text = llm.responses_image_text(FIGURE_VISION_SYSTEM_PROMPT, prompt, image_path)
            text = " ".join(str(text or "").split())[:240]
            if text:
                descriptions[marker] = text
        except Exception as exc:
            warnings.append(f"figure-vision-failed:{image_path.name}:{_short_error(exc, 180)}")
    return descriptions, warnings
