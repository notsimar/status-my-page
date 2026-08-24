#!/usr/bin/env bash
# Restart: stop → start quickly (no dep install or DB migration)
set -e
cd "$(dirname "$0")"

# ── Help ───────────────────────────────────────
usage() {
    sed -n '2,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
}

case "${1:-}" in
    -h|--help) usage ;;
esac

echo "=== Restarting server ==="
./stop.sh 2>/dev/null || true
sleep 1
./start.sh
