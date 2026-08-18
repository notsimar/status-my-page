#!/usr/bin/env python3
"""
Database backup script for status-my-page.

Creates a timestamped copy of the SQLite database file using SQLite's backup API
for a consistent snapshot. Supports manual execution and cron scheduling with
configurable retention.

Usage:
    python3 scripts/backup_db.py              # Create backup with default retention (30 days)
    python3 scripts/backup_db.py --retention 7  # Keep only 7 days
    python3 scripts/backup_db.py --list        # List existing backups
    python3 scripts/backup_db.py --prune       # Prune old backups only (no new backup)
"""

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path


# Project paths
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "instance" / "status.db"
BACKUPS_DIR = BASE_DIR / "backups"
DEFAULT_RETENTION_DAYS = 30


def get_connection():
    """Return a DB connection."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def create_backup(retention_days: int = DEFAULT_RETENTION_DAYS) -> Path:
    """Create a consistent backup of the database using SQLite backup API."""
    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH}")
        sys.exit(1)

    BACKUPS_DIR.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_filename = f"status_{timestamp}.db"
    backup_path = BACKUPS_DIR / backup_filename

    # Use SQLite's backup API for a consistent snapshot (handles WAL mode correctly)
    src_conn = get_connection()
    dst_conn = sqlite3.connect(str(backup_path))
    try:
        src_conn.backup(dst_conn)
        print(f"Backup created: {backup_filename}")
    finally:
        src_conn.close()
        dst_conn.close()

    # Prune old backups after successful creation
    prune_backups(retention_days)

    return backup_path


def prune_backups(retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
    """Delete backup files older than retention_days."""
    if not BACKUPS_DIR.exists():
        return 0

    cutoff = datetime.now() - timedelta(days=retention_days)
    deleted = 0

    for f in BACKUPS_DIR.glob("status_*.db"):
        try:
            ts_str = f.stem.replace("status_", "")
            file_dt = datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
            if file_dt < cutoff:
                f.unlink()
                deleted += 1
                print(f"Deleted old backup: {f.name}")
        except ValueError:
            continue

    if deleted:
        print(f"Pruned {deleted} backup(s) older than {retention_days} days")
    return deleted


def list_backups() -> None:
    """List all existing backups with size and age."""
    if not BACKUPS_DIR.exists():
        print("No backups directory found")
        return

    backups = sorted(BACKUPS_DIR.glob("status_*.db"))
    if not backups:
        print("No backups found")
        return

    print(f"{'Filename':<30} {'Size':>10} {'Age':>12} {'Date'}")
    print("-" * 70)
    now = datetime.now()
    for f in backups:
        try:
            ts_str = f.stem.replace("status_", "")
            file_dt = datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
            age = now - file_dt
            age_str = f"{age.days}d {age.seconds//3600}h"
            size = f.stat().st_size
            size_str = f"{size/1024:.1f} KB" if size < 1024*1024 else f"{size/(1024*1024):.1f} MB"
            print(f"{f.name:<30} {size_str:>10} {age_str:>12} {file_dt.strftime('%Y-%m-%d %H:%M')}")
        except ValueError:
            print(f"{f.name:<30} {'?':>10} {'?':>12} ?")


def restore_backup(backup_name: str) -> None:
    """Restore a backup to the live database (requires stopping the app first)."""
    backup_path = BACKUPS_DIR / backup_name
    if not backup_path.exists():
        print(f"Backup not found: {backup_name}")
        sys.exit(1)

    if DB_PATH.exists():
        print(f"WARNING: This will overwrite the live database at {DB_PATH}")
        confirm = input("Type 'yes' to confirm: ")
        if confirm != "yes":
            print("Aborted")
            return

    # Use SQLite backup API to restore
    src_conn = sqlite3.connect(str(backup_path))
    dst_conn = sqlite3.connect(str(DB_PATH))
    try:
        src_conn.backup(dst_conn)
        print(f"Restored from {backup_name}")
    finally:
        src_conn.close()
        dst_conn.close()


def main():
    parser = argparse.ArgumentParser(description="Database backup/restore for status-my-page")
    parser.add_argument("--retention", type=int, default=DEFAULT_RETENTION_DAYS,
                        help=f"Retention period in days (default: {DEFAULT_RETENTION_DAYS})")
    parser.add_argument("--list", action="store_true", help="List existing backups")
    parser.add_argument("--prune", action="store_true", help="Prune old backups only (no new backup)")
    parser.add_argument("--restore", metavar="FILENAME", help="Restore from a backup file")

    args = parser.parse_args()

    if args.list:
        list_backups()
        return

    if args.restore:
        restore_backup(args.restore)
        return

    if args.prune:
        prune_backups(args.retention)
        return

    # Default: create backup
    create_backup(args.retention)


if __name__ == "__main__":
    main()