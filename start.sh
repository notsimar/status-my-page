#!/usr/bin/env bash
# Start the status page server
set -e
cd "$(dirname "$0")"

PID_FILE=".server.pid"

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "Already running (PID $PID)"
        exit 0
    fi
fi

echo "Starting…"
# Archive current DB state BEFORE init_db() resets everything, then prune old snapshots
if [ -f instance/status.db ]; then
    .venv/bin/python3 archiver.py 2>/dev/null || true
    bash cleanup.sh prune >/dev/null 2>&1 || true
fi

nohup .venv/bin/python3 app.py >> logs/server.log 2>&1 &
echo $! > "$PID_FILE"
echo "Running on http://0.0.0.0:8920 (PID $!)"
