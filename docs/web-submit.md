# Web submission and persistent identity

MaxRead exposes a public paper submission surface at `/submit` on the admin
HTTP service. In production Nginx publishes it as `/maxread/submit` while the
control panel remains `/maxread/`.

## Identity model

The first visit receives an HttpOnly, SameSite cookie and creates a durable
`web_identities` row with `account_type=guest`. Usage is recorded under a
stable `guest:web_<id>` sender, so anonymous traffic remains visible and
distinct in the admin statistics.

A guest can request a six-digit, ten-minute binding code. Sending
`绑定 <code>` in a private chat with the Feishu bot proves ownership of the
real Feishu sender `open_id`. Binding then:

1. upgrades the web identity to `account_type=feishu`;
2. moves prior guest web usage and watchers to the Feishu `open_id`;
3. merges the guest conversation into the canonical
   `feishu:<open_id>` conversation;
4. lets another browser bound to the same Feishu account see the same durable
   conversation.

Users never type or select an `ou_xxx` identifier in the public UI.

## Conversation bridge

`web_conversations` owns an ordered `web_messages` stream. Each message stores
its role, channel, kind, source/job identifiers, status, document URL, and the
actual actor.

- A web submission writes the user message and queue acknowledgement.
- Queue stage changes update the acknowledgement.
- Success or failure appends the same terminal response used by the Feishu
  workflow, including the document link when available.
- Once an account is bound, future Feishu submissions and bot queue/final
  replies are mirrored into the same web conversation.
- Web watchers update SQLite only; they never call a Feishu reply API with a
  synthetic message ID.

## Administrator overlay

An existing admin session may send `X-MaxRead-Act-As: <web public id>` from the
web UI. The server verifies the admin cookie before resolving the target
identity. The user's identity remains the conversation owner, while newly
submitted user-role messages record `actor_type=admin` and `actor_id=admin`.
This preserves both layers instead of silently impersonating the user.

## Public boundary

- Public POST endpoints are limited to `/api/web/submit` and
  `/api/web/binding-code`.
- Inputs are parsed by the existing canonical arXiv source parser and reuse the
  durable global queue and deduplication logic.
- A session may submit at most ten papers per ten minutes; the HTTP server also
  limits each real client IP to eight requests per ten minutes.
- One request accepts at most five papers.
- Admin account enumeration and overlay require a valid server-side admin
  session.
- Session cookies are HttpOnly and become Secure behind HTTPS.

When publishing behind the existing `/maxread/` Nginx prefix, explicitly allow
POST for the two public endpoints; the general control-panel location remains
GET-only.
