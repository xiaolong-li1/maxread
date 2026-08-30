# 读不动了 / MaxRead

Send an arXiv ID/link, HuggingFace Papers link, `papers.cool/arxiv/<id>` mirror link, or supported web article URL to the Feishu bot, and MaxRead creates a public-readable Feishu docx summary.


## Deploy / Migrate to Another Machine

The GitHub repo contains code and deployment automation only. Real keys stay in a local env file that is never committed.

For Windows migration, start with the dedicated guide:

```text
deploy/windows/README.md
```

1. On the target machine, create a local key file, for example `~/maxread.env`:

```bash
OPENAI_API_KEY=<openai-api-key>
OPENAI_BASE_URL=https://api.openai.com/v1
MAXREAD_OPENAI_API_MODE=responses
MAXREAD_MODEL=gpt-5.5
MAXREAD_FEISHU_AS=bot
MAXREAD_LARK_CLI=lark-cli
MAXREAD_QUEUE_WORKERS=2
MAXREAD_LLM_CONCURRENCY=2
MAXREAD_FEISHU_CONCURRENCY=1

# Only needed when bootstrapping from the private GitHub repo over HTTPS.
MAXREAD_GITHUB_TOKEN=<github-token-with-private-repo-read-access>
```

2. Install from an existing checkout:

```bash
bash deploy/install.sh
```

The script asks for the deploy directory and local key file path, then creates `.env`, a Python venv, runtime directories, and user services when available.

For paper figure rendering, install Ghostscript (`gs`) for EPS/PS assets and
Poppler (`pdftoppm`) for PDF assets. MaxRead also discovers a user-local
Ghostscript bundle at `~/.local/share/maxread-tools/ghostscript`; set
`MAXREAD_GHOSTSCRIPT_ROOT` when that bundle lives elsewhere. A paper that
contains source figures but has no working renderer is blocked before model
generation instead of silently publishing a zero-image document.

The paper-reading listener and admin UI are independent from the duty reminder. On a machine that should only run MaxRead, leave `maxread-duty-reminder` stopped:

```bash
systemctl --user disable --now maxread-duty-reminder.service
systemctl --user enable --now maxread.service maxread-admin.service
```

On Linux, the user services are `maxread.service` and `maxread-admin.service`. The listener must be the only active `im.message.receive_v1` consumer for the configured Feishu app; do not run a second listener on another machine with the same app.

3. For a fresh Linux/macOS machine with no checkout yet, copy or download `deploy/bootstrap.sh` and run:

```bash
bash bootstrap.sh
```

`bootstrap.sh` reads `MAXREAD_GITHUB_TOKEN` from the environment or from the local key file, clones the private repo without storing the token in `git remote`, then runs `deploy/install.sh`.

After deployment, verify Feishu auth on that machine:

```bash
lark-cli doctor
```

If Feishu auth is missing, run the normal `lark-cli auth login` flow once on the target machine.

## Setup

For a complete production inventory, secret-handling policy, database schema, and
Aliyun migration runbook, see
[`docs/operations-and-migration.md`](docs/operations-and-migration.md) and
[`docs/database-schema.md`](docs/database-schema.md). The repository never
contains the real model key, Feishu app secret, lark-cli auth state, database,
paper cache, or SSH key.

1. Make sure `lark-cli doctor` is `ok: true`.
2. Copy `.env.example` to `.env` and set `OPENAI_API_KEY` for real summaries.
3. The Feishu bot must have docx write scopes, including `docs:document.media:upload` for inline paper figures. Without that scope, text and formulas can be written but image insertion will fail.
4. Run from this directory:

```bash
python3 -m maxread.cli extract 'Minimax paper https://arxiv.org/pdf/2506.13585.pdf and 2511.19416'
```

## Run One Paper

```bash
python3 -m maxread.cli process 'https://arxiv.org/abs/2604.12946'
python3 -m maxread.cli process 'https://huggingface.co/papers/2605.18739'
python3 -m maxread.cli process 'https://nrehiew.github.io/blog/sft_rl_opd/'
```

