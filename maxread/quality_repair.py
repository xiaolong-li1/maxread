from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Optional

from .quality import blocking_quality_warnings, pre_publish_quality_warnings
from .review import repair_markdown_with_quality_report
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
    max_repair_rounds: int = 3,
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

    raise AssertionError("quality repair loop terminated unexpectedly")


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
