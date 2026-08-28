# MaxRead Workflow State Machine

MaxRead has two kinds of state:

- `workflow_state` is the durable business state used for recovery, retry, and terminal error classification.
- `stage` is a best-effort progress label used by the admin UI and Feishu reactions.

The old `queue_jobs.status` column remains as a compatibility projection:

- `queued`: waiting for a worker
- `running`: any non-terminal active workflow state
- `done`: `completed`
- `failed`: any retryable or terminal failure state

## Compact operator view

The durable lifecycle below intentionally keeps check and repair states separate
for recovery and audit. The operator-facing architecture page uses a compact
projection so the same business phase is not shown as several redundant nodes:

```mermaid
stateDiagram-v2
    [*] --> queued
    queued --> preparing: claim
    preparing --> generating: source_ready
    preparing --> delivery_gate: resume_published
    generating --> generating: repair and recheck
    generating --> reviewing: draft_ready
    reviewing --> quality_gate: review_completed
    quality_gate --> quality_gate: repair and recheck
    quality_gate --> publishing: quality_passed
    publishing --> delivery_gate: publish_succeeded
    delivery_gate --> delivery_gate: repair and recheck
    delivery_gate --> completed: complete
    preparing --> retryable_failure: source missing
    generating --> retryable_failure: generation budget exhausted
    quality_gate --> retryable_failure: quality rejected
    delivery_gate --> retryable_failure: delivery rejected
    retryable_failure --> queued: automatic or explicit retry
    queued --> cancelled: cancel
```

`workflow_state` remains the source of truth; the compact view is generated from
the mapping in `maxread/workflow.py` and includes the durable states represented
by each node. This keeps the diagram readable without losing the exact failure
reason in the database or `job_events`.

## Main lifecycle

```mermaid
stateDiagram-v2
    [*] --> queued
    queued --> claimed: claim
    claimed --> fetching: fetch_started
    fetching --> source_ready: source_ready
    fetching --> needs_source: source_missing
    source_ready --> generating: generation_started
    generating --> generation_checking: generation_check_started
    generation_checking --> generation_repairing: generation_repair_required
    generation_repairing --> generation_checking: generation_recheck
    generation_checking --> reviewing: draft_ready
    generation_checking --> generation_incomplete: generation_incomplete
    generation_repairing --> generation_incomplete: generation_incomplete
    reviewing --> quality_checking: review_completed
    quality_checking --> quality_repairing: quality_repair_required
    quality_repairing --> quality_checking: quality_recheck
    quality_checking --> publishing: quality_passed
    quality_checking --> quality_failed: quality_rejected
    publishing --> post_publish_checking: publish_succeeded
    post_publish_checking --> visual_checking: visual_qa_started
    visual_checking --> visual_repairing: visual_repair_required
    visual_repairing --> visual_checking: visual_recheck
    claimed --> post_publish_checking: resume_published
    post_publish_checking --> completed: complete
    visual_checking --> completed: complete
    visual_repairing --> completed: complete
    post_publish_checking --> quality_failed: quality_rejected
    visual_checking --> quality_failed: quality_rejected
    visual_repairing --> quality_failed: quality_rejected
    claimed --> queued: recover
    fetching --> queued: recover
    generating --> queued: recover
    reviewing --> queued: recover
    quality_checking --> queued: recover
    quality_repairing --> queued: recover
    publishing --> queued: recover
    post_publish_checking --> queued: recover
    visual_checking --> queued: recover
    visual_repairing --> queued: recover
    needs_source --> queued: retry
    generation_incomplete --> queued: retry
    quality_failed --> queued: retry
    failed --> queued: retry
```

`fail` is a controlled escape transition from any active state to `failed`.
`recover` is only for a worker that disappeared or stopped heartbeating. It
does not mark the paper as successful; the job starts again from `queued`.

At the queue boundary, only transient model/network/browser failures and
generation-with-no-valid-output are automatically replayed, and only within
`MAXREAD_AUTO_RETRY_ATTEMPTS`. Source and deterministic quality failures remain
visible for an explicit retry. A write failure without a durable publish
checkpoint is never blindly replayed because it may have left a partial Feishu
document.

Each running worker also owns a queue lease identified by `worker_id`. Heartbeat,
stage updates, workflow transitions, completion, and failure writes from the
queue worker validate that lease. A stale worker therefore cannot overwrite a
new worker after recovery. The recovery transaction rechecks `status = running`
under a write lock, so two workers cannot recover the same job.

## Why this split matters

The paper and article pipelines have different source and rendering handlers,
but their durable lifecycle is the same. They can therefore share queue
recovery, retry policy, progress reporting, and audit tooling without forcing
their parsing or publishing code into one large function.

Generation, quality repair, and visual QA are bounded subloops. A generation
round moves from `generation_checking` to `generation_repairing` and back;
deterministic repair runs before the model receives the previous draft and the
exact validation errors. A quality round can move from
`quality_checking` to `quality_repairing` and back, or from `visual_checking`
to `visual_repairing` and back. The configured round limit remains in those
controllers; the main state machine records where the loop is and why it
stopped.

Every durable transition increments `state_version` and appends a structured
`transition` record to `job_events`. Existing rows without `workflow_state`
are upgraded lazily from their old `status/stage` values.

`publish_succeeded` stores the Feishu document URL in `queue_jobs.doc_url` and
the expected title/formula/table counts in `checkpoint_json` before
post-publish checks. If the process dies after publishing, the next attempt
resumes from `post_publish_checking` and only verifies or repairs that document;
it does not fetch the paper or call the model again. Queue retries also keep
the normal idempotency boundary: a completed paper is reused rather than
creating a second document.

The queue has dedicated claim, heartbeat, and worker indexes. Enqueue uses an
immediate SQLite transaction so concurrent messages cannot both observe the
same dedupe key as inactive and create duplicate active jobs.

## Next refactor steps

The first three steps are now implemented: side effects remain in the existing
handlers, milestone events are persisted, quality/visual repair rounds are
represented as bounded state transitions, and published-document recovery is
idempotent. The remaining worthwhile improvements are:

1. Add more resumable checkpoints for expensive phases, so a retry can skip a verified source fetch or document render.
2. Add a notification retry worker for watcher messages without changing the job terminal state.
