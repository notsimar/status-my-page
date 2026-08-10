#!/usr/bin/env python3
"""Archive the current DB state to a JSON snapshot *before* init_db() resets everything."""

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "instance" / "status.db"
ARCHIVES_DIR = Path(__file__).resolve().parent / "archives"


def snapshot():
    """Read all items from the live DB, write a timestamped JSON file into archives/."""
    if not DB_PATH.exists():
        print("No database found — skipping archive.")
        return None

    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            "SELECT id, name, status, notes, position FROM status_items ORDER BY position"
        ).fetchall()
    except sqlite3.OperationalError:
        print("Table not found — skipping archive.")
        return None
    finally:
        db.close()

    if not rows:
        print("No items in DB — skipping archive.")
        return None

    ARCHIVES_DIR.mkdir(exist_ok=True)

    ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    filename = ARCHIVES_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    snapshot = {
        "timestamp": ts,
        "items": [{"id": r["id"], "name": r["name"], "status": r["status"],
                    "notes": r["notes"], "position": r["position"]} for r in rows],
    }

    with open(filename, mode='w') as f:
        json.dump(snapshot, f, indent=2)

    reds = sum(1 for r in snapshot["items"] if r["status"] == "red")
    total = len(snapshot["items"])
    print(f"Archived {total} items ({reds}red / {total - reds} green) -> {filename.name}")
    return filename


if __name__ == "__main__":
    snapshot()
