#!/usr/bin/env bash
set -e
cd /home/ssahni/Developer/status-my-page

# Kill any stale process
pkill -f app.py 2>/dev/null || true
rm -f .server.pid

# Generate hash and start
export STATUS_ADMIN_PASS_HASH=$(/home/ssahni/Developer/status-my-page/.venv/bin/python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('test123'))")
export PYTHONUNBUFFERED=1

echo "Starting status page with hash: ${STATUS_ADMIN_PASS_HASH:0:20}..."
nohup .venv/bin/python3 app.py > logs/server.log 2>&1 &
echo $! > .server.pid
echo "Started (PID file: $(cat .server.pid))"
