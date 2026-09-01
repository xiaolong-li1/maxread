from __future__ import annotations

import html
import json
import os
import re
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from .arxiv import ArxivClient
from .db import Store
from .feishu import FeishuClient, doc_token_from_url
from .models import ArxivMetadata, FeishuEvent, PaperBundle, PaperRef
from .openai_client import OpenAIClient
from .prompts import FINAL_SYSTEM_PROMPT, SECTION_GENERATION_TASKS, build_final_user_prompt, build_paper_evidence_prefix, build_section_user_prompt, select_key_source_tables
from .project_metadata import extract_project_summary as _extract_project_summary, is_placeholder_project_title, one_sentence_summary as _one_sentence_summary
from .publishing import publish_marker_image
from .quality import PrePublishQualityError, blocking_quality_warnings, paper_markdown_completeness_errors, verify_published_docx
from .quality_repair import QualityRepairResult, repair_until_quality_passes
from .repository import find_repository_url
from .render import _figure_section_target, compiled_figure_captions, compose_related_figure_groups, enforce_figure_owner_sections, ensure_priority_figure_markers, ensure_referenced_figure_markers, figure_placeholders, markdown_to_docx_xml, normalize_figure_captions, polish_markdown, prepare_key_figures_with_owners, remove_false_material_warning
from .review import MethodValidationResult, ReviewIssue, audit_method_consistency_with_report, review_markdown_with_report, validate_method_consistency
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


