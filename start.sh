#!/usr/bin/env bash
# Start the status page server (gunicorn, matching install.sh's systemd unit)
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

mkdir -p logs

# Load env vars from project-local .env.local (created by install.sh), falling
# back to .env. Sourced with set -a so values containing spaces/quotes survive
# (export $(xargs) breaks on them). Workers inherit STATUS_ADMIN_PASS_HASH /
# STATUS_SECRET_KEY.
if [ -f .env.local ]; then
    set -a; . ./.env.local; set +a
elif [ -f .env ]; then
    set -a; . ./.env; set +a
fi

nohup .venv/bin/gunicorn --bind 0.0.0.0:8920 --workers 2 --timeout 30 app:app \
    >> logs/server.log 2>&1 &
echo $! > "$PID_FILE"
echo "Running on http://0.0.0.0:8920 (PID $!, gunicorn)"
