#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
exec python3 -m maxread.cli admin --host "${MAXREAD_ADMIN_HOST:-127.0.0.1}" --port "${MAXREAD_ADMIN_PORT:-8765}"
