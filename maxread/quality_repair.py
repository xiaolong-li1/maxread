from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Optional

from .quality import blocking_quality_warnings, pre_publish_quality_warnings
from .review import repair_markdown_block_with_quality_report, repair_markdown_with_quality_report
from .workflow import WorkflowEvent


@dataclass
class QualityRepairAttempt:
    round_index: int
    markdown: str
    xml: str
    warnings: List[str]
    blocking_warnings: List[str]
    model_response: str = ""
    repair_warnings: List[str] = field(default_factory=list)
    changed: bool = False


@dataclass
class QualityRepairResult:
    markdown: str
    xml: str
    warnings: List[str]
    blocking_warnings: List[str]
    repair_warnings: List[str]
    attempts: List[QualityRepairAttempt]

    @property
    def passed(self) -> bool:
        return not self.blocking_warnings


def repair_until_quality_passes(
    llm,
    markdown: str,
    markers: Iterable[str],
    render_xml: Callable[[str], str],
    normalize_markdown: Callable[[str], str],
    *,
    max_repair_rounds: int = 2,
    kind: str = "paper",
    reasoning_effort: Optional[str] = None,
    completeness_check: Optional[Callable[[str], Iterable[str]]] = None,
    prior_feedback: Iterable[str] = (),
    on_workflow_event=None,
) -> QualityRepairResult:
    """Run deterministic checks and ask the model to repair only blocking errors."""
    markers = list(markers)
    current = normalize_markdown(markdown)
    attempts: List[QualityRepairAttempt] = []
    repair_warnings: List[str] = []
    feedback_history = _dedupe(prior_feedback)
    max_rounds = max(0, int(max_repair_rounds))

    for round_index in range(max_rounds + 1):
        current = normalize_markdown(current)
        xml = render_xml(current)
        warnings = pre_publish_quality_warnings(current, xml)
        structural_warnings = _structural_blocking_warnings(current, completeness_check)
        blocking = _dedupe(structural_warnings + blocking_quality_warnings(warnings))
        attempt = QualityRepairAttempt(
            round_index=round_index,
            markdown=current,
            xml=xml,
            warnings=list(warnings),
            blocking_warnings=list(blocking),
        )
        attempts.append(attempt)
        if not blocking or round_index >= max_rounds or llm is None:
            return QualityRepairResult(
                markdown=current,
                xml=xml,
                warnings=list(warnings),
                blocking_warnings=list(blocking),
                repair_warnings=_dedupe(repair_warnings),
                attempts=attempts,
            )

        if on_workflow_event is not None:
            on_workflow_event(WorkflowEvent.QUALITY_REPAIR_REQUIRED, "; ".join(blocking))
        block_candidate, block_response, block_warnings = _repair_blocks_in_parallel(
            llm,
            current,
            render_xml,
            kind=kind,
            reasoning_effort=reasoning_effort,
            previous_feedback=feedback_history,
        )
        if block_candidate.strip() != current.strip():
            attempt.model_response = block_response
            attempt.repair_warnings = list(block_warnings)
            attempt.changed = True
            repair_warnings.extend(block_warnings)
            current = normalize_markdown(block_candidate)
            feedback_history = _dedupe(feedback_history + list(blocking) + list(block_warnings))
            if on_workflow_event is not None:
                on_workflow_event(WorkflowEvent.QUALITY_RECHECK, f"round={round_index + 1}; block-repair")
            continue
        try:
            review = repair_markdown_with_quality_report(
                llm,
                current,
                markers,
                blocking,
                kind=kind,
                reasoning_effort=reasoning_effort,
                previous_feedback=feedback_history,
            )
            attempt.model_response = review.raw
            attempt.repair_warnings = [
                f"quality-repair:{issue.category}:{issue.severity}:{issue.detail}"
                for issue in review.issues
            ]
            repair_warnings.extend(attempt.repair_warnings)
            candidate = normalize_markdown(review.markdown)
            attempt.changed = candidate.strip() != current.strip()
            if not attempt.changed:
                warning = f"quality-repair:round-{round_index + 1}:no-change"
                attempt.repair_warnings.append(warning)
                repair_warnings.append(warning)
            current = candidate
        except Exception as exc:
            warning = f"quality-repair:round-{round_index + 1}:model-call-failed:{_clip(exc)}"
            attempt.model_response = warning
            attempt.repair_warnings.append(warning)
            repair_warnings.append(warning)
        feedback_history = _dedupe(feedback_history + list(blocking) + list(attempt.repair_warnings))
        if on_workflow_event is not None:
            on_workflow_event(WorkflowEvent.QUALITY_RECHECK, f"round={round_index + 1}")
        review_format_failed = any("review returned non-json" in item for item in attempt.repair_warnings)
        if (
            not attempt.changed
            and not any("model-call-failed" in item for item in attempt.repair_warnings)
            and not review_format_failed
        ):
            return QualityRepairResult(
                markdown=current,
                xml=xml,
                warnings=list(warnings),
                blocking_warnings=list(blocking),
                repair_warnings=_dedupe(repair_warnings),
                attempts=attempts,
            )

    raise AssertionError("quality repair loop terminated unexpectedly")


