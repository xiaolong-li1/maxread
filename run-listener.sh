#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

export PATH="$HOME/.local/node/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export MAXREAD_MODEL="${MAXREAD_MODEL:-gpt-5.5}"
export MAXREAD_FEISHU_AS="${MAXREAD_FEISHU_AS:-bot}"
export MAXREAD_DB="${MAXREAD_DB:-./maxread.sqlite3}"
export MAXREAD_WORKDIR="${MAXREAD_WORKDIR:-./var/maxread}"

if [ -n "${MAXREAD_PYTHON:-}" ]; then
  PYTHON_BIN="$MAXREAD_PYTHON"
elif [ -x ./.venv/bin/python ]; then
  # The deployment venv is authoritative; dependency checks here can silently
  # switch the service to a different interpreter with a different package set.
  PYTHON_BIN="./.venv/bin/python"
else
  PYTHON_BIN="python3"
fi
exec "$PYTHON_BIN" -m maxread.cli listen
