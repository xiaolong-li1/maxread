#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${MAXREAD_REPO_URL:-https://github.com/xiaolong-li1/maxread.git}"
DEFAULT_INSTALL_DIR="${HOME}/maxread"
DEFAULT_KEYS_FILE="${HOME}/maxread.env"
BRANCH="${MAXREAD_BRANCH:-main}"

log() { printf '[maxread-bootstrap] %s\n' "$*"; }
fail() { printf '[maxread-bootstrap] ERROR: %s\n' "$*" >&2; exit 1; }
ask() {
  local prompt="$1" default="$2" value
  read -r -p "$prompt [$default]: " value || true
  printf '%s' "${value:-$default}"
}
abs_path() {
  python3 - "$1" <<'PY2'
from pathlib import Path
import sys
print(Path(sys.argv[1]).expanduser().resolve())
PY2
}
read_key_from_file() {
  local file="$1" name="$2"
  [ -f "$file" ] || return 0
  awk -F= -v key="$name" '$1 == key {print substr($0, length($1)+2); exit}' "$file" | sed "s/^['\"]//; s/['\"]$//"
}
make_askpass() {
  local path="$1"
  cat > "$path" <<'SH'
#!/bin/sh
case "$1" in
  *Username*) printf '%s\n' 'x-access-token' ;;
  *Password*) printf '%s\n' "$GITHUB_TOKEN" ;;
  *) printf '\n' ;;
esac
SH
  chmod 700 "$path"
}

main() {
  command -v git >/dev/null 2>&1 || fail 'git is required'
  command -v python3 >/dev/null 2>&1 || fail 'python3 is required'

  local install_dir keys_file token askpass
  install_dir="${MAXREAD_INSTALL_DIR:-$(ask 'Deploy MaxRead to directory' "$DEFAULT_INSTALL_DIR")}" 
  install_dir="$(abs_path "$install_dir")"
  keys_file="${MAXREAD_KEYS_FILE:-$(ask 'Local key/env file path' "$DEFAULT_KEYS_FILE")}" 
  keys_file="$(abs_path "$keys_file")"
  [ -f "$keys_file" ] || fail "Key/env file not found: $keys_file"

  token="${MAXREAD_GITHUB_TOKEN:-${GITHUB_TOKEN:-}}"
  if [ -z "$token" ]; then
    token="$(read_key_from_file "$keys_file" MAXREAD_GITHUB_TOKEN)"
  fi
  if [ -z "$token" ]; then
    token="$(read_key_from_file "$keys_file" GITHUB_TOKEN)"
  fi

  mkdir -p "$(dirname "$install_dir")"
  if [ ! -d "$install_dir/.git" ]; then
    if [ -n "$token" ]; then
      askpass="$(mktemp /tmp/maxread-git-askpass.XXXXXX)"
      make_askpass "$askpass"
      GITHUB_TOKEN="$token" GIT_ASKPASS="$askpass" GIT_TERMINAL_PROMPT=0 git clone --branch "$BRANCH" "$REPO_URL" "$install_dir"
      rm -f "$askpass"
    else
      log 'No GitHub token found in env or key file; trying normal git clone.'
      git clone --branch "$BRANCH" "$REPO_URL" "$install_dir"
    fi
  else
    log "Existing checkout found: $install_dir"
  fi

  MAXREAD_INSTALL_DIR="$install_dir" MAXREAD_KEYS_FILE="$keys_file" bash "$install_dir/deploy/install.sh"
}

main "$@"
