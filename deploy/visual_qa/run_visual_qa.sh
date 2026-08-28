#!/usr/bin/env bash
set -euo pipefail

ROOT="${MAXREAD_VISUAL_QA_ROOT:-$HOME/.local/share/maxread-browser}"
PYTHON_BIN="${MAXREAD_VISUAL_QA_PYTHON:-python3}"
export FONTCONFIG_FILE="${FONTCONFIG_FILE:-$ROOT/fonts.conf}"
export LD_LIBRARY_PATH="$ROOT/libs/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PYTHONPATH="$ROOT/python${PYTHONPATH:+:$PYTHONPATH}"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$ROOT/browsers}"

SLOTS="${MAXREAD_VISUAL_QA_CONCURRENCY:-2}"
if ! [[ "$SLOTS" =~ ^[1-9][0-9]*$ ]]; then
  SLOTS=2
fi

run_qa() {
  local output_file output_dir qa_script report_file status
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
  qa_script="$ROOT/maxread_visual_qa.py"
  if [[ "${MAXREAD_VISUAL_QA_EXPORT_PDF:-false}" =~ ^(1|true|yes|on)$ && -f "$ROOT/maxread_pdf_qa.py" ]]; then
    qa_script="$ROOT/maxread_pdf_qa.py"
  fi
  set +e
  timeout --kill-after=5 "${MAXREAD_VISUAL_QA_RUNNER_TIMEOUT:-220}" "$PYTHON_BIN" "$qa_script" "$@" >"$output_file"
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
