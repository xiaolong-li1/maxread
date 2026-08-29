#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${MAXREAD_REPO_URL:-https://github.com/xiaolong-li1/maxread.git}"
DEFAULT_INSTALL_DIR="${HOME}/maxread"
DEFAULT_KEYS_FILE="${HOME}/maxread.env"
SERVICE_NAME="maxread"
ADMIN_SERVICE_NAME="maxread-admin"

log() { printf '[maxread-deploy] %s\n' "$*"; }
fail() { printf '[maxread-deploy] ERROR: %s\n' "$*" >&2; exit 1; }
ask() {
  local prompt="$1" default="$2" value
  if [ -n "$default" ]; then
    read -r -p "$prompt [$default]: " value || true
    printf '%s' "${value:-$default}"
  else
    read -r -p "$prompt: " value || true
    printf '%s' "$value"
  fi
}
require_cmd() { command -v "$1" >/dev/null 2>&1 || fail "Missing command: $1"; }
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
git_with_optional_token() {
  local token="$1" askpass=""
  shift
  if [ -n "$token" ]; then
    askpass="$(mktemp /tmp/maxread-git-askpass.XXXXXX)"
    make_askpass "$askpass"
    GITHUB_TOKEN="$token" GIT_ASKPASS="$askpass" GIT_TERMINAL_PROMPT=0 git "$@"
    rm -f "$askpass"
  else
    git "$@"
  fi
}
abs_path() {
  python3 - "$1" <<'PY2'
from pathlib import Path
import sys
print(Path(sys.argv[1]).expanduser().resolve())
PY2
}
copy_env_file() {
  local src="$1" dst="$2"
  [ -f "$src" ] || fail "Key/env file not found: $src"
  mkdir -p "$(dirname "$dst")"
  cp "$src" "$dst"
  chmod 600 "$dst"
}
ensure_env_defaults() {
  local env_file="$1" install_dir="$2"
  touch "$env_file"
  chmod 600 "$env_file"
  grep -q '^MAXREAD_DB=' "$env_file" || printf '\nMAXREAD_DB=%s/maxread.sqlite3\n' "$install_dir" >> "$env_file"
  grep -q '^MAXREAD_WORKDIR=' "$env_file" || printf 'MAXREAD_WORKDIR=%s/var/maxread\n' "$install_dir" >> "$env_file"
  grep -q '^MAXREAD_LARK_CLI=' "$env_file" || printf 'MAXREAD_LARK_CLI=lark-cli\n' >> "$env_file"
  grep -q '^MAXREAD_FEISHU_AS=' "$env_file" || printf 'MAXREAD_FEISHU_AS=bot\n' >> "$env_file"
  grep -q '^MAXREAD_REQUIRE_SOURCE=' "$env_file" || printf 'MAXREAD_REQUIRE_SOURCE=true\n' >> "$env_file"
  grep -q '^MAXREAD_ARXIV_PARALLEL_STREAMS=' "$env_file" || printf 'MAXREAD_ARXIV_PARALLEL_STREAMS=1\n' >> "$env_file"
  grep -q '^MAXREAD_ARXIV_PARALLEL_MIN_BYTES=' "$env_file" || printf 'MAXREAD_ARXIV_PARALLEL_MIN_BYTES=1048576\n' >> "$env_file"
  grep -q '^MAXREAD_QUEUE_WORKERS=' "$env_file" || printf 'MAXREAD_QUEUE_WORKERS=2\n' >> "$env_file"
  grep -q '^MAXREAD_LLM_CONCURRENCY=' "$env_file" || printf 'MAXREAD_LLM_CONCURRENCY=2\n' >> "$env_file"
  grep -q '^MAXREAD_FEISHU_CONCURRENCY=' "$env_file" || printf 'MAXREAD_FEISHU_CONCURRENCY=3\n' >> "$env_file"
  grep -q '^MAXREAD_MODEL=' "$env_file" || printf 'MAXREAD_MODEL=gpt-5.5\n' >> "$env_file"
  grep -q '^MAXREAD_OPENAI_API_MODE=' "$env_file" || printf 'MAXREAD_OPENAI_API_MODE=responses\n' >> "$env_file"
  grep -q '^MAXREAD_RECOVERY_ATTEMPTS=' "$env_file" || printf 'MAXREAD_RECOVERY_ATTEMPTS=3\n' >> "$env_file"
  grep -q '^MAXREAD_DUTY_TIMEZONE=' "$env_file" || printf 'MAXREAD_DUTY_TIMEZONE=Asia/Shanghai\n' >> "$env_file"
  grep -q '^MAXREAD_DUTY_CHAT_ID=' "$env_file" || printf 'MAXREAD_DUTY_CHAT_ID=\n' >> "$env_file"
  grep -q '^MAXREAD_DUTY_HOUR=' "$env_file" || printf 'MAXREAD_DUTY_HOUR=7\n' >> "$env_file"
  grep -q '^MAXREAD_DUTY_MINUTE=' "$env_file" || printf 'MAXREAD_DUTY_MINUTE=0\n' >> "$env_file"
}
install_python_deps() {
  local install_dir="$1"
  python3 -m venv "$install_dir/.venv"
  "$install_dir/.venv/bin/python" -m pip install --upgrade pip wheel
  "$install_dir/.venv/bin/pip" install -e "$install_dir"
  "$install_dir/.venv/bin/pip" install Pillow
}
write_runtime_scripts() {
  local install_dir="$1"
  cat > "$install_dir/run-listener.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi
exec ./.venv/bin/python -m maxread.cli listen
SH
  cat > "$install_dir/run-admin.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi
exec ./.venv/bin/python -m maxread.cli admin --host "${MAXREAD_ADMIN_HOST:-127.0.0.1}" --port "${MAXREAD_ADMIN_PORT:-8765}"
SH
  chmod +x "$install_dir/run-listener.sh" "$install_dir/run-admin.sh"
  cat > "$install_dir/run-duty-reminder.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi
exec ./.venv/bin/python ./duty-reminder/duty_reminder.py --config ./duty-reminder/duty-reminder.json --daemon
SH
  chmod +x "$install_dir/run-duty-reminder.sh"
}
install_systemd_units() {
  local install_dir="$1" user_systemd_dir="$HOME/.config/systemd/user"
  mkdir -p "$user_systemd_dir"
  sed "s#__INSTALL_DIR__#$install_dir#g; s#__SERVICE_NAME__#$SERVICE_NAME#g" "$install_dir/deploy/systemd/maxread.service" > "$user_systemd_dir/$SERVICE_NAME.service"
  sed "s#__INSTALL_DIR__#$install_dir#g; s#__SERVICE_NAME__#$ADMIN_SERVICE_NAME#g" "$install_dir/deploy/systemd/maxread-admin.service" > "$user_systemd_dir/$ADMIN_SERVICE_NAME.service"
  sed "s#__INSTALL_DIR__#$install_dir#g" "$install_dir/deploy/systemd/maxread-duty-reminder.service" > "$user_systemd_dir/maxread-duty-reminder.service"
  systemctl --user daemon-reload
  systemctl --user enable --now "$SERVICE_NAME.service"
  systemctl --user enable --now "$ADMIN_SERVICE_NAME.service"
  # Duty reminder is a separate deployment and is intentionally not started
  # by the main installer. Enable it explicitly only on its owner machine.
}
install_launchd_plists() {
  local install_dir="$1" plist_dir="$HOME/Library/LaunchAgents"
  mkdir -p "$plist_dir"
  sed "s#__INSTALL_DIR__#$install_dir#g" "$install_dir/deploy/launchd/com.maxread.listener.plist" > "$plist_dir/com.maxread.listener.plist"
  sed "s#__INSTALL_DIR__#$install_dir#g" "$install_dir/deploy/launchd/com.maxread.admin.plist" > "$plist_dir/com.maxread.admin.plist"
  sed "s#__INSTALL_DIR__#$install_dir#g" "$install_dir/deploy/launchd/com.maxread.duty-reminder.plist" > "$plist_dir/com.maxread.duty-reminder.plist"
  launchctl unload "$plist_dir/com.maxread.listener.plist" >/dev/null 2>&1 || true
  launchctl unload "$plist_dir/com.maxread.admin.plist" >/dev/null 2>&1 || true
  launchctl load "$plist_dir/com.maxread.listener.plist"
  launchctl load "$plist_dir/com.maxread.admin.plist"
}

