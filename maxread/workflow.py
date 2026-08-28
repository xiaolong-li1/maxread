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


# The durable state machine intentionally keeps these states separate for
# recovery and audit.  The architecture page uses this smaller projection so
# implementation details such as "check -> repair -> recheck" do not look like
# three different business phases.
COMPACT_STATE_PRESENTATION = {
    "queued": ("等待处理", "intake", "等待 worker 认领任务，尚未开始读取论文。"),
    "preparing": ("获取原文", "intake", "下载并解析元数据、TeX source、PDF、表格和图片。"),
    "generating": ("生成初稿", "generation", "根据论文证据生成完整 Markdown，并检查章节和图片标记是否齐全。"),
    "reviewing": ("内容审校", "review", "核对事实、结构、方法上下文、公式解释和图文语义。"),
    "quality_gate": ("公式格式检查", "review", "规范化并检查公式、表格、Markdown 和 Docx XML，发现问题时有限修复。"),
    "publishing": ("写入飞书", "delivery", "创建文档、写入 XML、插图并持久化发布检查点。"),
    "delivery_gate": ("检查真实页面", "delivery", "回读飞书并用浏览器截图检查用户实际看到的公式、图片和布局。"),
    "completed": ("交付完成", "terminal", "真实页面通过最终检查，文档已经可以交付。"),
    "retryable_failure": ("失败待重试", "terminal", "保留错误、原稿和检查点；自动修复耗尽后等待重新排队。"),
    "cancelled": ("已取消", "terminal", "任务被显式取消，不再继续产生副作用。"),
}

COMPACT_STATE_FOR_DURABLE = {
    WorkflowState.QUEUED: "queued",
    WorkflowState.CLAIMED: "preparing",
    WorkflowState.FETCHING: "preparing",
    WorkflowState.SOURCE_READY: "preparing",
    WorkflowState.GENERATING: "generating",
    WorkflowState.GENERATION_CHECKING: "generating",
    WorkflowState.GENERATION_REPAIRING: "generating",
    WorkflowState.REVIEWING: "reviewing",
    WorkflowState.QUALITY_CHECKING: "quality_gate",
    WorkflowState.QUALITY_REPAIRING: "quality_gate",
    WorkflowState.PUBLISHING: "publishing",
    WorkflowState.POST_PUBLISH_CHECKING: "delivery_gate",
    WorkflowState.VISUAL_CHECKING: "delivery_gate",
    WorkflowState.VISUAL_REPAIRING: "delivery_gate",
    WorkflowState.COMPLETED: "completed",
    WorkflowState.NEEDS_SOURCE: "retryable_failure",
    WorkflowState.GENERATION_INCOMPLETE: "retryable_failure",
    WorkflowState.QUALITY_FAILED: "retryable_failure",
    WorkflowState.FAILED: "retryable_failure",
    WorkflowState.CANCELLED: "cancelled",
}