Use `--no-openai` to test the Feishu document flow with fallback content.
If arXiv returns HTTP 429, wait a few minutes before retrying. MaxRead stores failed attempts in SQLite but only treats `done` papers as cache hits.

Large arXiv source downloads use two bounded Range streams by default. This avoids long single-connection stalls while keeping request concurrency conservative; do not increase it without measuring the target network and respecting arXiv rate limits.

For hosts whose direct route to arXiv is unstable, configure the
application-scoped egress described in
[`docs/arxiv-egress.md`](docs/arxiv-egress.md). Do not use global proxy
environment variables or alter the machine default route.

By default `MAXREAD_REQUIRE_SOURCE=true`, so MaxRead will not generate a full document unless TeX source is available. This keeps formula/image/table alignment reliable.

If you can download source from the arXiv web page manually, import it first:

```bash
python3 -m maxread.cli import-source 2604.12946 /path/to/arxiv-source.tar
python3 -m maxread.cli process 'https://arxiv.org/abs/2604.12946'
```

## Listen to Feishu

```bash
python3 -m maxread.cli listen
```

The listener ignores ordinary messages with no supported input. It replies with a short intro/help message when users send help keywords such as `帮助`, `怎么用`, `你是谁`, `读不动了`, or `MaxRead`. Configure `MAXREAD_FEEDBACK_URL` to include a Feishu feedback doc link in that intro.

## Web Submission

The admin HTTP service also serves a compact paper submission and persistent
conversation page at `/submit`. Visitors start as durable guests and can bind
their real Feishu account with a one-time private-chat code; bound web and
Feishu submissions share identity, queue results, and conversation history.
See [`docs/web-submit.md`](docs/web-submit.md) for the data model, administrator
overlay, rate limits, and Nginx boundary.

## Quality Gates and Visual QA

MaxRead has four distinct quality gates: (1) the generation contract checks that the model returned a complete document; (2) one editorial review checks facts, context, method completeness, and figure meaning; (3) the compile gate checks Markdown and Docx XML formulas, tables, and formatting; (4) the delivery gate checks the fetched Feishu document and the real browser-rendered page. The gates do not repeat each other's work: the editor does not act as a compiler, and visual QA does not re-generate the paper.

Before publishing, MaxRead sanitizes LaTeX/Markdown formatting, checks required paper sections and figure references, and blocks documents with high-severity formula or raw-formatting errors. A blocked document is sent back to the review model with the exact quality findings and re-rendered for at most `MAXREAD_QUALITY_REPAIR_ROUNDS` repair rounds (default `2`). A no-change repair stops immediately. Every Markdown, XML, quality report, and model response is saved under `pipeline_artifacts`.

Paper generation is also a bounded state-machine loop: each output enters `generation_checking`, deterministic cleanup runs first, and a failed check enters `generation_repairing` with the previous output and exact errors included in the next model prompt. `MAXREAD_GENERATION_REPAIR_ROUNDS` controls model repair rounds (default `1`, two total generation opportunities). Exhausting the budget enters the retryable `generation_incomplete` terminal state without publishing.

The durable state machine keeps those internal states for audit, but the architecture page projects them into ten business nodes: preparation, generation gate, editorial review, compile-quality gate, publishing, delivery gate, one recoverable-failure terminal, and completion. Check/repair/recheck is drawn as a self-loop on the relevant gate instead of three duplicate business nodes. A failed model, network, or browser attempt is automatically replayed once by default (`MAXREAD_AUTO_RETRY_ATTEMPTS=1`); deterministic source/quality failures still wait for an explicit retry, and partial Feishu writes are never blindly replayed without a publish checkpoint.

