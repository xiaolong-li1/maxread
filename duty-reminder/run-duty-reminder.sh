#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
exec /usr/bin/python3 ./duty_reminder.py --config ./duty-reminder.json --daemon
