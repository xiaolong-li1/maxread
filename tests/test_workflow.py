from maxread.workflow import (
    FailureKind,
    InvalidWorkflowTransition,
    PublishedCheckpoint,
    WorkflowEvent,
    WorkflowState,
    failure_kind_for_state,
    queue_status_for_state,
    state_from_legacy,
    transition,
)


def test_happy_path_reaches_completed():
    state = WorkflowState.QUEUED
    events = [
        WorkflowEvent.CLAIM,
        WorkflowEvent.FETCH_STARTED,
        WorkflowEvent.SOURCE_READY,
        WorkflowEvent.GENERATION_STARTED,
        WorkflowEvent.DRAFT_READY,
        WorkflowEvent.REVIEW_COMPLETED,
        WorkflowEvent.QUALITY_PASSED,
        WorkflowEvent.PUBLISH_SUCCEEDED,
        WorkflowEvent.VISUAL_QA_STARTED,
        WorkflowEvent.COMPLETE,
    ]

    for event in events:
        state = transition(state, event).to_state

    assert state is WorkflowState.COMPLETED
    assert queue_status_for_state(state) == "done"


def test_published_document_can_resume_at_post_publish_check():
    state = transition(WorkflowState.CLAIMED, WorkflowEvent.RESUME_PUBLISHED).to_state
    assert state is WorkflowState.POST_PUBLISH_CHECKING


def test_published_checkpoint_round_trips_expected_quality_counts():
    checkpoint = PublishedCheckpoint("https://tenant.feishu.cn/docx/abc", "Title", 3, 12, 2)
    restored = PublishedCheckpoint.from_json(checkpoint.to_json())
    assert restored == checkpoint
    assert PublishedCheckpoint.from_json("https://tenant.feishu.cn/docx/legacy").doc_url.endswith("legacy")


def test_quality_and_visual_repairs_are_bounded_loops():
    quality = transition(WorkflowState.QUALITY_CHECKING, WorkflowEvent.QUALITY_REPAIR_REQUIRED)
    assert quality.to_state is WorkflowState.QUALITY_REPAIRING
    assert transition(quality.to_state, WorkflowEvent.QUALITY_RECHECK).to_state is WorkflowState.QUALITY_CHECKING

    visual = transition(WorkflowState.VISUAL_CHECKING, WorkflowEvent.VISUAL_REPAIR_REQUIRED)
    assert visual.to_state is WorkflowState.VISUAL_REPAIRING
    assert transition(visual.to_state, WorkflowEvent.VISUAL_RECHECK).to_state is WorkflowState.VISUAL_CHECKING


def test_failure_states_have_explicit_retry_semantics():
    failed = transition(WorkflowState.GENERATING, WorkflowEvent.FAIL)
    assert failed.to_state is WorkflowState.FAILED
    assert failed.terminal is True
    assert failed.retryable is True
    assert failure_kind_for_state(failed.to_state) is FailureKind.EXECUTION_FAILED
    assert transition(failed.to_state, WorkflowEvent.RETRY).to_state is WorkflowState.QUEUED


def test_generation_incomplete_is_a_distinct_retryable_terminal_state():
    state = transition(WorkflowState.SOURCE_READY, WorkflowEvent.GENERATION_STARTED).to_state
    state = transition(state, WorkflowEvent.GENERATION_INCOMPLETE).to_state
    assert state is WorkflowState.GENERATION_INCOMPLETE
    assert transition(state, WorkflowEvent.RETRY).to_state is WorkflowState.QUEUED


def test_worker_recovery_returns_active_job_to_queue():
    recovered = transition(WorkflowState.VISUAL_REPAIRING, WorkflowEvent.RECOVER)
    assert recovered.to_state is WorkflowState.QUEUED


def test_invalid_transition_is_rejected():
    try:
        transition(WorkflowState.QUEUED, WorkflowEvent.QUALITY_PASSED)
        raise AssertionError("invalid transition was accepted")
    except InvalidWorkflowTransition:
        pass

    try:
        transition(WorkflowState.COMPLETED, WorkflowEvent.RETRY)
        raise AssertionError("completed job was retried")
    except InvalidWorkflowTransition:
        pass


def test_legacy_queue_state_mapping_keeps_existing_database_compatible():
    assert state_from_legacy("queued", "") is WorkflowState.QUEUED
    assert state_from_legacy("running", "reading") is WorkflowState.GENERATING
    assert state_from_legacy("running", "visual-qa") is WorkflowState.VISUAL_CHECKING
    assert state_from_legacy("done", "done") is WorkflowState.COMPLETED
    assert state_from_legacy("failed", "quality_failed") is WorkflowState.QUALITY_FAILED
