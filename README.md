# 读不动了 / MaxRead

Local MVP: send an arXiv ID/link, HuggingFace Papers link, or supported web article URL to the Feishu bot, and MaxRead creates a public-readable Feishu docx summary.


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
MAXREAD_MODEL=gpt-5.5
MAXREAD_FEISHU_AS=bot
MAXREAD_LARK_CLI=lark-cli
MAXREAD_QUEUE_WORKERS=5
MAXREAD_LLM_CONCURRENCY=5
MAXREAD_FEISHU_CONCURRENCY=3

# Only needed when bootstrapping from the private GitHub repo over HTTPS.
MAXREAD_GITHUB_TOKEN=<github-token-with-private-repo-read-access>
```

2. Install from an existing checkout:

```bash
bash deploy/install.sh
```

The script asks for the deploy directory and local key file path, then creates `.env`, a Python venv, runtime directories, and user services when available.

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

Large arXiv PDF/source downloads use conservative Range parallelism by default: `MAXREAD_ARXIV_PARALLEL_STREAMS=4` for files at least `MAXREAD_ARXIV_PARALLEL_MIN_BYTES=1048576`. On a stable proxy this can be raised to `8`; avoid `16` unless the proxy is known to tolerate many parallel Range requests.

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

## Current Scope

- Supported: arXiv ID, `arxiv.org/abs`, `arxiv.org/pdf`.
- Supported: `huggingface.co/papers/<arxiv-id>`; this maps to the arXiv paper pipeline.
- Supported: ordinary HTML web articles; MaxRead extracts title, text, images, captions, tables, code, and formulas into an article-style Feishu doc.
- Supported fallback: manually imported arXiv source packages.
- Not yet supported: uploaded PDF files, arbitrary PDF URLs, WeChat links, Zhihu integration.
- Repeated paper IDs return the cached Feishu doc URL.

## Tests

This project has no required third-party test dependency. Run:

```bash
python3 -m compileall maxread tests
python3 tests/run_tests.py
```

If `pytest` is installed, `python3 -m pytest` also works.

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

## Global Queue

`listen` now enqueues supported Feishu messages and starts background worker threads in the same process. This keeps the event listener responsive while papers are processed concurrently.

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
MAXREAD_QUEUE_WORKERS=3
MAXREAD_QUEUE_STALE_MINUTES=30
MAXREAD_QUEUE_HEARTBEAT_SECONDS=15
MAXREAD_LLM_CONCURRENCY=2
MAXREAD_FEISHU_CONCURRENCY=1
```
