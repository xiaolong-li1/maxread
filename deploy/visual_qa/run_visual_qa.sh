#!/usr/bin/env bash
set -euo pipefail

ROOT="${MAXREAD_VISUAL_QA_ROOT:-$HOME/.local/share/maxread-browser}"
export FONTCONFIG_FILE="${FONTCONFIG_FILE:-$ROOT/fonts.conf}"
export LD_LIBRARY_PATH="$ROOT/libs/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PYTHONPATH="$ROOT/python${PYTHONPATH:+:$PYTHONPATH}"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$ROOT/browsers}"

exec flock -w 180 "$ROOT/visual_qa.lock" python3 "$ROOT/maxread_visual_qa.py" "$@"
