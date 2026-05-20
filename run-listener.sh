#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

export MAXREAD_MODEL="${MAXREAD_MODEL:-gpt-5.5}"
export MAXREAD_FEISHU_AS="${MAXREAD_FEISHU_AS:-bot}"
export MAXREAD_DB="${MAXREAD_DB:-./maxread.sqlite3}"
export MAXREAD_WORKDIR="${MAXREAD_WORKDIR:-./var/maxread}"

exec python3 -m maxread.cli listen

