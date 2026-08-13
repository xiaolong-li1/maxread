#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

mkdir -p var/maxread/logs
exec bash -lc 'tail -f /dev/null | ./run-listener.sh' >> var/maxread/logs/listener.nohup.log 2>&1