Retries are durable across queue jobs. MaxRead reloads the latest failed draft plus the generation, pre-publish quality, and visual QA ledgers from `pipeline_artifacts`; the repair prompt treats those diagnostics as a no-regression checklist. In a Feishu topic, a user can reply `重试` to the failure message (or `重试 2608.10416`) without reposting the original link or mentioning the bot again. Queue acknowledgements estimate start and completion time from the median duration of recent successful jobs of the same source kind instead of a fixed per-batch constant.

An optional delivery worker can inspect the final Feishu rendering for invalid formulas, leaked formatting commands, abnormal blank pages, and other visible failures. The preferred production path exports the Docx through Feishu's server-side PDF renderer and inspects every selected PDF page with Poppler; Playwright remains a fallback. Enable it only after the bot has `drive:export:readonly` and `docs:document:export` and a smoke test passes:

```bash
MAXREAD_VISUAL_QA_ENABLED=true
MAXREAD_VISUAL_QA_HOST=localhost
MAXREAD_VISUAL_QA_RUNNER=/opt/maxread/deploy/visual_qa/run_visual_qa.sh
MAXREAD_VISUAL_QA_REMOTE_ROOT=/opt/maxread-browser
MAXREAD_VISUAL_QA_EXPORT_PDF=true
MAXREAD_VISUAL_QA_RUNNER_TIMEOUT=120
MAXREAD_VISUAL_QA_INSPECT_RETRIES=2
MAXREAD_VISUAL_QA_REPAIR_ROUNDS=2
```

The delivery worker is isolated; the coordinator changes only explicitly identified Feishu blocks. Formula/image/table counts are telemetry, not acceptance thresholds: delivery is blocked only by concrete visible failures in the exported rendering. After a successful inspection, MaxRead performs at most two inspect -> targeted repair -> inspect cycles. Each attempt and finding is saved as `09-visual-qa.json`; successful runs delete exported PDFs and page PNGs, while failures retain evidence for diagnosis.

Set `MAXREAD_OPENAI_API_MODE=chat` for OpenAI-compatible gateways whose
`/chat/completions` implementation follows instructions more reliably than
their `/responses` implementation. If that gateway cannot accept local base64
screenshots, keep visual repair on a separate compatible model with
`MAXREAD_VISUAL_OPENAI_API_KEY`, `MAXREAD_VISUAL_OPENAI_BASE_URL`,
`MAXREAD_VISUAL_OPENAI_API_MODE`, and `MAXREAD_VISUAL_MODEL`.

## Current Scope

- Supported: arXiv ID, `arxiv.org/abs`, `arxiv.org/pdf`.
- Supported: `huggingface.co/papers/<arxiv-id>`; this maps to the arXiv paper pipeline.
- Supported: `papers.cool/arxiv/<arxiv-id>`; this is canonicalized to `arxiv.org/abs/<id>` and never treated as a generic webpage.
- Supported: ordinary HTML web articles; MaxRead extracts title, text, images, captions, tables, code, and formulas into an article-style Feishu doc.
- Supported fallback: manually imported arXiv source packages.
- Not yet supported: uploaded PDF files, arbitrary PDF URLs, WeChat links, Zhihu integration.
- Repeated paper IDs return the cached Feishu doc URL only while the source
  record remains `done`. Records marked `legacy` are rebuilt on the next request.

## Cache Lifecycle

After a document reaches the accepted `completed` terminal state, MaxRead keeps
the SQLite result, Feishu document URL, and compact `pipeline_artifacts`, then
removes rebuildable PDF, TeX source, extracted source trees, and rendered images.
The daily `maxread-cache-cleanup.timer` catches leftovers from interrupted
workers. Operators can preview or run cleanup manually:

```bash
python3 -m maxread.cli cache-cleanup --older-than-hours 1 --dry-run
python3 -m maxread.cli cache-cleanup --older-than-hours 1
```

Invalidate an older output generation without deleting its Feishu document or
history by marking completed records before a local date as `legacy`:

```bash
python3 -m maxread.cli invalidate-cache --before 2026-08-28 --timezone Asia/Shanghai
```