def compact_workflow_spec() -> dict:
    """Return the user-facing workflow projection.

    This is deliberately derived from the durable machine rather than used as
    its persistence format.  It gives operators one stable path while keeping
    enough detail in ``workflow_state`` and ``job_events`` for diagnostics.
    """
    if set(COMPACT_STATE_FOR_DURABLE) != set(WorkflowState):
        raise RuntimeError("compact graph does not cover every durable workflow state")

    def state(state_id: str, terminal: bool = False, retryable: bool = False) -> dict:
        label, phase, detail = COMPACT_STATE_PRESENTATION[state_id]
        durable_states = [
            item.value for item, compact in COMPACT_STATE_FOR_DURABLE.items() if compact == state_id
        ]
        return {
            "id": state_id,
            "terminal": terminal,
            "retryable": retryable,
            "active": not terminal,
            "label": label,
            "phase": phase,
            "detail": detail,
            "durable_states": durable_states,
        }

    states = [
        state("queued"),
        state("preparing"),
        state("generating"),
        state("reviewing"),
        state("quality_gate"),
        state("publishing"),
        state("delivery_gate"),
        state("completed", terminal=True),
        state("retryable_failure", terminal=True, retryable=True),
        state("cancelled", terminal=True),
    ]
    transitions = [
        {"from": "queued", "event": "claim", "to": "preparing", "label": "开始获取原文", "condition": "worker 成功认领任务"},
        {"from": "preparing", "event": "generation_started", "to": "generating", "label": "原文解析完成", "condition": "原文材料满足生成前置条件"},
        {"from": "preparing", "event": "source_missing", "to": "retryable_failure", "label": "原文材料不全", "condition": "材料不足，等待补源或显式重试"},
        {"from": "generating", "event": "draft_ready", "to": "reviewing", "label": "初稿完整", "condition": "完整性契约通过；不通过时在本步骤内修复"},
        {"from": "generating", "event": "generation_repair_required", "to": "generating", "label": "修复初稿后重检", "condition": "缺章节、公式/图片标记或格式契约失败，预算未耗尽"},
        {"from": "generating", "event": "generation_incomplete", "to": "retryable_failure", "label": "初稿修复耗尽", "condition": "有限生成预算耗尽，保留每轮原始输出"},
        {"from": "reviewing", "event": "review_completed", "to": "quality_gate", "label": "内容审校完成", "condition": "内容审阅完成；辅助审阅失败可带 warning 降级"},
        {"from": "quality_gate", "event": "quality_repair_required", "to": "quality_gate", "label": "修复格式后重检", "condition": "公式、XML 或格式告警可定位且预算未耗尽"},
        {"from": "quality_gate", "event": "quality_passed", "to": "publishing", "label": "公式格式通过", "condition": "不存在阻断级发布前问题"},
        {"from": "quality_gate", "event": "quality_rejected", "to": "retryable_failure", "label": "格式修复耗尽", "condition": "修复预算耗尽，暂不创建或交付文档"},
        {"from": "publishing", "event": "publish_succeeded", "to": "delivery_gate", "label": "飞书写入完成", "condition": "文档写入成功，已保存可恢复 checkpoint"},
        {"from": "preparing", "event": "resume_published", "to": "delivery_gate", "label": "复用已发布文档", "condition": "已有发布文档，跳过正文生成直接复检"},
        {"from": "delivery_gate", "event": "visual_repair_required", "to": "delivery_gate", "label": "修复页面后重检", "condition": "真实页面发现可定位问题，预算未耗尽"},
        {"from": "delivery_gate", "event": "complete", "to": "completed", "label": "页面检查通过", "condition": "回读、截图和阻断级检查全部通过"},
        {"from": "delivery_gate", "event": "quality_rejected", "to": "retryable_failure", "label": "页面修复耗尽", "condition": "页面检查或基础设施重试预算耗尽"},
        {"from": "retryable_failure", "event": "retry", "to": "queued", "label": "重新排队", "condition": "自动恢复预算或用户显式重试"},
    ]
    policies = [
        {"event": "recover", "from": "active_except_queued", "to": "queued", "sources": ["preparing", "generating", "reviewing", "quality_gate", "publishing", "delivery_gate"], "label": "任务重新排队", "condition": "Worker 失联或心跳超时"},
        {"event": "fail", "from": "non_terminal", "to": "retryable_failure", "sources": ["queued", "preparing", "generating", "reviewing", "quality_gate", "publishing", "delivery_gate"], "label": "失败后等待重试", "condition": "未处理异常；记录原因后等待自动或显式重试"},
        {"event": "cancel", "from": "non_terminal", "to": "cancelled", "sources": ["queued", "preparing", "generating", "reviewing", "quality_gate", "publishing", "delivery_gate"], "label": "取消任务", "condition": "收到显式取消请求"},
    ]
    scenarios = [
        {
            "id": "happy",
            "label": "正常交付",
            "summary": "所有业务阶段沿一条主路径完成；只有发现问题时才进入对应修复支路并返回原门重检。",
            "states": ["queued", "preparing", "generating", "reviewing", "quality_gate", "publishing", "delivery_gate", "completed"],
            "events": ["claim", "generation_started", "draft_ready", "review_completed", "quality_passed", "publish_succeeded", "complete"],
        },
        {
            "id": "bounded-repair",
            "label": "发现问题后修复",
            "summary": "检查发现问题后进入对应修复支路，修复完成返回原步骤重检；只有预算耗尽才进入失败待重试。",
            "states": ["generating", "generating", "reviewing", "quality_gate", "quality_gate", "publishing", "delivery_gate", "delivery_gate", "completed"],
            "events": ["generation_repair_required", "draft_ready", "review_completed", "quality_repair_required", "quality_passed", "publish_succeeded", "visual_repair_required", "complete"],
        },
        {
            "id": "automatic-recovery",
            "label": "自动恢复",
            "summary": "暂态故障先有限次回队列；带发布检查点时复用原文档，不重跑正文。",
            "states": ["delivery_gate", "retryable_failure", "queued", "preparing", "delivery_gate", "completed"],
            "events": ["quality_rejected", "retry", "claim", "resume_published", "complete"],
        },
    ]
    return {"states": states, "transitions": transitions, "policies": policies, "scenarios": scenarios}


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