_BLOCK_LOCAL_PREFIXES = (
    "quality:formula:",
    "quality:xml:",
    "quality:format:",
)


def _repair_blocks_in_parallel(
    llm,
    markdown: str,
    render_xml: Callable[[str], str],
    *,
    kind: str,
    reasoning_effort: Optional[str],
    previous_feedback: Iterable[str],
) -> tuple[str, str, List[str]]:
    blocks = [block for block in re.split(r"\n{2,}", str(markdown or "")) if block.strip()]
    suspicious = []
    for index, block in enumerate(blocks):
        warnings = blocking_quality_warnings(pre_publish_quality_warnings(block, render_xml(block)))
        local = [warning for warning in warnings if warning.startswith(_BLOCK_LOCAL_PREFIXES)]
        if local:
            suspicious.append((index, block, local))
    if not suspicious:
        return markdown, "", []

    try:
        workers = max(1, int(os.environ.get("MAXREAD_QUALITY_BLOCK_WORKERS", "4")))
    except ValueError:
        workers = 4

    def repair(item):
        index, block, warnings = item
        markers = re.findall(r"\[MaxReadFigure:[^\]]+\]", block)
        try:
            review = repair_markdown_block_with_quality_report(
                llm,
                block,
                warnings,
                reasoning_effort=reasoning_effort,
                previous_feedback=previous_feedback,
            )
            candidate = review.markdown.strip()
            if not block.lstrip().startswith("# ") and candidate.lstrip().startswith("# "):
                return index, block, review.raw, [f"quality-block:{index}:returned-full-document"]
            if any(marker not in candidate for marker in markers):
                return index, block, review.raw, [f"quality-block:{index}:lost-marker"]
            remaining = blocking_quality_warnings(
                pre_publish_quality_warnings(candidate, render_xml(candidate))
            )
            remaining_local = [warning for warning in remaining if warning.startswith(_BLOCK_LOCAL_PREFIXES)]
            if len(remaining_local) >= len(warnings):
                return index, block, review.raw, [f"quality-block:{index}:no-improvement"]
            notes = [
                f"quality-block:{index}:{issue.category}:{issue.severity}:{issue.detail}"
                for issue in review.issues
            ]
            return index, candidate, review.raw, notes
        except Exception as exc:
            return index, block, "", [f"quality-block:{index}:model-call-failed:{_clip(exc)}"]

    repaired = {}
    responses = []
    notes: List[str] = []
    with ThreadPoolExecutor(max_workers=min(workers, len(suspicious))) as executor:
        futures = {executor.submit(repair, item): item[0] for item in suspicious}
        for future in as_completed(futures):
            index, candidate, raw, item_notes = future.result()
            repaired[index] = candidate
            if raw:
                responses.append(f"[block {index}]\n{raw}")
            notes.extend(item_notes)
    output = [repaired.get(index, block) for index, block in enumerate(blocks)]
    return "\n\n".join(output).strip() + "\n", "\n\n".join(responses), notes


def _structural_blocking_warnings(
    markdown: str,
    completeness_check: Optional[Callable[[str], Iterable[str]]],
) -> List[str]:
    if completeness_check is None:
        return []
    return [
        f"quality:structure:markdown:high:{error}"
        for error in completeness_check(markdown)
        if str(error).strip()
    ]


def _dedupe(values: Iterable[str]) -> List[str]:
    output: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            output.append(text)
    return output


def _clip(value, max_chars: int = 240) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= max_chars else text[:max_chars] + "..."
