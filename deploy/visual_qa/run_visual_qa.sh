#!/usr/bin/env bash
set -euo pipefail

ROOT="${MAXREAD_VISUAL_QA_ROOT:-$HOME/.local/share/maxread-browser}"
PYTHON_BIN="${MAXREAD_VISUAL_QA_PYTHON:-python3}"
export FONTCONFIG_FILE="${FONTCONFIG_FILE:-$ROOT/fonts.conf}"
export LD_LIBRARY_PATH="$ROOT/libs/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PYTHONPATH="$ROOT/python${PYTHONPATH:+:$PYTHONPATH}"
if [[ -z "${PLAYWRIGHT_BROWSERS_PATH:-}" ]]; then
  if [[ -d "$ROOT/browsers" ]]; then
    PLAYWRIGHT_BROWSERS_PATH="$ROOT/browsers"
  elif [[ -d "${HOME:-}/.cache/ms-playwright" ]]; then
    # The browser bundle may be installed by Playwright's normal cache
    # instead of the MaxRead resource bundle.
    PLAYWRIGHT_BROWSERS_PATH="$HOME/.cache/ms-playwright"
  else
    PLAYWRIGHT_BROWSERS_PATH="$ROOT/browsers"
  fi
fi
export PLAYWRIGHT_BROWSERS_PATH

SLOTS="${MAXREAD_VISUAL_QA_CONCURRENCY:-1}"
if ! [[ "$SLOTS" =~ ^[1-9][0-9]*$ ]]; then
  SLOTS=2
fi

run_qa() {
  local output_file output_dir report_file status
  output_file="$(mktemp)"
  output_dir=""
  local previous=""
  for argument in "$@"; do
    if [[ "$previous" == "--output-dir" ]]; then
      output_dir="$argument"
      break
    fi
    previous="$argument"
  done
  set +e
  # Production acceptance always inspects the live Feishu page with
  # Playwright/CDP. Do not switch to asynchronous Feishu PDF export here.
  timeout --kill-after=5 "${MAXREAD_VISUAL_QA_RUNNER_TIMEOUT:-220}" "$PYTHON_BIN" "$ROOT/maxread_visual_qa.py" "$@" >"$output_file"
  status=$?
  set -e
  cat "$output_file"
  if [[ "$status" == "124" || "$status" == "137" ]]; then
    report_file="${output_dir%/}/report.json"
    if [[ -n "$output_dir" && -s "$report_file" ]]; then
      if [[ ! -s "$output_file" ]]; then
        "$PYTHON_BIN" -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1], encoding="utf-8")), ensure_ascii=False, separators=(",", ":")))' "$report_file"
      fi
      rm -f "$output_file"
      return 0
    fi
  fi
  rm -f "$output_file"
  return "$status"
}

for ((slot=0; slot<SLOTS; slot++)); do
  lock_file="$ROOT/visual_qa.${slot}.lock"
  exec {lock_fd}>"$lock_file"
  if flock -n "$lock_fd"; then
    run_qa "$@"
    exit $?
  fi
  exec {lock_fd}>&-
done

# All slots are busy. Pick one deterministically and wait with the existing
# bounded timeout instead of spawning unbounded browser processes.
slot=$(( $$ % SLOTS ))
exec {lock_fd}>"$ROOT/visual_qa.${slot}.lock"
flock -w 180 "$lock_fd"
run_qa "$@"
