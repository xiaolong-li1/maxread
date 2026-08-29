# MaxRead Aliyun maintainer handoff

Last verified: 2026-08-29, Asia/Shanghai.

This is the first document a new maintainer or a new ChatGPT task should read.
It describes production facts, not a proposed deployment. Never paste runtime
secrets into chat, Git, logs, screenshots, or issue trackers.

## 1. Source of truth and hosts

| Item | Current value |
| --- | --- |
| GitHub | `https://github.com/xiaolong-li1/maxread`, branch `main` |
| Primary local checkout | `/Users/xiaolong/projects/maxread` |
| Aliyun SSH | `root@47.103.111.28` |
| Local SSH key | `/Users/xiaolong/projects/aliyun-mechine/deploy_key` (never commit) |
| Production code | `/opt/maxread` |
| Production OS | Ubuntu 24.04, Python 3.12.3 |
| Public site | `https://xiaolong-dev.me/maxread/` |
| Personal projects | `/maxread/`, `/maxread/projects`, `/maxread/submit` |
| Admin console | `/maxread/admin` (server-side admin session required) |

`/opt/maxread` is a deployment tree, not the authoritative Git checkout. Code
changes must be made and tested in the Git repository, committed to `main`, and
then deployed deliberately. Do not overwrite `.env`, SQLite files, OAuth
tokens, `~/.lark-cli`, `/opt/maxread-duty`, or runtime work directories.

## 2. Three business services

There are three business services, although systemd shows four long-running
units because the paper service has a listener and a web process.

### A. Paper reading: 读不动了 / MaxRead

Systemd units:

- `maxread.service`: the **only** Feishu event listener and the durable queue's
  two workers. Starts `/opt/maxread/run-listener.sh`.
- `maxread-admin.service`: personal project UI, admin UI and scoped web APIs on
  `127.0.0.1:8765`. Starts `/opt/maxread/run-admin.sh`.
- `maxread-cache-cleanup.timer`: daily at 03:40 Asia/Shanghai; invokes the
  one-shot cache cleanup service.

Main files:

- `/opt/maxread/maxread/`: application package.
- `/opt/maxread/maxread.sqlite3`: authoritative SQLite database.
- `/opt/maxread/.env`: runtime configuration and model credentials, mode 600.
- `/opt/maxread/var/maxread/`: source bundles and pipeline artifacts.
- `/opt/maxread-browser/`: Feishu PDF export and visual-QA runner state.
- `/root/.lark-cli/`: bot identity and Feishu authorization.

Request flow:

```text
Feishu event or scoped web submission
  -> canonical arXiv ID/source parser
  -> usage event + watcher + durable queue job
  -> worker lease and heartbeat
  -> metadata + TeX source (PDF fallback only when needed)
  -> parallel figure rendering/inspection and sectional generation
  -> editorial review
  -> deterministic formula/table/format compile gate and bounded repair
  -> Feishu document write and image insertion
  -> fetched Docx verification
  -> exported-PDF visual QA
  -> completed document, or an audited retryable terminal
```

`queue_jobs.workflow_state` is canonical. `stage` is the UI projection.
`job_events` is the first place to answer “why did it stop or retry?”. Workers
must never share a SQLite connection; every thread/request owns a `Store`.
Heartbeat and lease recovery are bounded, and published checkpoints prevent a
visual retry from blindly creating a second document.

Current non-secret production settings:

```dotenv
MAXREAD_MODEL=gpt-5.6-sol
MAXREAD_OPENAI_API_MODE=responses
OPENAI_SUB_MODULE=codex-internal
MAXREAD_DB=/opt/maxread/maxread.sqlite3
MAXREAD_WORKDIR=/opt/maxread/var/maxread
MAXREAD_REQUIRE_SOURCE=true
MAXREAD_QUEUE_WORKERS=2
MAXREAD_LLM_CONCURRENCY=2
MAXREAD_SECTIONAL_GENERATION_ENABLED=true
MAXREAD_SECTIONAL_GENERATION_WORKERS=2
MAXREAD_FIGURE_RENDER_WORKERS=2
MAXREAD_FIGURE_VISION_WORKERS=2
MAXREAD_FEISHU_CONCURRENCY=1
MAXREAD_VISUAL_QA_ENABLED=true
MAXREAD_VISUAL_QA_EXPORT_PDF=true
MAXREAD_VISUAL_QA_CONCURRENCY=1
MAXREAD_MAX_RENDERED_IMAGE_BYTES=10485760
MAXREAD_MAX_RENDERED_IMAGE_PIXELS=16000000
MAXREAD_MAX_RENDERED_IMAGE_SIDE=3200
MAXREAD_MAX_IMAGE_DISPLAY_HEIGHT=560
```