main() {
  require_cmd git
  require_cmd python3

  local install_dir keys_file mode
  install_dir="${MAXREAD_INSTALL_DIR:-}"
  if [ -z "$install_dir" ]; then
    install_dir="$(ask 'Deploy MaxRead to directory' "$DEFAULT_INSTALL_DIR")"
  fi
  install_dir="$(abs_path "$install_dir")"
  keys_file="${MAXREAD_KEYS_FILE:-}"
  if [ -z "$keys_file" ]; then
    keys_file="$(ask 'Local key/env file path' "$DEFAULT_KEYS_FILE")"
  fi
  keys_file="$(abs_path "$keys_file")"
  [ -f "$keys_file" ] || fail "Key/env file not found: $keys_file"

  local github_token
  github_token="${MAXREAD_GITHUB_TOKEN:-${GITHUB_TOKEN:-}}"
  if [ -z "$github_token" ]; then
    github_token="$(read_key_from_file "$keys_file" MAXREAD_GITHUB_TOKEN)"
  fi
  if [ -z "$github_token" ]; then
    github_token="$(read_key_from_file "$keys_file" GITHUB_TOKEN)"
  fi

  if [ ! -d "$install_dir/.git" ]; then
    mkdir -p "$(dirname "$install_dir")"
    log "Cloning $REPO_URL -> $install_dir"
    git_with_optional_token "$github_token" clone "$REPO_URL" "$install_dir"
  else
    log "Updating existing checkout at $install_dir"
    git_with_optional_token "$github_token" -C "$install_dir" fetch origin
    git -C "$install_dir" checkout main
    git_with_optional_token "$github_token" -C "$install_dir" pull --ff-only origin main
  fi

  copy_env_file "$keys_file" "$install_dir/.env"
  ensure_env_defaults "$install_dir/.env" "$install_dir"
  mkdir -p "$install_dir/var/maxread"
  install_python_deps "$install_dir"
  write_runtime_scripts "$install_dir"

  mode="manual"
  if command -v systemctl >/dev/null 2>&1 && systemctl --user status >/dev/null 2>&1; then
    mode="${MAXREAD_AUTO_START:-}"
    if [ -z "$mode" ]; then
      mode="$(ask 'Start with systemd user services? yes/no' 'no')"
    fi
    if [ "$mode" = "yes" ]; then
      install_systemd_units "$install_dir"
      log "Started systemd user services: $SERVICE_NAME, $ADMIN_SERVICE_NAME (duty reminder not started)"
    fi
  elif command -v launchctl >/dev/null 2>&1; then
    mode="${MAXREAD_AUTO_START:-}"
    if [ -z "$mode" ]; then
      mode="$(ask 'Start with launchd user agents? yes/no' 'no')"
    fi
    if [ "$mode" = "yes" ]; then
      install_launchd_plists "$install_dir"
      log "Started launchd agents: com.maxread.listener, com.maxread.admin (duty reminder not started)"
    fi
  fi

  log "Install dir: $install_dir"
  log "Env file: $install_dir/.env"
  log "Admin UI: http://127.0.0.1:8765/"
  log "Manual listener: cd '$install_dir' && ./run-listener.sh"
  log "Manual admin: cd '$install_dir' && ./run-admin.sh"
  log "Before first real use, verify Feishu auth with: lark-cli doctor"
}

main "$@"
