#!/usr/bin/env bash
# Rebuild: install deps, run DB migrations, restart server
set -e
cd "$(dirname "$0")"

# ── Help ───────────────────────────────────────
usage() {
    sed -n '2,5p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
}

case "${1:-}" in
    -h|--help) usage ;;
esac

echo "=== Installing Python dependencies ==="
.venv/bin/pip install -r requirements.txt --quiet 2>&1 | grep -v "notice\|WARN" || true

echo ""
echo "=== Running DB migrations ==="
.venv/bin/python3 << 'PYEOF'
import sqlite3, os
from pathlib import Path

DB_PATH = Path("instance/status.db")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

if not DB_PATH.exists():
    print("No database found — seed on next start.py will create it.")
else:
    db = sqlite3.connect(str(DB_PATH))
    
    # Add notes column if missing
    cols = [r[1] for r in db.execute("PRAGMA table_info(status_items)").fetchall() if r[1]]
    if "notes" not in cols:
        print("Adding 'notes' column to status_items…")
        try:
            db.execute("ALTER TABLE status_items ADD COLUMN notes TEXT DEFAULT ''")
            db.commit()
            print("  Done.")
        except sqlite3.OperationalError as e:
            if "duplicate" not in str(e).lower():
                raise
    
    # Add position column if missing
    cols = [r[1] for r in db.execute("PRAGMA table_info(status_items)").fetchall() if r[1]]
    if "position" not in cols:
        print("Adding 'position' column to status_items…")
        try:
            db.execute("ALTER TABLE status_items ADD COLUMN position INTEGER DEFAULT 0")
            db.commit()
            print("  Done.")
        except sqlite3.OperationalError as e:
            if "duplicate" not in str(e).lower():
                raise
    
    count = db.execute("SELECT COUNT(*) FROM status_items").fetchone()[0]
    print(f"Database OK — {count} items.")
    db.close()

PYEOF

echo ""
echo "=== Restarting server ==="
./stop.sh 2>/dev/null || true
sleep 1
./start.sh