def legacy_stage_for_state(state: WorkflowState | str) -> str:
    """Project the durable workflow onto the legacy progress-stage column.

    ``queue_jobs.stage`` still drives the operator UI and message reactions.
    Keeping this projection next to the state machine prevents a job from
    appearing to be downloading after it has already entered generation.
    """
    current = WorkflowState(state)
    if current is WorkflowState.QUEUED:
        return "queued"
    if current is WorkflowState.CLAIMED:
        return "claimed"
    if current is WorkflowState.FETCHING:
        return "downloading"
    if current in {
        WorkflowState.SOURCE_READY,
        WorkflowState.GENERATING,
        WorkflowState.GENERATION_CHECKING,
        WorkflowState.GENERATION_REPAIRING,
    }:
        return "reading"
    if current in {
        WorkflowState.REVIEWING,
        WorkflowState.QUALITY_CHECKING,
        WorkflowState.QUALITY_REPAIRING,
        WorkflowState.POST_PUBLISH_CHECKING,
        WorkflowState.VISUAL_CHECKING,
        WorkflowState.VISUAL_REPAIRING,
    }:
        return "reviewing"
    if current is WorkflowState.PUBLISHING:
        return "writing"
    if current is WorkflowState.COMPLETED:
        return "done"
    if current is WorkflowState.CANCELLED:
        return "cancelled"
    return "failed"


def failure_kind_for_state(state: WorkflowState | str) -> FailureKind:
    current = WorkflowState(state)
    return {
        WorkflowState.NEEDS_SOURCE: FailureKind.SOURCE_UNAVAILABLE,
        WorkflowState.GENERATION_INCOMPLETE: FailureKind.GENERATION_INCOMPLETE,
        WorkflowState.QUALITY_FAILED: FailureKind.QUALITY_REJECTED,
        WorkflowState.FAILED: FailureKind.EXECUTION_FAILED,
        WorkflowState.CANCELLED: FailureKind.CANCELLED,
    }.get(current, FailureKind.NONE)


def workflow_spec() -> dict:
    """Return a serialization-safe description of the executable state machine."""
    return {
        "states": [
            {
                "id": state.value,
                "terminal": state in TERMINAL_STATES,
                "retryable": state in RETRYABLE_STATES,
                "active": state in ACTIVE_STATES,
            }
            for state in WorkflowState
        ],
        "transitions": [
            {
                "from": source.value,
                "event": event.value,
                "to": target.value,
            }
            for (source, event), target in _TRANSITIONS.items()
        ],
        "policies": [
            {"event": WorkflowEvent.FAIL.value, "from": "non_terminal", "to": WorkflowState.FAILED.value},
            {"event": WorkflowEvent.RECOVER.value, "from": "active_except_queued", "to": WorkflowState.QUEUED.value},
            {"event": WorkflowEvent.RETRY.value, "from": "retryable", "to": WorkflowState.QUEUED.value},
            {"event": WorkflowEvent.CANCEL.value, "from": "non_terminal", "to": WorkflowState.CANCELLED.value},
            {
                "event": WorkflowEvent.COMPLETE.value,
                "from": "publish_or_visual_check",
                "to": WorkflowState.COMPLETED.value,
            },
        ],
        "compact_graph": compact_workflow_spec(),
    }


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
