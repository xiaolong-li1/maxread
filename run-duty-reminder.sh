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

PYTHON_BIN="${MAXREAD_PYTHON:-/usr/bin/python3}"
exec "$PYTHON_BIN" -m maxread.cli duty daemon
