#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

export PATH="$HOME/.local/node/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
if [ -n "${MAXREAD_PYTHON:-}" ]; then
  PYTHON_BIN="$MAXREAD_PYTHON"
elif [ -x ./.venv/bin/python ] && ./.venv/bin/python -c 'import PIL' >/dev/null 2>&1; then
  PYTHON_BIN="./.venv/bin/python"
else
  PYTHON_BIN="python3"
fi
exec "$PYTHON_BIN" -m maxread.cli admin --host "${MAXREAD_ADMIN_HOST:-127.0.0.1}" --port "${MAXREAD_ADMIN_PORT:-8765}"