The project UI is identity-scoped. A sliding one-year HttpOnly cookie identifies
a browser; Feishu binding maps it to a real `open_id`. Each paper is one project
card updated in place. Favorite, delete and manual category are per identity.
Automatic category uses the paper title plus MaxRead's generated one-sentence
summary; manual category always wins. Deleting hides the user's project and
only cancels a queued job when that web identity is its sole watcher.

Max is a project-scoped diagnostic agent, not a global chatbot. It may inspect
the selected project's events, heartbeat and compact artifact diagnostics. An
explicit repair request may retry an owned failed job or recover that one job
after its heartbeat is proven stale. It cannot read secrets, inspect another
user, run shell commands, restart services, delete projects, or mutate arbitrary
data. Its chat stays browser-side and is not added to the formal project log.

### B. ZIP Lab recruiting mailbox pipeline

Systemd units:

- `recruiting-pipeline.service`: persistent incremental mailbox processor.
- `recruiting-weekly-report.timer`: Monday 07:00 Asia/Shanghai; invokes a
  one-shot, idempotent weekly report.

Files:

- Code: `/opt/maxread/features/mail_ingestion/`.
- Private account config:
  `/opt/maxread/features/mail_ingestion/data/accounts/zip-lab.env`.
- OAuth token cache:
  `/opt/maxread/features/mail_ingestion/data/accounts/zip-lab/outlook-token.json`.
- SQLite:
  `/opt/maxread/features/mail_ingestion/data/accounts/zip-lab/mail_collector.sqlite3`.

Flow:

```text
read-only incremental IMAP scan
  -> MIME attachment and thread reconstruction
  -> local PDF text extraction
  -> candidate/other classification and structured evidence extraction
  -> candidate document create/update
  -> Feishu Base upsert
  -> run/message/thread audit and bounded retry
```

Candidate and “other” mail must both remain visible so classification mistakes
can be found. Follow-ups merge into the existing thread; outgoing lab replies
must not become new candidates. GPA/rank evidence is extracted from email and
resume PDF, while human screening status always has precedence. Manual sends or
test sends require the owner's permission; do not use a real chat as a dry run.
Base also stores standalone `院校` and `排名` fields plus `是否985`、`是否C9`、
`是否已回复`. AI extracts the school/evidence, official deterministic lists
assign the institution tags, and outgoing thread direction determines reply
state. Use `tag-records --confirm` for an audited full refresh.

### C. Daily duty reminder

Systemd unit:

- `maxread-duty-reminder.service`: independent daemon under
  `/opt/maxread-duty`.

Files:

- `/opt/maxread-duty/duty_reminder.py`
- `/opt/maxread-duty/duty-reminder.json` (private roster/chat configuration)
- `/opt/maxread-duty/formal-duty-reminder.sqlite3` (idempotency state)

It polls locally and sends once at 07:00 Asia/Shanghai. The SQLite record makes
restarts idempotent. Never run a second copy, never reset the state database to
“test”, and never manually send a duty message without explicit permission.

## 3. Public web and admin boundary

Nginx terminates TLS for `xiaolong-dev.me` and proxies `/maxread/` to
`127.0.0.1:8765`. The root path is the personal project console. The old public
global dashboard has been replaced; global usage, users, feedback, jobs, logs,
reviews, service status and architecture APIs require the admin session.

Public write endpoints are narrowly allowlisted by Nginx:

- web submit and binding code;
- project retry and project action (favorite/category/delete);
- scoped Max project chat;
- admin login/logout.

All other POSTs are denied before reaching Python. Any new public endpoint must
have server-side identity validation, a rate limit, tests, and an explicit
Nginx allowlist entry. Hiding a button is not authorization.

## 4. Data and retention

Main database tables are documented in `docs/database-schema.md`. Important
groups are:

- `papers`, `documents`: source and final document index.
- `queue_jobs`, `job_watchers`, `job_events`: durable workflow and audit.
- `usage_events`, `feedback`, `review_issues`: observability and feedback.
- `web_identities`, `web_project_preferences`: scoped web identity and project
  organization.

Artifacts live outside SQLite:

```text
/opt/maxread/var/maxread/papers/<arxiv-id>/pipeline_artifacts/
/opt/maxread-browser/runs/
```

After successful delivery, TeX/PDF downloads, extracted source and rendered
figures are rebuildable and removed. `pipeline_artifacts` is retained because
it powers no-regression retry and diagnostics. The daily timer removes missed
rebuildable caches older than one hour. Before deleting anything manually,
check that no related job is queued/running and keep a database backup.

## 5. arXiv egress and ZeroTier

Aliyun direct access to arXiv/Fastly was unstable. Only `ArxivClient` uses:

```dotenv
MAXREAD_ARXIV_PROXY_URL=http://127.0.0.1:17890
MAXREAD_ARXIV_PROXY_REQUIRED=true
MAXREAD_ARXIV_PARALLEL_STREAMS=2
```

