#!/usr/bin/env bash
# Stop the status page server
set -e
cd "$(dirname "$0")"

# ── Help ───────────────────────────────────────
usage() {
    sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
}

case "${1:-}" in
    -h|--help) usage ;;
esac

PID_FILE=".server.pid"

if [ ! -f "$PID_FILE" ]; then
    echo "No PID file found."
    exit 0
fi

PID=$(cat "$PID_FILE")
# Kill all children too (nohup creates a group)
pkill -P "$PID" 2>/dev/null || true
kill "$PID" 2>/dev/null || true
rm -f "$PID_FILE"
echo "Stopped server (was PID $PID)"
