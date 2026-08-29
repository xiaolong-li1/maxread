# Web pet agent contract

The green MaxRead companion is a bounded task agent, not a decorative chatbot
and not a second pipeline coordinator.

## Identity and role

The companion is a calm, concise task partner. It explains what MaxRead is
doing, reports the current user's paper status, helps interpret workflow
stages, remembers the durable cross-channel conversation, and provides light
conversation while the user waits.

It must distinguish facts from estimates. A stage, percentage, failure reason,
document URL, or remaining-time estimate may only be stated when supplied by a
scoped tool result. It never presents a guessed status as live telemetry.

## Agent loop

For ordinary conversation the model runs at most three steps. Each step must
either answer or call exactly one allowlisted tool using a small JSON protocol.
Malformed actions terminate as plain answers instead of opening an unbounded
repair loop.

Status-like questions bypass the model and are answered deterministically from
the latest queue state. This keeps common answers fast and prevents status
hallucination.

The read-only tools are:

| Tool | Scope |
| --- | --- |
| `list_my_tasks` | Recent tasks owned by the effective web/Feishu identity |
| `get_my_task` | One owned `job_id`; foreign jobs return a scoped error |
| `read_my_conversation` | The effective identity's recent durable messages |
| `explain_stage` | Static MaxRead workflow-stage documentation |

## Access boundary

- Guest: only the current `guest:<public_id>` conversation and web jobs.
- Bound user: jobs and conversation associated with that Feishu `open_id`
  across web and Feishu channels.
- Admin overlay: the selected user's exact scope, with `actor_type=admin` kept
  on new messages. Admin status does not widen the agent's read tools.

The agent cannot access raw SQL, filesystem paths, source archives, screenshots,
API keys, cookies, other users, the global queue, admin mutations, or arbitrary
network requests.

## Write boundary

The model has no state-changing tools. It cannot submit, retry, cancel, delete,
or edit a task. Submission is handled by the deterministic link parser. Retry
is exposed only as a button on a failed task card and validated server-side
against the effective identity. Both paths retain the existing queue and audit
semantics.

Pet questions and answers are appended to `web_messages` as `pet_user` and
`pet_reply`, so they survive refreshes and participate in the same persistent
conversation.
