from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Dict, FrozenSet, Optional, Tuple


class WorkflowState(str, Enum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    FETCHING = "fetching"
    SOURCE_READY = "source_ready"
    GENERATING = "generating"
    GENERATION_CHECKING = "generation_checking"
    GENERATION_REPAIRING = "generation_repairing"
    REVIEWING = "reviewing"
    QUALITY_CHECKING = "quality_checking"
    QUALITY_REPAIRING = "quality_repairing"
    PUBLISHING = "publishing"
    POST_PUBLISH_CHECKING = "post_publish_checking"
    VISUAL_CHECKING = "visual_checking"
    VISUAL_REPAIRING = "visual_repairing"
    COMPLETED = "completed"
    NEEDS_SOURCE = "needs_source"
    GENERATION_INCOMPLETE = "generation_incomplete"
    QUALITY_FAILED = "quality_failed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkflowEvent(str, Enum):
    CLAIM = "claim"
    FETCH_STARTED = "fetch_started"
    SOURCE_READY = "source_ready"
    SOURCE_MISSING = "source_missing"
    GENERATION_STARTED = "generation_started"
    GENERATION_CHECK_STARTED = "generation_check_started"
    GENERATION_REPAIR_REQUIRED = "generation_repair_required"
    GENERATION_RECHECK = "generation_recheck"
    DRAFT_READY = "draft_ready"
    GENERATION_INCOMPLETE = "generation_incomplete"
    REVIEW_COMPLETED = "review_completed"
    QUALITY_REPAIR_REQUIRED = "quality_repair_required"
    QUALITY_RECHECK = "quality_recheck"
    QUALITY_PASSED = "quality_passed"
    QUALITY_REJECTED = "quality_rejected"
    PUBLISH_SUCCEEDED = "publish_succeeded"
    RESUME_PUBLISHED = "resume_published"
    VISUAL_QA_STARTED = "visual_qa_started"
    VISUAL_REPAIR_REQUIRED = "visual_repair_required"
    VISUAL_RECHECK = "visual_recheck"
    COMPLETE = "complete"
    FAIL = "fail"
    RECOVER = "recover"
    RETRY = "retry"
    CANCEL = "cancel"


class FailureKind(str, Enum):
    NONE = "none"
    SOURCE_UNAVAILABLE = "source_unavailable"
    GENERATION_INCOMPLETE = "generation_incomplete"
    QUALITY_REJECTED = "quality_rejected"
    EXECUTION_FAILED = "execution_failed"
    CANCELLED = "cancelled"


TERMINAL_STATES: FrozenSet[WorkflowState] = frozenset(
    {
        WorkflowState.COMPLETED,
        WorkflowState.NEEDS_SOURCE,
        WorkflowState.GENERATION_INCOMPLETE,
        WorkflowState.QUALITY_FAILED,
        WorkflowState.FAILED,
        WorkflowState.CANCELLED,
    }
)

RETRYABLE_STATES: FrozenSet[WorkflowState] = frozenset(
    {
        WorkflowState.NEEDS_SOURCE,
        WorkflowState.GENERATION_INCOMPLETE,
        WorkflowState.QUALITY_FAILED,
        WorkflowState.FAILED,
    }
)

ACTIVE_STATES: FrozenSet[WorkflowState] = frozenset(set(WorkflowState) - set(TERMINAL_STATES))


_TRANSITIONS: Dict[Tuple[WorkflowState, WorkflowEvent], WorkflowState] = {
    (WorkflowState.QUEUED, WorkflowEvent.CLAIM): WorkflowState.CLAIMED,
    (WorkflowState.CLAIMED, WorkflowEvent.FETCH_STARTED): WorkflowState.FETCHING,
    (WorkflowState.FETCHING, WorkflowEvent.SOURCE_READY): WorkflowState.SOURCE_READY,
    (WorkflowState.FETCHING, WorkflowEvent.SOURCE_MISSING): WorkflowState.NEEDS_SOURCE,
    (WorkflowState.SOURCE_READY, WorkflowEvent.GENERATION_STARTED): WorkflowState.GENERATING,
    (WorkflowState.GENERATING, WorkflowEvent.GENERATION_CHECK_STARTED): WorkflowState.GENERATION_CHECKING,
    (WorkflowState.GENERATION_CHECKING, WorkflowEvent.GENERATION_REPAIR_REQUIRED): WorkflowState.GENERATION_REPAIRING,
    (WorkflowState.GENERATION_REPAIRING, WorkflowEvent.GENERATION_RECHECK): WorkflowState.GENERATION_CHECKING,
    (WorkflowState.GENERATING, WorkflowEvent.DRAFT_READY): WorkflowState.REVIEWING,
    (WorkflowState.GENERATION_CHECKING, WorkflowEvent.DRAFT_READY): WorkflowState.REVIEWING,
    (WorkflowState.GENERATING, WorkflowEvent.GENERATION_INCOMPLETE): WorkflowState.GENERATION_INCOMPLETE,
    (WorkflowState.GENERATION_CHECKING, WorkflowEvent.GENERATION_INCOMPLETE): WorkflowState.GENERATION_INCOMPLETE,
    (WorkflowState.GENERATION_REPAIRING, WorkflowEvent.GENERATION_INCOMPLETE): WorkflowState.GENERATION_INCOMPLETE,
    (WorkflowState.REVIEWING, WorkflowEvent.REVIEW_COMPLETED): WorkflowState.QUALITY_CHECKING,
    (WorkflowState.QUALITY_CHECKING, WorkflowEvent.QUALITY_REPAIR_REQUIRED): WorkflowState.QUALITY_REPAIRING,
    (WorkflowState.QUALITY_REPAIRING, WorkflowEvent.QUALITY_RECHECK): WorkflowState.QUALITY_CHECKING,
    (WorkflowState.QUALITY_CHECKING, WorkflowEvent.QUALITY_PASSED): WorkflowState.PUBLISHING,
    (WorkflowState.QUALITY_CHECKING, WorkflowEvent.QUALITY_REJECTED): WorkflowState.QUALITY_FAILED,
    (WorkflowState.QUALITY_REPAIRING, WorkflowEvent.QUALITY_REJECTED): WorkflowState.QUALITY_FAILED,
    (WorkflowState.PUBLISHING, WorkflowEvent.PUBLISH_SUCCEEDED): WorkflowState.POST_PUBLISH_CHECKING,
    (WorkflowState.CLAIMED, WorkflowEvent.RESUME_PUBLISHED): WorkflowState.POST_PUBLISH_CHECKING,
    (WorkflowState.POST_PUBLISH_CHECKING, WorkflowEvent.VISUAL_QA_STARTED): WorkflowState.VISUAL_CHECKING,
    (WorkflowState.VISUAL_CHECKING, WorkflowEvent.VISUAL_REPAIR_REQUIRED): WorkflowState.VISUAL_REPAIRING,
    (WorkflowState.VISUAL_REPAIRING, WorkflowEvent.VISUAL_RECHECK): WorkflowState.VISUAL_CHECKING,
    (WorkflowState.POST_PUBLISH_CHECKING, WorkflowEvent.QUALITY_REJECTED): WorkflowState.QUALITY_FAILED,
    (WorkflowState.VISUAL_CHECKING, WorkflowEvent.QUALITY_REJECTED): WorkflowState.QUALITY_FAILED,
    (WorkflowState.VISUAL_REPAIRING, WorkflowEvent.QUALITY_REJECTED): WorkflowState.QUALITY_FAILED,
}


class InvalidWorkflowTransition(ValueError):
    def __init__(self, state: WorkflowState, event: WorkflowEvent):
        self.state = state
        self.event = event
        super().__init__(f"invalid workflow transition: {state.value} + {event.value}")


@dataclass(frozen=True)
class WorkflowTransition:
    from_state: WorkflowState
    event: WorkflowEvent
    to_state: WorkflowState

    @property
    def terminal(self) -> bool:
        return self.to_state in TERMINAL_STATES

    @property
    def retryable(self) -> bool:
        return self.to_state in RETRYABLE_STATES


@dataclass(frozen=True)
class PublishedCheckpoint:
    """Durable facts needed to verify a document after a publish-time crash."""

    doc_url: str
    expected_title: str = ""
    expected_image_min: int = 0
    expected_latex_min: int = 0
    expected_table_min: int = 0

    def to_json(self) -> str:
        return json.dumps(
            {
                "doc_url": self.doc_url,
                "expected_title": self.expected_title,
                "expected_image_min": max(0, int(self.expected_image_min or 0)),
                "expected_latex_min": max(0, int(self.expected_latex_min or 0)),
                "expected_table_min": max(0, int(self.expected_table_min or 0)),
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, value: str = "", fallback_url: str = "") -> Optional["PublishedCheckpoint"]:
        raw = str(value or "").strip()
        payload = {}
        if raw:
            try:
                candidate = json.loads(raw)
                if isinstance(candidate, dict):
                    payload = candidate
            except (TypeError, ValueError):
                pass
        raw_url = raw if raw.startswith(("http://", "https://")) else ""
        doc_url = str(payload.get("doc_url") or fallback_url or raw_url or "").strip()
        if not doc_url:
            return None

        def count(name: str) -> int:
            try:
                return max(0, int(payload.get(name) or 0))
            except (TypeError, ValueError):
                return 0

        return cls(
            doc_url=doc_url,
            expected_title=str(payload.get("expected_title") or "").strip(),
            expected_image_min=count("expected_image_min"),
            expected_latex_min=count("expected_latex_min"),
            expected_table_min=count("expected_table_min"),
        )


def transition(state: WorkflowState | str, event: WorkflowEvent | str) -> WorkflowTransition:
    current = WorkflowState(state)
    trigger = WorkflowEvent(event)
    target = _TRANSITIONS.get((current, trigger))

    if target is None and trigger is WorkflowEvent.FAIL and current not in TERMINAL_STATES:
        target = WorkflowState.FAILED
    elif target is None and trigger is WorkflowEvent.RECOVER and current in ACTIVE_STATES - {WorkflowState.QUEUED}:
        target = WorkflowState.QUEUED
    elif target is None and trigger is WorkflowEvent.RETRY and current in RETRYABLE_STATES:
        target = WorkflowState.QUEUED
    elif target is None and trigger is WorkflowEvent.CANCEL and current not in TERMINAL_STATES:
        target = WorkflowState.CANCELLED
    elif target is None and trigger is WorkflowEvent.COMPLETE and current in {
        WorkflowState.CLAIMED,
        WorkflowState.PUBLISHING,
        WorkflowState.POST_PUBLISH_CHECKING,
        WorkflowState.VISUAL_CHECKING,
        WorkflowState.VISUAL_REPAIRING,
    }:
        # CLAIMED is accepted for compatibility with older workers that only
        # persisted queue-level progress before marking a job complete.
        target = WorkflowState.COMPLETED

    if target is None:
        raise InvalidWorkflowTransition(current, trigger)
    return WorkflowTransition(current, trigger, target)


def queue_status_for_state(state: WorkflowState | str) -> str:
    current = WorkflowState(state)
    if current is WorkflowState.QUEUED:
        return "queued"
    if current is WorkflowState.COMPLETED:
        return "done"
    if current in TERMINAL_STATES:
        return "failed"
    return "running"


def failure_kind_for_state(state: WorkflowState | str) -> FailureKind:
    current = WorkflowState(state)
    return {
        WorkflowState.NEEDS_SOURCE: FailureKind.SOURCE_UNAVAILABLE,
        WorkflowState.GENERATION_INCOMPLETE: FailureKind.GENERATION_INCOMPLETE,
        WorkflowState.QUALITY_FAILED: FailureKind.QUALITY_REJECTED,
        WorkflowState.FAILED: FailureKind.EXECUTION_FAILED,
        WorkflowState.CANCELLED: FailureKind.CANCELLED,
    }.get(current, FailureKind.NONE)


def state_from_legacy(status: str, stage: str = "") -> WorkflowState:
    stage_value = str(stage or "").strip().lower().replace("-", "_")
    try:
        return WorkflowState(stage_value)
    except ValueError:
        pass
    aliases = {
        "downloading": WorkflowState.FETCHING,
        "reading": WorkflowState.GENERATING,
        "reviewing": WorkflowState.REVIEWING,
        "quality_checking": WorkflowState.QUALITY_CHECKING,
        "quality_repairing": WorkflowState.QUALITY_REPAIRING,
        "writing": WorkflowState.PUBLISHING,
        "verifying": WorkflowState.POST_PUBLISH_CHECKING,
        "visual_qa": WorkflowState.VISUAL_CHECKING,
        "visual_repair": WorkflowState.VISUAL_REPAIRING,
        "done": WorkflowState.COMPLETED,
        "needs_source": WorkflowState.NEEDS_SOURCE,
        "summary_incomplete": WorkflowState.GENERATION_INCOMPLETE,
        "quality_failed": WorkflowState.QUALITY_FAILED,
        "failed": WorkflowState.FAILED,
        "cancelled": WorkflowState.CANCELLED,
        "claimed": WorkflowState.CLAIMED,
    }
    if stage_value in aliases:
        return aliases[stage_value]

    status_value = str(status or "").strip().lower()
    if status_value == "queued":
        return WorkflowState.QUEUED
    if status_value == "done":
        return WorkflowState.COMPLETED
    if status_value == "failed":
        return WorkflowState.FAILED
    if status_value == "running":
        return WorkflowState.CLAIMED
    raise ValueError(f"unknown legacy workflow state: status={status!r}, stage={stage!r}")
