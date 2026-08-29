# Web pet agent contract

The green MaxRead companion is a bounded task agent, not a decorative chatbot
and not a second pipeline coordinator.

## Identity and role

The companion is a calm, concise task partner. It explains what MaxRead is
doing, reports one selected paper project's status, helps interpret workflow
stages, and provides light conversation while the user waits. Every project
card owns its companion context; there is no global pet context.

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
| `get_project` | The selected owned `job_id` or paper source; foreign projects return a scoped error |
| `inspect_project` | Scoped job events, heartbeat age and compact quality/artifact diagnostics |
| `explain_stage` | Static MaxRead workflow-stage documentation |
| `retry_project` | Retry the selected failed project after an explicit user request |
| `recover_stale_project` | Recover only the selected project after its heartbeat is proven stale |

## Access boundary

- Guest: only web jobs submitted by the current `guest:<public_id>` identity.
- Bound user: projects associated with that Feishu `open_id` across web and
  Feishu channels.
- Admin overlay: the selected user's exact scope, with `actor_type=admin` kept
  on new messages. Admin status does not widen the agent's read tools.

The agent cannot access raw SQL, filesystem paths, source archives, screenshots,
API keys, cookies, other users, the global queue, admin mutations, or arbitrary
network requests.

## Write boundary

The model cannot submit, cancel, delete, edit arbitrary data, restart services,
or touch another project. Two narrowly scoped state-changing tools are
available: retrying an owned failed project and recovering an owned running
project whose heartbeat has already exceeded the configured stale threshold.
Both require explicit repair intent in the current user message, revalidate
ownership server-side, and write normal queue audit events. A healthy worker is
never interrupted just because a stage has taken longer than usual.

Pet questions and answers stay in the project's browser-side chat panel and
are sent only as bounded context for the current request. They are not written
to `web_messages`, do not appear in the project timeline, and disappear when
the page session ends. Durable storage contains project state and audit events,
not companion small talk.