## Tests

Image publishing and figure composition use Pillow, which is declared in `pyproject.toml`. Use an isolated environment before running tests:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m compileall maxread tests
.venv/bin/python tests/run_tests.py
```

If `pytest` is installed, `python3 -m pytest` also works.

## Production handoff

The current Aliyun topology, three business services, secret boundaries,
ZeroTier status, arXiv egress, databases, deployment checklist and feature
extension rules are documented in
[`docs/aliyun-maintainer-handoff.md`](docs/aliyun-maintainer-handoff.md).
Read it before operating production or handing the project to another agent.

## Usage Audit

MaxRead stores private/group sender open_id, input link, status, generated doc URL, and timestamps in local SQLite. Recent records:

```bash
python3 -m maxread.cli usage --limit 50
python3 -m maxread.cli usage --limit 50 --resolve-users
```

## Feedback

Private-chat messages without supported links are stored as feedback in local SQLite. Recent records:

```bash
python3 -m maxread.cli feedback --limit 50
python3 -m maxread.cli feedback --limit 50 --resolve-users
```

## Recruiting Mail Ingestion

`features/mail_ingestion` is an independent, read-only IMAP collector and recruiting pipeline. It keeps mailbox credentials, raw messages, attachments, OAuth state, and SQLite data outside Git, while syncing candidate summaries and material documents to Feishu. See [`features/mail_ingestion/README.md`](features/mail_ingestion/README.md) for account setup, dry-run, backfill, weekly reporting, and systemd deployment.

## Global Queue

`listen` enqueues supported Feishu messages and starts role-filtered background workers. In production Aliyun claims `article` only, while the authenticated 5090 remote worker claims `paper`; Aliyun remains the single database and notification owner. See [`docs/remote-paper-worker.md`](docs/remote-paper-worker.md) for the deployed split, tunnel, recovery and rollback procedure.

The durable queue lifecycle is modeled as a state machine with audited transitions, bounded generation/quality/visual repair loops, worker leases, and publish checkpoints. See [`docs/workflow-state-machine.md`](docs/workflow-state-machine.md) for the lifecycle and recovery invariants. The admin server exposes the live executable specification at `/architecture` and `/api/workflow-spec`; both are generated from `maxread/workflow.py` instead of a second handwritten state graph.

```bash
python3 -m maxread.cli listen
python3 -m maxread.cli worker
python3 -m maxread.cli jobs --limit 50
python3 -m maxread.cli jobs --status queued
python3 -m maxread.cli job-stats
python3 -m maxread.cli job-events --job-id 1
python3 -m maxread.cli retry-job 1
```

Queue settings:

```bash
MAXREAD_QUEUE_WORKERS=2
MAXREAD_QUEUE_SOURCE_KINDS=paper,article
MAXREAD_QUEUE_STALE_MINUTES=30
MAXREAD_QUEUE_HEARTBEAT_SECONDS=15
MAXREAD_LLM_CONCURRENCY=5
MAXREAD_FEISHU_CONCURRENCY=1
```

## Independent Duty Reminder

The duty reminder is separate from the paper-reading queue. It rotates through a configured roster and posts once per day to a fixed Feishu group at 07:00 Asia/Shanghai by default. Configure the target group with its `oc_...` chat ID, then set the roster:

```bash
python3 -m maxread.cli duty set \
  --member '张三' \
  --member '李四' \
  --member '王五'
python3 -m maxread.cli duty list
python3 -m maxread.cli duty today
python3 -m maxread.cli duty send --dry-run
python3 -m maxread.cli duty history
```

Set `MAXREAD_DUTY_CHAT_ID=oc_...` in `.env` only when this deployment owns the duty module. The deployment installer writes the independent unit but does not start it. Enable it explicitly only on the designated duty machine. It records each date's result in SQLite, so restarts do not duplicate a successful reminder; a failed reminder remains retryable on the next poll.