@dataclass
class RetryContext:
    """Previous durable diagnostics used as input to a later manual retry."""

    previous_markdown: str = ""
    feedback: list[str] = field(default_factory=list)


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
        generation_repair_rounds: int = 2,
        sectional_generation_enabled: bool = False,
        sectional_generation_workers: int = 5,
        quality_repair_rounds: int = 2,
        on_workflow_event=None,
    ):
        self.store = store
        self.arxiv = arxiv
        self.feishu = feishu
        self.llm = llm
        self.require_source = require_source
        self.review_reasoning_effort = review_reasoning_effort
        self.visual_qa = visual_qa
        self.generation_repair_rounds = max(0, int(generation_repair_rounds))
        self.sectional_generation_enabled = bool(sectional_generation_enabled)
        self.sectional_generation_workers = max(1, int(sectional_generation_workers))
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
        force_rebuild: bool = False,
        editorial_guidance: str = "",
        retry_feedback: str = "",
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
            published_url = "" if force_rebuild else str(resume_published_url or "").strip()
            if not force_rebuild and not published_url and record and record.status == "quality_failed":
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
                project_summary=_one_sentence_summary(bundle.metadata.summary),
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
                        "figure_owners": [
                            {
                                "asset": figure.asset,
                                "label": figure.label,
                                "owner_section": figure.owner_section,
                                "owner_evidence": figure.owner_evidence,
                            }
                            for figure in bundle.source_figures
                            if not figure.is_appendix
                        ],
                        "tables": len(bundle.source_tables),
                        "parse_warnings": list(bundle.parse_warnings),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            retry_context = _load_retry_context(bundle)
            durable_feedback = [
                str(record.error if record else "").strip(),
                str(retry_feedback or "").strip(),
            ]
            retry_context = RetryContext(
                previous_markdown=retry_context.previous_markdown,
                feedback=_dedupe_feedback(
                    list(retry_context.feedback) + [item for item in durable_feedback if item]
                )[-16:],
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
                prepared_figures = prepare_key_figures_with_owners(bundle)
                figures = [(path, caption) for path, caption, _owner in prepared_figures]
                _require_renderable_source_figures(bundle, figures)
                figure_inserts = figure_placeholders(figures)
                figure_owners = {
                    marker: owner
                    for (marker, _path, _caption), (_prepared_path, _prepared_caption, owner) in zip(
                        figure_inserts, prepared_figures
                    )
                }
                figure_visuals, figure_visual_warnings = _describe_figures_for_prompt(self.llm, figure_inserts)
                figure_inserts, figure_visuals = compose_related_figure_groups(
                    figure_inserts,
                    figure_visuals,
                    owner_sections=figure_owners,
                )
                macro_kwargs = _paper_macro_kwargs(bundle)
                markers = [marker for marker, _path, _caption in figure_inserts]
                selected_tables = select_key_source_tables(bundle.source_tables)
                repository_url = find_repository_url(bundle)
                generation_run = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

                def generate_sectional() -> str:
                    evidence_prefix = build_paper_evidence_prefix(
                        bundle,
                        figure_inserts,
                        figure_visuals,
                        figure_owners=figure_owners,
                        editorial_guidance=editorial_guidance,
                    )
                    if retry_context.feedback:
                        evidence_prefix += (
                            "\n\n本次重试需避免的历史问题：\n"
                            + "\n".join(f"- {item}" for item in retry_context.feedback[-12:])
                        )
                    marker_assignments, table_assignments = _sectional_material_assignments(
                        figure_inserts,
                        figure_visuals,
                        selected_tables,
                        owner_sections=figure_owners,
                    )
                    return _generate_sectional_paper_markdown(
                        self.llm,
                        evidence_prefix,
                        paper_id=bundle.metadata.paper_id,
                        markers_by_section=marker_assignments,
                        tables_by_section=table_assignments,
                        attempts=self.generation_repair_rounds + 1,
                        workers=self.sectional_generation_workers,
                        artifact_writer=lambda section, attempt, raw, errors: _write_section_generation_attempt(
                            bundle, generation_run, section, attempt, raw, errors
                        ),
                        on_workflow_event=self._workflow,
                    )

                if self.sectional_generation_enabled and not retry_context.previous_markdown:
                    markdown = generate_sectional()
                else:
                    generation_prompt = build_final_user_prompt(
                        bundle,
                        figure_inserts,
                        figure_visuals,
                        figure_owners=figure_owners,
                        editorial_guidance=editorial_guidance,
                    )
                    can_fallback = self.sectional_generation_enabled and bool(retry_context.previous_markdown)
                    try:
                        markdown = _generate_complete_paper_markdown(
                            self.llm,
                            generation_prompt,
                            markers,
                            attempts=1 if can_fallback else self.generation_repair_rounds + 1,
                            paper_id=bundle.metadata.paper_id,
                            title=bundle.metadata.title,
                            prior_feedback=retry_context.feedback,
                            previous_markdown=retry_context.previous_markdown,
                            attempt_writer=lambda attempt, raw, errors: _write_generation_attempt(
                                bundle, generation_run, attempt, raw, errors
                            ),
                            on_workflow_event=self._workflow,
                        )
                    except (RuntimeError, IncompleteGenerationError) as exc:
                        if not can_fallback:
                            raise
                        _write_paper_artifact(
                            bundle,
                            f"01-{generation_run}-sectional-fallback.json",
                            json.dumps(
                                {"reason": str(exc)[:1200], "previous_draft": True},
                                ensure_ascii=False,
                                indent=2,
                            ),
                        )
                        markdown = generate_sectional()
                self._workflow(WorkflowEvent.DRAFT_READY, ref.paper_id)
                _write_paper_artifact(bundle, "01-generated.md", markdown)
                markdown = _sanitize_repository_markdown(markdown, repository_url)
                markdown = polish_markdown(markdown, **macro_kwargs)
                markdown = remove_false_material_warning(markdown, bundle)
                markdown = enforce_figure_owner_sections(markdown, figure_inserts, figure_owners)
                markdown = ensure_priority_figure_markers(
                    markdown,
                    figure_inserts,
                    visual_descriptions=figure_visuals,
                    owner_sections=figure_owners,
                )
                _write_paper_artifact(bundle, "02-polished.md", markdown)
                review_warnings = list(figure_visual_warnings)
                if event and send_progress:
                    self._reply(event, f"[审阅中] 正在审阅/修订：{ref.paper_id}", "reviewing", ref.paper_id)
                audit_source_context = _paper_method_source_context(bundle)
                try:
                    editorial_validation = _deterministic_editorial_validation(markdown, markers)
                    validation = validate_method_consistency(
                        self.llm,
                        _paper_method_markdown(markdown),
                        audit_source_context,
                        editorial_guidance,
                        self.review_reasoning_effort,
                    )
                    _write_paper_artifact(
                        bundle,
                        "03-editorial-validation.json",
                        json.dumps(
                            {
                                "passed": editorial_validation.passed,
                                "findings": [issue.__dict__ for issue in editorial_validation.issues],
                                "raw": editorial_validation.raw,
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                    )
                    _write_paper_artifact(
                        bundle,
                        "04c-method-validation.json",
                        json.dumps(
                            {
                                "passed": validation.passed,
                                "findings": [issue.__dict__ for issue in validation.issues],
                                "raw": validation.raw,
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                    )
                    editorial_changed = False
                    if not editorial_validation.passed:
                        finding_text = "\n".join(
                            f"- [{issue.category}:{issue.severity}] {issue.detail}"
                            for issue in editorial_validation.issues
                        ) or "- 交付可读性验收未通过，但未返回具体 finding"
                        review = review_markdown_with_report(
                            self.llm,
                            markdown,
                            markers,
                            kind="paper",
                            reasoning_effort=self.review_reasoning_effort,
                            source_context=audit_source_context,
                            editorial_guidance=(
                                str(editorial_guidance or "").strip()
                                + "\n\n交付可读性 findings，必须逐条做最小修复：\n"
                                + finding_text
                            ).strip(),
                        )
                        _write_paper_artifact(bundle, "03-review-response.txt", review.raw)
                        editorial_changed = review.markdown.strip() != markdown.strip()
                        markdown = review.markdown
                        _write_paper_artifact(bundle, "04-reviewed.md", markdown)
                        self.store.add_review_issues("paper", ref.paper_id, review.issues)
                        for issue in review.issues:
                            review_warnings.append(f"review:{issue.category}:{issue.severity}:{issue.detail}")
                        editorial_validation = validate_editorial_quality(
                            self.llm,
                            markdown,
                            markers,
                            reasoning_effort=self.review_reasoning_effort,
                        )
                        _write_paper_artifact(
                            bundle,
                            "04a-editorial-revalidation.json",
                            json.dumps(
                                {
                                    "passed": editorial_validation.passed,
                                    "findings": [issue.__dict__ for issue in editorial_validation.issues],
                                    "raw": editorial_validation.raw,
                                },
                                ensure_ascii=False,
                                indent=2,
                            ),
                        )
                    if not editorial_validation.passed:
                        blocking = "; ".join(
                            f"editorial:{issue.category}:{issue.severity}:{issue.detail}"
                            for issue in editorial_validation.issues
                            if issue.severity in {"medium", "high"}
                        ) or "editorial:other:high:delivery readability validation failed"
                        self._workflow(WorkflowEvent.REVIEW_COMPLETED, ref.paper_id)
                        self._workflow(WorkflowEvent.QUALITY_REJECTED, blocking)
                        raise PrePublishQualityError(blocking)
                    if editorial_changed:
                        validation = validate_method_consistency(
                            self.llm,
                            _paper_method_markdown(markdown),
                            source_context=audit_source_context,
                            editorial_guidance=editorial_guidance,
                            reasoning_effort=self.review_reasoning_effort,
                        )
                    if not validation.passed:
                        finding_text = "\n".join(
                            f"- [{issue.category}:{issue.severity}] {issue.detail}"
                            for issue in validation.issues
                        ) or "- 方法一致性验收未通过，但未返回具体 finding"
                        method_markdown = _paper_method_markdown(markdown)
                        method_markers = [marker for marker in markers if marker in method_markdown]
                        method_repair = audit_method_consistency_with_report(
                            self.llm,
                            method_markdown,
                            method_markers,
                            source_context=audit_source_context,
                            editorial_guidance=(
                                str(editorial_guidance or "").strip()
                                + "\n\n上一轮独立验收 findings，必须逐条修复：\n"
                                + finding_text
                            ).strip(),
                            reasoning_effort=self.review_reasoning_effort,
                        )
                        _write_paper_artifact(bundle, "04d-method-repair-response.txt", method_repair.raw)
                        markdown = _replace_paper_method_markdown(markdown, method_repair.markdown)
                        _write_paper_artifact(bundle, "04e-method-repaired.md", markdown)
                        validation = validate_method_consistency(
                            self.llm,
                            _paper_method_markdown(markdown),
                            source_context=audit_source_context,
                            editorial_guidance=editorial_guidance,
                            reasoning_effort=self.review_reasoning_effort,
                        )
                        _write_paper_artifact(
                            bundle,
                            "04f-method-revalidation.json",
                            json.dumps(
                                {
                                    "passed": validation.passed,
                                    "findings": [issue.__dict__ for issue in validation.issues],
                                    "raw": validation.raw,
                                },
                                ensure_ascii=False,
                                indent=2,
                            ),
                        )
                    review_warnings.extend(
                        f"method-audit:{issue.category}:{issue.severity}:{issue.detail}"
                        for issue in validation.issues
                        if issue.severity == "medium"
                    )
                    if not validation.passed:
                        blocking = "; ".join(
                            f"method-audit:{issue.category}:{issue.severity}:{issue.detail}"
                            for issue in validation.issues
                            if issue.severity == "high"
                        ) or "method-audit:other:high:method consistency validation failed"
                        # Method validation runs while the durable workflow is
                        # still in reviewing. Enter the quality gate before
                        # rejecting; jumping directly to QUALITY_REJECTED is
                        # an invalid transition and would be mistaken for an
                        # optional audit failure by the catch block below.
                        self._workflow(WorkflowEvent.REVIEW_COMPLETED, ref.paper_id)
                        self._workflow(WorkflowEvent.QUALITY_REJECTED, blocking)
                        raise PrePublishQualityError(blocking)
                except Exception as review_exc:
                    if isinstance(review_exc, PrePublishQualityError):
                        raise
                    review_warnings.append(f"Parallel delivery validation failed: {review_exc}")
                self._workflow(WorkflowEvent.REVIEW_COMPLETED, ref.paper_id)
                def normalize_for_quality(candidate: str) -> str:
                    candidate = _sanitize_repository_markdown(candidate, repository_url)
                    candidate = polish_markdown(candidate, **macro_kwargs)
                    candidate = remove_false_material_warning(candidate, bundle)
                    candidate = enforce_figure_owner_sections(
                        candidate, figure_inserts, figure_owners
                    )
                    candidate = ensure_priority_figure_markers(
                        candidate,
                        figure_inserts,
                        visual_descriptions=figure_visuals,
                        owner_sections=figure_owners,
                    )
                    candidate = ensure_referenced_figure_markers(
                        candidate,
                        figure_inserts,
                        visual_descriptions=figure_visuals,
                        owner_sections=figure_owners,
                    )
                    return normalize_figure_captions(
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
                    prior_feedback=retry_context.feedback,
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
                            "rounds": sum(1 for attempt in quality_result.attempts if attempt.model_response),
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
                self.store.upsert_paper(
                    ref.paper_id,
                    "quality_failed",
                    project_summary=_extract_project_summary(markdown, bundle.metadata.summary),
                    error=message,
                )
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

            self.store.upsert_paper(
                ref.paper_id,
                "writing_doc",
                title=_extract_generated_paper_title(markdown, bundle.metadata.title, ref.paper_id),
                project_summary=_extract_project_summary(markdown, bundle.metadata.summary),
            )
            if event and send_progress:
                self._reply(event, f"[敲键盘] 在写飞书文档：{ref.paper_id}", "writing", ref.paper_id)
            refresh_existing = bool(
                record
                and record.status in {"legacy", "cache_expired"}
                and record.doc_url
            )
            if refresh_existing:
                doc = {
                    "url": record.doc_url,
                    "token": record.doc_token or doc_token_from_url(record.doc_url),
                }
                try:
                    self.feishu.overwrite_docx_xml(doc["url"], xml)
                except Exception:
                    # A deleted or inaccessible legacy document cannot be
                    # refreshed in place. Creating a replacement is the only
                    # fallback that still delivers a usable document.
                    doc = self.feishu.create_docx(bundle.metadata.title or ref.paper_id)
                    self.feishu.overwrite_docx_xml(doc["url"], xml)
            else:
                doc = self.feishu.create_docx(bundle.metadata.title or ref.paper_id)
                self.feishu.overwrite_docx_xml(doc["url"], xml)
            figure_warnings = list(publish_warnings)
            native_captions = compiled_figure_captions(markdown)
            for marker, image_path, caption in figure_inserts:
                if marker not in markdown:
                    continue
                publish_result = publish_marker_image(
                    self.feishu,
                    doc["url"],
                    image_path,
                    native_captions.get(marker, caption),
                    marker,
                )
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
                message = _post_publish_failure_message(post_publish_blocking)
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
            self.store.upsert_paper(
                ref.paper_id,
                "done",
                title=_extract_generated_paper_title(markdown, bundle.metadata.title, ref.paper_id),
                doc_url=doc["url"],
                doc_token=doc["token"],
                error="; ".join(figure_warnings),
            )
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
        resolved_title = str(record.title if record else "")
        if is_placeholder_project_title(resolved_title, ref.paper_id):
            try:
                fetched = self.feishu.fetch_docx(doc_url, doc_format="xml", detail="simple")
                resolved_title = _published_document_title_from_payload(fetched) or resolved_title
            except Exception:
                pass
        expected_title = checkpoint.expected_title or resolved_title
        try:
            self._workflow(WorkflowEvent.RESUME_PUBLISHED, doc_url)
            initial_warnings = list(
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
                    initial_warnings=initial_warnings,
                    source_id=ref.paper_id,
                    expected_image_min=checkpoint.expected_image_min,
                    expected_formula_min=checkpoint.expected_latex_min,
                    expected_table_min=checkpoint.expected_table_min,
                    previous_feedback=[record.error] if record and record.error else (),
                    on_workflow_event=self._workflow,
                )
                if visual_result.changed:
                    warnings = list(visual_result.warnings)
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
                else:
                    warnings = initial_warnings + list(visual_result.warnings)
            else:
                warnings = initial_warnings
            blocking = blocking_quality_warnings(warnings)
            if blocking:
                message = _post_publish_failure_message(blocking)
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
                title=resolved_title,
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
    body_figures = [figure for figure in bundle.source_figures if not getattr(figure, "is_appendix", False)]
    if body_figures and not figures:
        formats = sorted({Path(figure.asset).suffix.lower() or "(none)" for figure in body_figures})
        detail = ",".join(formats) or "unknown"
        raise PrePublishQualityError(f"quality:figure:source:high:no-renderable-source-figure:formats={detail}")


def _write_paper_artifact(bundle: PaperBundle, name: str, content: str) -> None:
    """Persist pipeline stages without making diagnostics part of the happy path."""
    artifact_dir = _paper_artifact_dir(bundle)
    if artifact_dir is None:
        return
    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        target = artifact_dir / Path(name).name
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(str(content or ""), encoding="utf-8")
        temporary.replace(target)
    except Exception:
        return


def _remove_paper_artifact(bundle: PaperBundle, name: str) -> None:
    artifact_dir = _paper_artifact_dir(bundle)
    if artifact_dir is None:
        return
    try:
        (artifact_dir / Path(name).name).unlink(missing_ok=True)
    except Exception:
        return


def _paper_artifact_dir(bundle: PaperBundle) -> Optional[Path]:
    for path in (bundle.pdf_path, bundle.source_path):
        if path and Path(path).is_absolute() and Path(path).parent.exists():
            return Path(path).parent / "pipeline_artifacts"
    return None


def _load_retry_context(bundle: PaperBundle, max_feedback: int = 16) -> RetryContext:
    """Load the last failed draft and compact diagnostics from durable artifacts."""
    artifact_dir = _paper_artifact_dir(bundle)
    if artifact_dir is None or not artifact_dir.exists():
        return RetryContext()

    feedback: list[str] = []
    markdown_candidates: list[Path] = []

    for report in sorted(artifact_dir.glob("01-*-attempt-*.json"))[-12:]:
        payload = _read_json_artifact(report)
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if isinstance(errors, list) and errors:
            label = report.stem.removesuffix(".json")
            feedback.append(f"{label}: {', '.join(str(item) for item in errors)}")
        markdown = report.with_suffix(".md")
        if markdown.exists():
            markdown_candidates.append(markdown)

    for report in sorted(artifact_dir.glob("07-quality-round-*.json"))[-8:]:
        payload = _read_json_artifact(report)
        if not isinstance(payload, dict):
            continue
        blocking = payload.get("blocking_warnings")
        if isinstance(blocking, list) and blocking:
            feedback.append(f"{report.stem}: {', '.join(str(item) for item in blocking)}")

    visual = _read_json_artifact(artifact_dir / "09-visual-qa.json")
    if isinstance(visual, dict):
        for item in list(visual.get("rounds") or [])[-4:]:
            if not isinstance(item, dict):
                continue
            details = []
            for finding in item.get("findings") or []:
                if not isinstance(finding, dict):
                    continue
                detail = str(finding.get("detail") or finding.get("kind") or "").strip()
                section = str(finding.get("section") or "").strip()
                details.append(f"{detail} [section={section}]" if section else detail)
            if details:
                feedback.append(f"visual-round-{item.get('round', '?')}: {'; '.join(details)}")

    failure_path = artifact_dir / "08-failure.txt"
    if failure_path.exists():
        try:
            failure = failure_path.read_text(encoding="utf-8").strip()
            if failure:
                feedback.append(f"last-job: {failure}")
        except OSError:
            pass

    preferred = [artifact_dir / "05-final.md"]
    preferred.extend(sorted(artifact_dir.glob("05-quality-round-*.md"), reverse=True))
    # A worker can die after generation has produced a complete draft but
    # before review or publication records a database checkpoint. Resume from
    # the complete durable draft; an individual sectional attempt is not a
    # valid whole-document retry input.
    preferred.extend([artifact_dir / "02-polished.md", artifact_dir / "01-generated.md"])
    preferred.extend(reversed(markdown_candidates))
    previous_markdown = ""
    for candidate in preferred:
        try:
            text = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            previous_markdown = text + "\n"
            break

    return RetryContext(
        previous_markdown=previous_markdown,
        feedback=_dedupe_feedback(feedback)[-max(1, int(max_feedback)):],
    )


def _read_json_artifact(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}


def _dedupe_feedback(values: Iterable[str]) -> list[str]:
    output: list[str] = []
    seen = set()
    for value in values:
        text = " ".join(str(value or "").split())[:700]
        if text and text not in seen:
            seen.add(text)
            output.append(text)
    return output


def _write_generation_attempt(bundle: PaperBundle, run_id: str, attempt: int, markdown: str, errors) -> None:
    _write_paper_artifact(bundle, f"01-{run_id}-attempt-{attempt}.md", markdown)
    _write_paper_artifact(
        bundle,
        f"01-{run_id}-attempt-{attempt}.json",
        json.dumps({"attempt": attempt, "errors": list(errors)}, ensure_ascii=False, indent=2),
    )


def _write_section_generation_attempt(
    bundle: PaperBundle,
    run_id: str,
    section: str,
    attempt: int,
    markdown: str,
    errors,
) -> None:
    safe_section = re.sub(r"[^a-z0-9_-]+", "-", str(section).lower()).strip("-") or "section"
    _write_paper_artifact(bundle, f"01-{run_id}-{safe_section}-attempt-{attempt}.md", markdown)
    _write_paper_artifact(
        bundle,
        f"01-{run_id}-{safe_section}-attempt-{attempt}.json",
        json.dumps(
            {"section": section, "attempt": attempt, "errors": list(errors)},
            ensure_ascii=False,
            indent=2,
        ),
    )


def _sectional_material_assignments(
    figure_inserts,
    figure_visuals,
    source_tables,
    owner_sections=None,
):
    markers: Dict[str, List[str]] = {key: [] for key in SECTION_GENERATION_TASKS}
    tables: Dict[str, List[int]] = {key: [] for key in SECTION_GENERATION_TASKS}
    for marker, path, caption in figure_inserts:
        target = str((owner_sections or {}).get(marker) or "")
        if target not in {"method", "experiments", "analysis"}:
            target = _figure_section_target(path, caption, figure_visuals.get(marker, ""))
        section = {"experiments": "experiments", "analysis": "ablation", "method": "method"}.get(target, "method")
        markers[section].append(marker)
    for index, table in enumerate(source_tables, start=1):
        text = str(table or "").lower()
        if any(word in text for word in ("ablation", "sensitivity", "ratio", "stride", "hyperparameter", "w/o", "without")):
            section = "ablation"
        elif any(word in text for word in ("notation", "algorithm", "complexity", "module", "component", "architecture")):
            section = "method"
        else:
            section = "experiments"
        tables[section].append(index)
    return markers, tables


def _generate_sectional_paper_markdown(
    llm,
    evidence_prefix: str,
    paper_id: str,
    markers_by_section: Dict[str, List[str]],
    tables_by_section: Dict[str, List[int]],
    attempts: int = 3,
    workers: int = 4,
    artifact_writer=None,
    on_workflow_event=None,
) -> str:
    section_order = list(SECTION_GENERATION_TASKS)
    attempt_serials = {section: 0 for section in section_order}

    def generate(section: str, merge_feedback: Optional[List[str]] = None, previous_output: str = "") -> str:
        base_prompt = build_section_user_prompt(
            evidence_prefix,
            section,
            paper_id,
            markers=markers_by_section.get(section, []),
            table_ids=tables_by_section.get(section, []),
        )
        if merge_feedback:
            base_prompt += (
                "\n\n合并级返修要求（优先处理）：\n"
                + "\n".join(f"- {item}" for item in merge_feedback)
                + "\n- 下面是本章上一版，只能作为返修输入，不得原样重复其中与其他章节冲突的表格：\n"
                + "```markdown\n"
                + previous_output.strip()
                + "\n```"
            )
        prompt = base_prompt
        last_errors: List[str] = []
        for attempt in range(1, max(1, int(attempts)) + 1):
            try:
                raw = llm.responses_text(
                    FINAL_SYSTEM_PROMPT,
                    prompt,
                    reasoning_effort=None if section == "method" else "medium",
                )
            except Exception as exc:
                last_errors = [f"model-call:{str(exc)[:900]}"]
                if artifact_writer:
                    attempt_serials[section] += 1
                    artifact_writer(section, attempt_serials[section], "", last_errors)
                prompt = (
                    base_prompt
                    + "\n\n上一轮本章模型调用失败：\n"
                    + f"- {last_errors[0]}\n"
                    + "请重新生成本章完整 Markdown。"
                )
                continue
            markdown = _extract_section_output(_unwrap_outer_markdown_fence(raw), section)
            errors = _section_output_errors(
                markdown,
                section,
                markers_by_section.get(section, []),
                tables_by_section.get(section, []),
            )
            if artifact_writer:
                attempt_serials[section] += 1
                artifact_writer(section, attempt_serials[section], raw, errors)
            if not errors:
                return markdown
            last_errors = errors
            prompt = (
                base_prompt
                + "\n\n上一轮本章输出未通过，精确错误如下：\n"
                + "\n".join(f"- {error}" for error in errors)
                + "\n\n上一轮输出：\n```markdown\n"
                + markdown.strip()
                + "\n```\n\n请只重写本章完整 Markdown，并在输出前逐项检查上述错误。"
            )
        raise IncompleteGenerationError([f"section-{section}:{error}" for error in last_errors], attempts=max(1, int(attempts)))

    # Every section is independent and starts with the same evidence bytes.
    # Launch all five together; provider-side prefix caching can still reuse
    # the shared prefix without adding a serial warm-up to the critical path.
    outputs: Dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(max(1, int(workers)), len(section_order))) as executor:
        futures = {executor.submit(generate, section): section for section in section_order}
        for future in as_completed(futures):
            section = futures[future]
            outputs[section] = future.result()

    # A section can independently pass while still repeating a table invented
    # by another section. Repair only the conflicting section instead of
    # discarding all successful calls or rerunning the whole paper.
    for _round in range(max(0, int(attempts) - 1)):
        duplicate_sections = _duplicate_markdown_table_sections(outputs, section_order)
        if not duplicate_sections:
            break
        for section in section_order:
            if section not in duplicate_sections:
                continue
            previous = outputs[section]
            outputs[section] = generate(
                section,
                merge_feedback=[
                    "本章存在与其他章节完全相同的 Markdown 表格。",
                    "保留本章指定的 source 表；其他重复内容改成文字回指，不得再次制表。",
                ],
                previous_output=previous,
            )

    if on_workflow_event:
        on_workflow_event(WorkflowEvent.GENERATION_CHECK_STARTED, "sectional-generation")
    markdown = "\n\n".join(outputs[key].strip() for key in section_order).strip() + "\n"
    all_markers = [marker for values in markers_by_section.values() for marker in values]
    all_tables = [table_id for values in tables_by_section.values() for table_id in values]
    errors = paper_markdown_completeness_errors(markdown, all_markers)
    errors.extend(_global_sectional_uniqueness_errors(markdown, all_markers, all_tables))
    if errors:
        raise IncompleteGenerationError(errors, attempts=max(1, int(attempts)))
    markdown = re.sub(r"(?m)^\s*\[MaxReadTable:\d+\]\s*$\n?", "", markdown)
    return re.sub(r"\n{3,}", "\n\n", markdown).strip() + "\n"


def _extract_section_output(markdown: str, section: str) -> str:
    text = str(markdown or "").strip()
    expected = {
        "front": {1, 2},
        "method": {3},
        "experiments": {4},
        "ablation": {5},
        "closing": {6, 7},
    }[section]
    if section == "front":
        # Providers occasionally prepend a short explanation and concatenate
        # the requested heading onto the same line. Use the final H1 candidate
        # so an explanatory mention of the contract cannot become the document.
        starts = list(re.finditer(r"(?<!#)#(?!#)\s+", text))
    else:
        first = min(expected)
        # The actual section is normally the final occurrence: preambles often
        # quote the expected heading before emitting it, sometimes without a
        # newline after the quote or an ellipsis.
        starts = list(re.finditer(rf"(?<!#)##(?!#)\s+{first}(?:\.|\s)", text))
    if not starts:
        return text
    start = starts[-1]
    sliced = text[start.start():]
    for match in re.finditer(r"(?m)^##\s+([1-7])(?:\.|\s)", sliced):
        number = int(match.group(1))
        if match.start() > 0 and number not in expected:
            sliced = sliced[:match.start()]
            break
    return sliced.strip() + "\n"


def _section_output_errors(markdown: str, section: str, markers: List[str], table_ids: List[int]) -> List[str]:
    text = str(markdown or "")
    expected = {
        "front": {1, 2},
        "method": {3},
        "experiments": {4},
        "ablation": {5},
        "closing": {6, 7},
    }[section]
    found = [int(value) for value in re.findall(r"(?m)^##\s+([1-7])(?:\.|\s)", text)]
    errors: List[str] = []
    if set(found) != expected or len(found) != len(expected):
        errors.append(f"headings expected={sorted(expected)} found={found}")
    if re.search(r"(?m)^#{2,6}\s+(?:\d+(?:\.\d+)*\.)?#(?:\s|\d)", text):
        errors.append("malformed nested heading")
    if section == "front":
        if len(re.findall(r"(?m)^#\s+", text)) != 1:
            errors.append("front requires exactly one H1")
        if "TL;DR" not in text:
            errors.append("front missing TL;DR")
    narrative_length = _section_narrative_length(text)
    minimum = {"front": 600, "method": 1200, "experiments": 450, "ablation": 450, "closing": 300}[section]
    if section in {"experiments", "ablation"} and (markers or table_ids):
        minimum = 250
    if narrative_length < minimum:
        errors.append(f"section narrative too short:{narrative_length}<{minimum}")
    allowed_markers = set(markers)
    found_markers = re.findall(r"\[MaxReadFigure:[^\]]+\]", text)
    for marker in markers:
        if found_markers.count(marker) != 1:
            errors.append(f"figure marker count {marker}={found_markers.count(marker)} expected=1")
    for marker in found_markers:
        if marker not in allowed_markers:
            errors.append(f"unassigned figure marker:{marker}")
    allowed_tables = {f"[MaxReadTable:{table_id}]" for table_id in table_ids}
    found_tables = re.findall(r"\[MaxReadTable:\d+\]", text)
    for marker in allowed_tables:
        if found_tables.count(marker) != 1:
            errors.append(f"table marker count {marker}={found_tables.count(marker)} expected=1")
    for marker in found_tables:
        if marker not in allowed_tables:
            errors.append(f"unassigned table marker:{marker}")
    return errors


def _section_narrative_length(markdown: str) -> int:
    """Measure explanatory prose without charging source tables or markers."""
    kept: List[str] = []
    in_table = False
    for line in str(markdown or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("|"):
            in_table = True
            continue
        if in_table and not stripped:
            in_table = False
            continue
        if in_table:
            continue
        if re.fullmatch(r"\[MaxRead(?:Figure:[^\]]+|Table:\d+)\]", stripped):
            continue
        kept.append(stripped)
    return len("".join(kept))


def _global_sectional_uniqueness_errors(markdown: str, markers: List[str], table_ids: List[int]) -> List[str]:
    errors: List[str] = []
    for marker in markers:
        count = markdown.count(marker)
        if count != 1:
            errors.append(f"global figure marker count {marker}={count}")
    for table_id in table_ids:
        marker = f"[MaxReadTable:{table_id}]"
        count = markdown.count(marker)
        if count != 1:
            errors.append(f"global table marker count {marker}={count}")
    hashes: Dict[str, int] = {}
    lines = markdown.splitlines()
    index = 0
    while index < len(lines):
        if not lines[index].lstrip().startswith("|"):
            index += 1
            continue
        block = []
        while index < len(lines) and lines[index].lstrip().startswith("|"):
            block.append(re.sub(r"\s+", "", lines[index]).lower())
            index += 1
        if len(block) >= 2:
            digest = sha256("\n".join(block).encode("utf-8")).hexdigest()
            hashes[digest] = hashes.get(digest, 0) + 1
    if any(count > 1 for count in hashes.values()):
        errors.append("duplicate markdown table content across sections")
    return errors


def _duplicate_markdown_table_sections(outputs: Dict[str, str], section_order: List[str]) -> set[str]:
    """Return only sections that should be regenerated for duplicate tables.

    A table immediately preceded by a MaxReadTable marker owns source evidence,
    so it wins over an unmarked summary copy. Otherwise the later section is
    repaired, preserving deterministic document order.
    """
    seen: Dict[str, tuple[str, bool]] = {}
    offenders: set[str] = set()
    for section in section_order:
        lines = str(outputs.get(section, "") or "").splitlines()
        index = 0
        while index < len(lines):
            if not lines[index].lstrip().startswith("|"):
                index += 1
                continue
            block = []
            while index < len(lines) and lines[index].lstrip().startswith("|"):
                block.append(re.sub(r"\s+", "", lines[index]).lower())
                index += 1
            if len(block) < 2:
                continue
            previous_index = index - len(block) - 1
            while previous_index >= 0 and not lines[previous_index].strip():
                previous_index -= 1
            protected = bool(
                previous_index >= 0
                and re.fullmatch(r"\[MaxReadTable:\d+\]", lines[previous_index].strip())
            )
            digest = sha256("\n".join(block).encode("utf-8")).hexdigest()
            prior = seen.get(digest)
            if prior is None:
                seen[digest] = (section, protected)
                continue
            prior_section, prior_protected = prior
            if protected and not prior_protected:
                offenders.add(prior_section)
                seen[digest] = (section, True)
            else:
                offenders.add(section)
    return offenders


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
    attempts: int = 3,
    paper_id: str = "",
    title: str = "",
    prior_feedback: Optional[Iterable[str]] = None,
    previous_markdown: str = "",
    attempt_writer: Optional[Callable[[int, str, list[str]], None]] = None,
    on_workflow_event: Optional[Callable[[WorkflowEvent, str], None]] = None,
) -> str:
    attempt_count = max(1, attempts)
    contract = _markdown_generation_contract(paper_id)
    prompt = user_prompt + contract
    failure_history = _dedupe_feedback(prior_feedback or [])
    if str(previous_markdown or "").strip():
        prompt = _generation_repair_prompt(
            user_prompt,
            contract,
            previous_markdown,
            ["retry-from-previous-failed-draft"],
            0,
            history=failure_history,
        )
    last_errors: list[str] = []
    last_markdown = ""
    last_exception = ""
    had_model_output = False
    checking_started = False
    for attempt in range(attempt_count):
        attempt_number = attempt + 1
        try:
            raw_markdown = llm.responses_text(FINAL_SYSTEM_PROMPT, prompt)
        except Exception as exc:
            last_exception = str(exc)
            if attempt_writer:
                attempt_writer(attempt_number, "", [f"model-call:{last_exception}"])
            continue
        had_model_output = True
        if on_workflow_event:
            if checking_started:
                on_workflow_event(
                    WorkflowEvent.GENERATION_RECHECK,
                    f"attempt={attempt_number}/{attempt_count}",
                )
            else:
                on_workflow_event(
                    WorkflowEvent.GENERATION_CHECK_STARTED,
                    f"attempt={attempt_number}/{attempt_count}",
                )
                checking_started = True
        markdown = _unwrap_outer_markdown_fence(raw_markdown)
        errors = paper_markdown_completeness_errors(markdown, markers)
        if attempt_writer:
            attempt_writer(attempt_number, raw_markdown, errors)
        if not errors:
            return markdown
        repaired, repair_kind = _deterministic_generation_repair(
            markdown,
            errors,
            markers,
            paper_id,
            title,
        )
        if repaired:
            if on_workflow_event:
                on_workflow_event(
                    WorkflowEvent.GENERATION_REPAIR_REQUIRED,
                    f"attempt={attempt_number}/{attempt_count} deterministic={repair_kind}",
                )
            if attempt_writer:
                attempt_writer(
                    attempt_count + attempt_number,
                    repaired,
                    [f"deterministic-repair:{repair_kind}"],
                )
            if on_workflow_event:
                on_workflow_event(
                    WorkflowEvent.GENERATION_RECHECK,
                    f"attempt={attempt_number}/{attempt_count} deterministic={repair_kind}",
                )
            return repaired
        last_markdown = markdown
        last_errors = errors
        current_feedback = _generation_feedback(errors, attempt_number)
        if attempt_number < attempt_count:
            if on_workflow_event:
                on_workflow_event(
                    WorkflowEvent.GENERATION_REPAIR_REQUIRED,
                    f"attempt={attempt_number}/{attempt_count} errors={','.join(errors)}",
                )
            prompt = _generation_repair_prompt(
                user_prompt,
                contract,
                markdown,
                errors,
                attempt_number,
                history=failure_history,
            )
        failure_history.extend(item for item in current_feedback if item not in failure_history)
    if not had_model_output:
        raise RuntimeError("paper generation failed: " + (last_exception or "no model output"))
    raise IncompleteGenerationError(last_errors, attempts=attempt_count)


def _deterministic_generation_repair(
    markdown: str,
    errors,
    markers,
    paper_id: str,
    title: str,
) -> tuple[str, str]:
    repaired = _extract_complete_document_suffix(markdown, markers, paper_id)
    if repaired:
        return repaired, "complete-document-suffix"
    if list(errors) == ["missing-h1"]:
        repaired = _repair_missing_h1(markdown, paper_id, title)
        if not paper_markdown_completeness_errors(repaired, markers):
            return repaired, "missing-h1"
    return "", ""


def _generation_repair_prompt(
    user_prompt: str,
    contract: str,
    previous_markdown: str,
    errors,
    repair_round: int,
    history: Optional[Iterable[str]] = None,
) -> str:
    current_feedback = "\n".join(f"  - {item}" for item in _generation_feedback(errors, repair_round))
    history_feedback = "\n".join(f"  - {item}" for item in _dedupe_feedback(history or [])) or "  - 无"
    return (
        user_prompt
        + contract
        + "\n\n生成修复任务（第 "
        + str(repair_round)
        + " 轮输出未通过）：\n"
        + "- 精确错误：本轮未通过项与验收要求如下。\n"
        + current_feedback
        + "\n- 历史失败账本（这些错误在更早尝试中出现过，不得再次引入）：\n"
        + history_feedback
        + "\n- 下面给出上一份完整输出。以它为基线逐项修复，不要从头另写，不要只输出补丁。\n"
        + "- 输出前逐项自检本轮错误和历史失败账本；保留上一稿已经正确的章节、公式、表格和图片位置。\n"
        + "- 第一行必须是 `# ` 开头的 H1；不得有前置解释、JSON、YAML 或包住全文的代码围栏。\n"
        + "- 必须保留第 1 至第 7 章和已有的 MaxReadFigure 标记。不要复述本修复指令或分隔线。\n"
        + "- 方法节不能被压缩成摘要：保留任务设定、模块输入/操作/输出、模块间承接、公式解释，以及论文提供的训练/推理流程。\n"
        + "- 修复方法事实时只补理解核心结论所需的前提；不要新增符号账本、作用域矩阵或机械的同组/跨组穷举。\n"
        + "- 实验与消融只保留上一稿已经选择的关键表和结论；不得补回未选择的 source/附录表。\n"
        + "\n----- BEGIN PREVIOUS OUTPUT -----\n"
        + previous_markdown.strip()
        + "\n----- END PREVIOUS OUTPUT -----\n"
    )


def _generation_feedback(errors: Iterable[str], attempt_number: int) -> list[str]:
    descriptions = {
        "missing-h1": "第一个非空行不是 H1；最终输出必须直接以 `# ` 标题开始",
        "leading-code-fence": "全文被 Markdown 代码围栏包裹；必须删除最外层围栏",
        "prompt-leak": "正文泄露了模型的思考或指令复述；必须删除这些前置解释",
        "duplicate-h1": "存在重复 H1 或重复完整文档；只保留一份最终文档",
        "missing-tldr": "缺少 TL;DR；必须在标题后保留清晰的一句话摘要",
        "retry-from-previous-failed-draft": "这是上一任务留下的失败稿；以历史失败账本为验收清单完成修复",
    }
    output = []
    for error in errors:
        code = str(error or "").strip()
        if code.startswith("missing-section-"):
            number = code.rsplit("-", 1)[-1]
            detail = f"缺少第 {number} 章二级标题及正文；必须补齐该章，不能只放空标题"
        elif code.startswith("too-few-figures:"):
            detail = f"关键图 marker 数量不足（{code.split(':', 1)[-1]}）；按方法、实验、分析位置恢复要求数量"
        else:
            detail = descriptions.get(code, code)
        output.append(f"attempt {attempt_number}: [{code}] {detail}")
    return output


def _markdown_generation_contract(paper_id: str) -> str:
    heading_hint = f"# [{paper_id}] ..." if paper_id else "# ..."
    return (
        "\n\n输出协议（硬约束）：\n"
        f"- 第一个非空行必须是 Markdown H1，形如 `{heading_hint}`。\n"
        "- 直接输出最终 Markdown；不要输出前置说明、JSON、YAML 或代码围栏。\n"
        "- 不要用 ```markdown``` 包住全文；正文中的 marker 必须逐字保留。\n"
        "- 方法节必须保留上下文和因果链：先写任务设定，再按真实方法子节展开模块输入/操作/输出，公式前后解释其作用，最后交代训练/推理或端到端流转（以 source 中实际存在的内容为准）。\n"
        "- 方法以讲清任务设定、核心动作、数据流和输出为准；符号紧贴公式解释，不生成符号账本、作用域矩阵或机械边界证明。\n"
        "- 分组/reset 等前提只有在它决定核心结论时才用一两句说明。\n"
        "- 实验与消融只使用证据包中被选择的关键表；不要补回其他 source/附录表，也不要逐行复述表格。\n"
        "- 如果上一轮因为格式错误重试，修复格式时不得顺手删掉方法子节、公式解释、模块承接句或端到端例子。"
    )


def _paper_review_source_context(bundle: PaperBundle, max_chars: int = 190000) -> str:
    """Give the semantic reviewer enough primary evidence to audit methods."""
    source = str(bundle.source_text or "")
    if len(source) > 90000:
        source = source[:70000] + "\n\n[... source middle omitted ...]\n\n" + source[-20000:]
    tables = "\n\n".join(select_key_source_tables(bundle.source_tables))
    sections = [
        f"Title: {bundle.metadata.title}",
        f"Abstract: {bundle.metadata.summary}",
        "TeX/source excerpt:\n" + source,
        "Figure captions:\n" + "\n".join(bundle.source_captions or []),
        "Tables:\n" + tables,
    ]
    text = "\n\n".join(item for item in sections if item.strip())
    return text[: max(1000, int(max_chars))]


def _extract_generated_paper_title(markdown: str, fallback: str, paper_id: str) -> str:
    if not is_placeholder_project_title(fallback, paper_id):
        return str(fallback).strip()
    match = re.search(r"(?mi)^\s*\*\*英文标题\*\*\s*[：:]\s*(.+?)\s*$", str(markdown or ""))
    if match:
        title = _clean_paper_title(match.group(1), paper_id)
        if title:
            return title
    heading = re.search(r"(?m)^#\s+(.+?)\s*$", str(markdown or ""))
    if heading:
        title = _clean_paper_title(heading.group(1), paper_id)
        if title:
            return title
    return str(fallback or "").strip()


def _published_document_title_from_payload(payload) -> str:
    content = _nested_document_content(payload)
    if not content:
        return ""
    english = re.search(
        r"(?is)(?:<b[^>]*>)?英文标题(?:</b>)?\s*[：:]\s*(.+?)(?:<br\s*/?>|</p>)",
        content,
    )
    if english:
        title = _clean_paper_title(re.sub(r"<[^>]+>", "", english.group(1)), "")
        if title:
            return title
    document_title = re.search(r"(?is)<title(?:\s[^>]*)?>(.*?)</title>", content)
    return _clean_paper_title(re.sub(r"<[^>]+>", "", document_title.group(1)), "") if document_title else ""


def _nested_document_content(value) -> str:
    if isinstance(value, dict):
        for item in value.values():
            content = _nested_document_content(item)
            if content:
                return content
    elif isinstance(value, list):
        for item in value:
            content = _nested_document_content(item)
            if content:
                return content
    elif isinstance(value, str) and "<title" in value:
        return value
    return ""


def _clean_paper_title(value: str, paper_id: str) -> str:
    title = html.unescape(str(value or ""))
    title = re.sub(r"^\s*\[[^\]]+\]\s*", "", title)
    title = re.sub(r"[*_`#]", "", title)
    title = re.sub(r"\s+", " ", title).strip(" ：:")
    if is_placeholder_project_title(title, paper_id):
        return ""
    return title[:500]


def _paper_method_markdown(markdown: str) -> str:
    match = re.search(
        r"(?ms)^##\s+3(?:[.、]|\s).*?(?=^##\s+4(?:[.、]|\s)|\Z)",
        str(markdown or ""),
    )
    return match.group(0).strip() if match else str(markdown or "").strip()


def _replace_paper_method_markdown(markdown: str, method_markdown: str) -> str:
    """Replace only section 3 after a source-aware method repair."""
    text = str(markdown or "")
    replacement = str(method_markdown or "").strip()
    if not re.match(r"^##\s+3(?:[.、]|\s)", replacement):
        return text
    match = re.search(
        r"(?ms)^##\s+3(?:[.、]|\s).*?(?=^##\s+4(?:[.、]|\s)|\Z)",
        text,
    )
    if not match:
        return text
    return (text[: match.start()] + replacement + "\n\n" + text[match.end() :].lstrip()).strip() + "\n"


def _paper_method_source_context(bundle: PaperBundle, max_chars: int = 45000) -> str:
    source = str(bundle.source_text or "")
    start = re.search(
        r"\\(?:section|chapter)\*?\s*\{[^}]*?(?:method|approach|architecture|model|framework)",
        source,
        flags=re.I,
    )
    if start:
        tail = source[start.start():]
        end = re.search(
            r"\\(?:section|chapter)\*?\s*\{[^}]*?(?:experiment|evaluation|result)",
            tail,
            flags=re.I,
        )
        excerpt = tail[: end.start()] if end and end.start() > 0 else tail
    else:
        excerpt = source
    excerpt = excerpt[: max(4000, int(max_chars))]
    captions = [
        figure.caption for figure in bundle.source_figures
        if figure.caption and not getattr(figure, "is_appendix", False)
    ][:20]
    return (
        f"Title: {bundle.metadata.title}\n"
        f"Abstract: {bundle.metadata.summary}\n\n"
        "Method source excerpt:\n"
        + (excerpt or "[无 method source evidence]")
        + "\n\nBody figure captions:\n"
        + ("\n".join(captions) or "[无]")
    )


def _deterministic_editorial_validation(markdown: str, markers: Iterable[str]) -> MethodValidationResult:
    marker_list = list(markers)
    errors = list(paper_markdown_completeness_errors(markdown, marker_list))
    errors.extend(_global_sectional_uniqueness_errors(markdown, marker_list, []))
    h1_count = len(re.findall(r"(?m)^#\s+", str(markdown or "")))
    if h1_count != 1:
        errors.append(f"h1-count:{h1_count}")
    if re.search(r"(?m)^#{2,6}\s+(?:\d+(?:\.\d+)*\.)?#(?:\s|\d)", str(markdown or "")):
        errors.append("malformed nested heading")
    errors = list(dict.fromkeys(error for error in errors if str(error).strip()))
    issues = [ReviewIssue("layout", "high", error) for error in errors]
    return MethodValidationResult(passed=not issues, issues=issues, raw="deterministic")


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


def _extract_complete_document_suffix(markdown: str, markers, paper_id: str = "") -> str:
    """Recover a complete final document appended after model narration or a partial draft."""
    text = _unwrap_outer_markdown_fence(markdown)
    identifier = re.escape(str(paper_id).strip()) if paper_id else r"[^\]\n]+"
    starts = [match.start() for match in re.finditer(rf"#\s+\[{identifier}\]", text)]
    for start in reversed(starts):
        candidate = text[start:].strip() + "\n"
        if not paper_markdown_completeness_errors(candidate, markers):
            return candidate
    return ""


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


def _post_publish_failure_message(warnings) -> str:
    items = [str(item) for item in warnings]
    infrastructure = [item for item in items if item.startswith("visual-qa:infrastructure:")]
    quality = [item for item in items if item not in infrastructure]
    if infrastructure and not quality:
        return (
            "文档已生成，但飞书 PDF 导出仍在处理中，视觉验收尚未完成；"
            "这是可恢复的基础设施状态，系统会继续重试：" + "; ".join(infrastructure)
        )
    if infrastructure:
        return (
            "文档已生成，但发布后验收尚未完成："
            + "; ".join(quality)
            + "；飞书导出仍在处理中："
            + "; ".join(infrastructure)
        )
    return "文档已生成，但发布后质检发现明确问题，暂不交付：" + "; ".join(items)


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
    pending = []
    for index, (marker, path, caption) in enumerate(figure_inserts):
        image_path = Path(path)
        if not image_path.exists():
            warnings.append((index, f"figure-vision-missing:{image_path.name}"))
            continue
        pending.append((index, marker, image_path, caption))

    def describe(item):
        index, marker, image_path, caption = item
        try:
            prompt = (
                f"marker: {marker}\n"
                f"caption: {caption or '[无 caption]'}\n"
                "请读图并说明这张图实际展示什么；如果 caption 与图像不一致，优先指出图像内容。"
            )
            try:
                text = llm.responses_image_text(
                    FIGURE_VISION_SYSTEM_PROMPT,
                    prompt,
                    image_path,
                    reasoning_effort="low",
                )
            except TypeError as exc:
                if "reasoning_effort" not in str(exc):
                    raise
                text = llm.responses_image_text(FIGURE_VISION_SYSTEM_PROMPT, prompt, image_path)
            text = " ".join(str(text or "").split())[:240]
            return index, marker, text, ""
        except Exception as exc:
            return index, marker, "", f"figure-vision-failed:{image_path.name}:{_short_error(exc, 180)}"

    try:
        workers = max(1, int(os.environ.get("MAXREAD_FIGURE_VISION_WORKERS", "4")))
    except ValueError:
        workers = 4
    results = []
    if len(pending) <= 1 or workers <= 1:
        results = [describe(item) for item in pending]
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(pending))) as executor:
            results = [future.result() for future in [executor.submit(describe, item) for item in pending]]
    for index, marker, description, warning in sorted(results, key=lambda item: item[0]):
        if description:
            descriptions[marker] = description
        if warning:
            warnings.append((index, warning))
    return descriptions, [warning for _index, warning in sorted(warnings, key=lambda item: item[0])]
