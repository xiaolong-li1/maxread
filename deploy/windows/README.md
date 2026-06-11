# MaxRead Windows Migration Guide

This folder is a Windows-oriented migration plan and minimal runtime scaffold. It does not contain real keys, Feishu/Lark login state, SQLite data, or cached articles. Keep those in a local folder outside Git, for example `C:\MaxReadLocal`.

## Target Layout

Recommended Windows layout:

```text
C:\MaxRead\
  maxread source checkout
  .venv\
  .env                  copied from C:\MaxReadLocal\maxread.env
  maxread.sqlite3       optional migrated local database
  var\maxread\          optional migrated cache/work files

C:\MaxReadLocal\
  maxread.env           real API keys and runtime config
  lark-cli\             optional copied lark-cli auth/config backup
```

The GitHub repo should only hold source code, examples, and scripts. Do not commit `C:\MaxRead\.env`, `maxread.sqlite3`, or `var\`.

## Model Configuration

Create `C:\MaxReadLocal\maxread.env` from `env.windows.example` and fill real values:

```powershell
Copy-Item .\deploy\windows\env.windows.example C:\MaxReadLocal\maxread.env
notepad C:\MaxReadLocal\maxread.env
```

Minimum model settings:

```text
OPENAI_API_KEY=...
OPENAI_BASE_URL=https://api.openai.com/v1
MAXREAD_MODEL=gpt-5.5
MAXREAD_LLM_CONCURRENCY=5
MAXREAD_FEISHU_CONCURRENCY=3
```

If you use another OpenAI-compatible provider, keep `OPENAI_API_KEY`, change `OPENAI_BASE_URL`, and set `MAXREAD_MODEL` to the provider model name.

## Feishu/Lark Authentication

MaxRead uses `lark-cli` for Feishu access. Do not upload `lark-cli` config, app secrets, OAuth tokens, or copied auth state to GitHub. Keep them on the Windows machine only.

The safest Windows migration path is to initialize `lark-cli` on Windows and verify it there:

```powershell
lark-cli config init --new
lark-cli doctor
```

Set this in `C:\MaxReadLocal\maxread.env` if `lark-cli` is not on `PATH`:

```text
MAXREAD_LARK_CLI=C:\path\to\lark-cli.exe
MAXREAD_FEISHU_AS=bot
```

Identity notes:

- `MAXREAD_FEISHU_AS=bot` uses app/bot credentials from `lark-cli config init`. Do not run user OAuth just to fix bot permissions.
- `MAXREAD_FEISHU_AS=user` requires user authorization. Use `lark-cli auth login` on Windows, then verify with `lark-cli auth status`.
- If a command reports missing scopes, enable the scope in the Feishu developer console for bot identity; for user identity, run `lark-cli auth login` again with the required scope.

Useful verification commands:

```powershell
lark-cli config show
lark-cli doctor
lark-cli auth status --as user
lark-cli docs --help
lark-cli event --help
```

Do not commit copied Feishu auth files. If you must copy auth state from macOS, copy it only into a private local folder on Windows and point `lark-cli` to it according to that tool's own config/profile rules. After copying, verify with `lark-cli doctor` before running MaxRead.

The Feishu bot/app must keep the same permissions as the old machine. Required practical checks:

- The bot can receive IM events.
- The selected identity in `MAXREAD_FEISHU_AS` works.
- Docx creation/write works.
- Media upload works, especially `docs:document.media:upload`.

## File And Cache Management

Runtime files are local machine state:

- `MAXREAD_DB=C:\MaxRead\maxread.sqlite3`
- `MAXREAD_WORKDIR=C:\MaxRead\var\maxread`

Migration options:

- Clean migration: do not copy `maxread.sqlite3` or `var\`; MaxRead starts with an empty local history/cache.
- Continuity migration: stop the old listener, copy `maxread.sqlite3` and `var\maxread` to Windows, then start Windows listener.

Do not run old and new listeners at the same time for the same bot unless you intentionally want duplicate processing.

## Install On Windows

Prerequisites:

- Windows 10/11
- Git for Windows
- Python 3.11+ on `PATH`
- `lark-cli` installed and authenticated
- Private GitHub repo access token if cloning over HTTPS

From PowerShell:

```powershell
mkdir C:\MaxReadLocal -Force
git clone https://github.com/xiaolong-li1/maxread.git C:\MaxRead
cd C:\MaxRead
Copy-Item .\deploy\windows\env.windows.example C:\MaxReadLocal\maxread.env
notepad C:\MaxReadLocal\maxread.env
.\deploy\windows\install.ps1
```

Then verify:

```powershell
.\run-admin.ps1
.\run-listener.ps1
```

Admin UI defaults to:

```text
http://127.0.0.1:8765/
```

## Task Plan

1. Prepare Windows machine: install Git, Python, and `lark-cli`.
2. Create `C:\MaxReadLocal\maxread.env` from `env.windows.example`.
3. Fill model settings: `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `MAXREAD_MODEL`.
4. Initialize Feishu on Windows with `lark-cli config init --new`; only run `lark-cli auth login` if you intentionally use `MAXREAD_FEISHU_AS=user`.
5. Verify `lark-cli doctor` is healthy.
6. Clone private repo into `C:\MaxRead`.
7. Run `.\deploy\windows\install.ps1`.
8. Optional: copy old `maxread.sqlite3` and `var\maxread` after stopping the old machine's listener.
9. Run `.\run-admin.ps1` and open the admin UI.
10. Run `.\run-listener.ps1`, send a small arXiv ID or web article to the Feishu bot, and confirm the generated doc.
11. After Windows is confirmed, keep only one production listener running.

## Optional Auto Start

For the first migration, run manually from PowerShell so logs stay visible. After it works, create a Windows Task Scheduler task that runs:

```powershell
powershell.exe -ExecutionPolicy Bypass -File C:\MaxRead\run-listener.ps1
```

Create a second optional task for:

```powershell
powershell.exe -ExecutionPolicy Bypass -File C:\MaxRead\run-admin.ps1
```
