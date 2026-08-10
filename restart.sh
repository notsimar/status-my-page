#!/usr/bin/env bash
# Restart: stop → start quickly (no dep install or DB migration)
set -e
cd "$(dirname "$0")"

echo "=== Restarting server ==="
./stop.sh 2>/dev/null || true
sleep 1
./start.sh