`127.0.0.1:17890` is an SSH reverse-forward listener owned by `sshd`. The
egress host runs user services `maxread-egress.service` (Mihomo on loopback)
and `maxread-egress-tunnel.service` (persistent reverse SSH tunnel). The proxy
is not public and does not alter Aliyun's default route. If it disappears,
arXiv fails quickly while Feishu/OpenAI/SSH continue direct.

Aliyun currently has **no ZeroTier client and no ZeroTier address**. The old
`ziplab-5090` address `10.214.232.45` belongs to the retired 5090 deployment and
must not be used as the paper listener, admin backend or visual-QA host.
ZeroTier may still be useful from a maintainer laptop to reach old/internal
machines, but it is not in the Aliyun production data path. The arXiv reverse
tunnel is SSH-based, not ZeroTier-based.

Check egress without touching global networking:

```bash
ss -lntp | grep 127.0.0.1:17890
curl --proxy http://127.0.0.1:17890 -L --max-time 60 -o /dev/null \
  -w 'code=%{http_code} speed=%{speed_download} total=%{time_total}\n' \
  https://arxiv.org/e-print/2608.25927
```

Do not install a global VPN, set process-wide `HTTP_PROXY`, expose port 17890,
or switch back to WARP local-proxy mode without a measured migration plan.

## 6. Secrets and machine state

Required runtime secrets are intentionally absent from Git:

- `/opt/maxread/.env`: model gateway and visual-model keys.
- `/root/.lark-cli/`: Feishu app credentials/auth state.
- recruiting account env and Outlook token cache.
- duty reminder JSON and its chat ID.
- egress host proxy nodes/controller secret and tunnel private key.
- local Aliyun `deploy_key`.

When another machine or ChatGPT takes over, copy secrets through an approved
secure channel, preserving permissions. Do not ask the model to print them.
Validate presence using key names, file modes and service smoke tests instead
of echoing values.

## 7. Safe deployment workflow

1. Pull `main` and read `git status`; never discard unrelated user changes.
2. Run the complete test suite locally: `.venv/bin/pytest -q`.
3. Inspect production queue before restart:

   ```bash
   sqlite3 -readonly /opt/maxread/maxread.sqlite3 \
     "select status,count(*) from queue_jobs where status in ('queued','running') group by status;"
   ```

4. Back up SQLite with `.backup` and copy changed runtime files to a timestamped
   directory. Never upload local `.env` or local SQLite over production.
5. Run `py_compile` on changed Python modules.
6. Restart only affected units. UI/API/db method changes need
   `maxread-admin.service`; queue/pipeline/db changes also need
   `maxread.service`. Wait for an idle window when possible.
7. Check systemd, logs, DB integrity and public GET routes. Nginx changes require
   `nginx -t` before reload.
8. Commit and push the exact tested source to GitHub.

Do not start a second Feishu listener on 5090 or a laptop. It can consume the
same event and cause duplicate acknowledgements/documents.

## 8. Routine health checks

```bash
systemctl is-active maxread.service maxread-admin.service \
  recruiting-pipeline.service maxread-duty-reminder.service
systemctl list-timers --all | grep -E 'maxread|recruiting'
journalctl -u maxread.service -u maxread-admin.service -n 100 --no-pager
journalctl -u recruiting-pipeline.service -n 100 --no-pager
journalctl -u maxread-duty-reminder.service -n 30 --no-pager
sqlite3 /opt/maxread/maxread.sqlite3 'pragma integrity_check;'
curl -fsS https://xiaolong-dev.me/maxread/projects >/dev/null
```

Visual QA dependencies currently include `pdftotext`, PyMuPDF, pypdf, Pillow,
Playwright and `/opt/maxread-browser/run_visual_qa.sh`. An export ticket still
processing is an infrastructure-pending state, not proof of a bad document.

## 9. How to add features without destabilizing production

- Extend the durable state machine and audit event before adding UI-only state.
- Keep deterministic parsers/compilers for formulas, figures and source paths;
  use models for semantic judgment, not for syntax that a parser can verify.
- Every repair must return to the gate that detected the issue and preserve
  previous diagnostics as a no-regression checklist.
- Parallelize independent paper sections/figures, but keep Feishu document
  writes and same-thread recruiting updates serialized.
- New project-agent tools must be identity-scoped, narrow, auditable and safe
  under repeated calls. Never expose generic SQL, shell, filesystem or restart
  tools to the public agent.
- Add regression tests from every real failure case. Before deployment, test
  desktop/mobile layout and verify that formulas, images and tables are judged
  from rendered output rather than guessed counts.
- Sending Feishu messages is a real side effect. Obtain permission before any
  manual test send; prefer web-only watchers and dry runs.

Related documents:

- `docs/workflow-state-machine.md`
- `docs/database-schema.md`
- `docs/operations-and-migration.md`
- `docs/arxiv-egress.md`
- `docs/web-submit.md`
- `docs/web-pet-agent.md`
- `features/mail_ingestion/docs/recruiting-pipeline.md`
