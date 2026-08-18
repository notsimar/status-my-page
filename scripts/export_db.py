#!/usr/bin/env python3
"""
Nightly database export to JSON with 30-day retention.

Exports the full status_my_page database (status_items + status_history)
to a timestamped JSON file in the exports/ directory.
Retains exports for 30 days, deleting older files.
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path


# Project paths
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "instance" / "status.db"
EXPORTS_DIR = BASE_DIR / "exports"
RETENTION_DAYS = 30


def get_connection():
    """Return a DB connection."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def export_database():
    """Export the entire database to a JSON file."""
    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH}")
        sys.exit(1)

    EXPORTS_DIR.mkdir(exist_ok=True)

    conn = get_connection()
    try:
        # Export status_items
        items = conn.execute(
            "SELECT id, name, status, notes, position FROM status_items ORDER BY position"
        ).fetchall()

        # Export status_history
        history = conn.execute(
            "SELECT id, item_id, event_type, old_value, new_value, occurred FROM status_history ORDER BY occurred"
        ).fetchall()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        export_data = {
            "timestamp": datetime.now().isoformat(),
            "items": [
                {"id": r["id"], "name": r["name"], "status": r["status"],
                 "notes": r["notes"], "position": r["position"]}
                for r in items
            ],
            "history": [
                {"id": r["id"], "item_id": r["item_id"], "event_type": r["event_type"],
                 "old_value": r["old_value"], "new_value": r["new_value"],
                 "occurred": r["occurred"]}
                for r in history
            ],
        }

        filename = EXPORTS_DIR / f"{timestamp}.json"
        # Write atomically
        import tempfile
        fd, tmp_path = tempfile.mkstemp(dir=str(EXPORTS_DIR), prefix=".export_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(export_data, fh, indent=2)
            os.replace(tmp_path, str(filename))
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        print(f"Exported {len(items)} items, {len(history)} history entries -> {filename.name}")
        return filename
    finally:
        conn.close()


def prune_old_exports():
    """Delete export files older than RETENTION_DAYS."""
    if not EXPORTS_DIR.exists():
        return

    cutoff = datetime.now() - timedelta(days=RETENTION_DAYS)
    deleted = 0
    for f in EXPORTS_DIR.glob("*.json"):
        try:
            # Parse timestamp from filename: YYYYMMDD_HHMMSS.json
            ts_str = f.stem
            file_dt = datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
            if file_dt < cutoff:
                f.unlink()
                deleted += 1
                print(f"Deleted old export: {f.name}")
        except ValueError:
            # Skip files that don't match the expected format
            continue

    if deleted:
        print(f"Pruned {deleted} export(s) older than {RETENTION_DAYS} days")


def main():
    export_database()
    prune_old_exports()


if __name__ == "__main__":
    main()