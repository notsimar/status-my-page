"""Database layer for status-my-page.

Handles schema creation, migrations, seeding, queries, and archival.
"""

import datetime as dt
import json
import sqlite3
import tempfile
from pathlib import Path

from statuspage.config import get_db_path, get_archives_dir, load_config
from constants import (
    MAX_HISTORY_PER_ITEM,
    HISTORY_RUNTIME_CAP,
    DEFAULT_SEED_ITEMS,
    ARCHIVE_DIR_NAME,
    INSTANCE_DIR_NAME,
    DB_FILENAME,
    STATUS_SORT_ORDER,
)


# ── Connection management ───────────────────────────────────────────

def get_connection() -> sqlite3.Connection:
    """Return a DB connection.

    Inside a Flask request context this is the SAME connection for the whole
    request (stored in ``g``), matching the original ``get_db()`` semantics —
    callers may call get_connection() several times and rely on one
    transactional scope. Outside a request context a fresh connection is
    returned (close it when done).
    """
    from flask import g, has_request_context

    if has_request_context():
        if "db" not in g:
            g.db = _new_connection()
        return g.db
    return _new_connection()


def _new_connection() -> sqlite3.Connection:
    db_path = get_db_path()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ── Schema & Migrations ─────────────────────────────────────────────

def create_schema(db: sqlite3.Connection) -> None:
    """Create tables and backfill columns for older databases."""
    db.execute(
        """CREATE TABLE IF NOT EXISTS status_items (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'green',
            notes  TEXT DEFAULT '',
            position INTEGER NOT NULL DEFAULT 0
        )"""
    )
    try:
        db.execute("ALTER TABLE status_items ADD COLUMN notes TEXT DEFAULT ''")
        db.commit()
    except sqlite3.OperationalError:
        pass  # column already exists


def create_history_table(db: sqlite3.Connection) -> None:
    """Create history table and backfill occurred column."""
    db.execute(
        """CREATE TABLE IF NOT EXISTS status_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id    INTEGER NOT NULL REFERENCES status_items(id),
            event_type TEXT    NOT NULL DEFAULT 'status',
            old_value  TEXT    DEFAULT '',
            new_value  TEXT    DEFAULT '',
            occurred   TEXT    NOT NULL
        )"""
    )
    # Backfill `occurred` column for pre-existing databases
    try:
        db.execute("ALTER TABLE status_history ADD COLUMN occurred TEXT NOT NULL DEFAULT '1970-01-01T00:00:00Z'")
        db.commit()
    except sqlite3.OperationalError:
        pass  # column already exists


# ── Seeding & Sync ──────────────────────────────────────────────────

def compute_seed_items() -> list[str]:
    """Compute the list of items to seed from config + runtime."""
    from statuspage.config import _load_runtime
    rt = _load_runtime()
    runtime_items: list[str] = rt.get("items", [])
    seed_items = list(dict.fromkeys(
        [n.strip() for n in load_config().get("items", []) if n.strip()] +
        [n.strip() for n in runtime_items if n.strip()]
    )) or DEFAULT_SEED_ITEMS
    return seed_items


def sync_db_to_config(db: sqlite3.Connection, seed_items: list[str]) -> tuple[int, int]:
    """Sync DB rows to match seed_items. Returns (deleted_count, inserted_count)."""
    seed_set = set(seed_items)
    existing_rows = {name: rid for name, rid in
                     db.execute("SELECT name, id FROM status_items").fetchall()}

    deleted_count = 0
    inserted_count = 0

    # Delete items no longer in config
    for name in list(existing_rows):
        if name not in seed_set:
            db.execute("DELETE FROM status_items WHERE id = ?", [existing_rows[name]])
            deleted_count += 1

    # Insert new items (not yet in DB)
    existing_after_delete = {row[0] for row in
                             db.execute("SELECT name, id FROM status_items").fetchall()}
    new_items = [n for n in seed_items if n not in existing_after_delete]
    if new_items:
        max_pos = (db.execute("SELECT COALESCE(MAX(position), 0) FROM status_items").fetchone()[0])
        db.executemany(
            "INSERT INTO status_items (name, status, position) VALUES (?, 'green', ?)",
            [(n, max_pos + i + 1) for i, n in enumerate(new_items)]
        )
        inserted_count = len(new_items)

    # Re-index positions to match config order — DO NOT reset status/notes here.
    # Status/notes are only changed by admin actions or healthcheck worker.
    for i, name in enumerate(seed_items):
        row = db.execute("SELECT id FROM status_items WHERE name = ?", [name]).fetchone()
        if row:
            db.execute(
                "UPDATE status_items SET position=? WHERE id=?",
                (i, row[0])
            )

    return deleted_count, inserted_count


def restore_runtime_overrides(db: sqlite3.Connection, seed_set: set[str]) -> None:
    """Restore status, notes, and reorder overrides from _runtime YAML."""
    from statuspage.config import _load_runtime
    rt = _load_runtime()
    # Status overrides: {item_name: "degraded"|"red"}
    for item_name, new_state in rt.get("status", {}).items():
        if item_name not in seed_set or new_state in ("green", ""):
            continue
        row = db.execute(
            "SELECT id FROM status_items WHERE name = ?", [item_name]
        ).fetchone()
        if row:
            db.execute(
                "UPDATE status_items SET status=? WHERE id=?",
                (new_state, row["id"]),
            )

    # Notes overrides: {item_name: note_text}
    for item_name, note_text in rt.get("notes", {}).items():
        if item_name not in seed_set or not note_text.strip():
            continue
        row = db.execute(
            "SELECT id FROM status_items WHERE name = ?", [item_name]
        ).fetchone()
        if row:
            db.execute(
                "UPDATE status_items SET notes=? WHERE id=?",
                (note_text, row["id"]),
            )

    # Reorder overrides: [name, name, ...]
    reorder_list = rt.get("reorder", None)
    if reorder_list and isinstance(reorder_list, list):
        for i, item_name in enumerate(reorder_list):
            row = db.execute(
                "SELECT id FROM status_items WHERE name = ?", [item_name]
            ).fetchone()
            if row:
                db.execute(
                    "UPDATE status_items SET position=? WHERE id=?",
                    (i + 1, row["id"]),
                )


def restore_history_from_yaml(db: sqlite3.Connection) -> None:
    """Restore history entries from _runtime.history.

    Idempotent: before re-inserting the YAML mirror, remove the rows it
    would duplicate (matched by the unique ``occurred`` timestamp). Without
    this, every restart re-INSERTs the same YAML rows and the table grows by
    the full mirror size on every boot (reproduced: 1 → 2 → 3 rows for one
    event). DB-only entries beyond the YAML cap (HISTORY_RUNTIME_CAP) are
    preserved.
    """
    from statuspage.config import _load_runtime
    rt = _load_runtime()
    for item_name, entries in rt.get("history", {}).items():
        row = db.execute(
            "SELECT id FROM status_items WHERE name = ?", [item_name]
        ).fetchone()
        if not row:
            continue
        occurred_vals = [e.get("occurred") for e in entries if e.get("occurred")]
        if occurred_vals:
            placeholders = ",".join("?" * len(occurred_vals))
            db.execute(
                f"DELETE FROM status_history WHERE item_id = ? AND occurred IN ({placeholders})",
                [row["id"], *occurred_vals],
            )
        for entry in entries:
            db.execute(
                "INSERT INTO status_history (item_id, event_type, old_value, new_value, occurred) VALUES (?, ?, ?, ?, ?)",
                (row["id"], entry.get("event_type", "status"),
                 entry.get("old_value", ""), entry.get("new_value", ""),
                 entry.get("occurred", "1970-01-01T00:00:00Z")),
            )


def init_db() -> None:
    """Initialize/migrate DB tables and seed items from config.yaml.

    Takes a timestamped JSON snapshot of the live DB state (into archives/)
    before seeding so admin changes survive across restarts. Archive can be
    disabled for testing with STATUS_NO_ARCHIVE=1.
    """
    # ── Pre-reset archival — save current DB state before init_db() wipes it ──
    archive_db_snapshot()

    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row

    seed_items = compute_seed_items()
    seed_set = set(seed_items)

    create_schema(db)
    deleted_count, inserted_count = sync_db_to_config(db, seed_items)
    restore_runtime_overrides(db, seed_set)

    action = f'Rebuilt {len(seed_items)} config items from config.yaml'
    if deleted_count:
        action += f" ({deleted_count} removed)"
    if inserted_count:
        action += f", {inserted_count} added"
    print(action)

    create_history_table(db)
    restore_history_from_yaml(db)

    db.commit()
    db.close()


# ── Archive snapshot ────────────────────────────────────────────────

def archive_db_snapshot() -> None:
    """Take a timestamped JSON snapshot of current DB state before init_db() resets it.

    Archives are stored in `archives/YYYYMMDD_HHMMSS.json` and can be restored
    manually or programmatically.  Set env var STATUS_NO_ARCHIVE=1 to skip
    (useful during testing).
    """
    import os
    if os.environ.get("STATUS_NO_ARCHIVE"):
        return
    if not get_db_path().exists():
        return

    try:
        archive_db = sqlite3.connect(str(get_db_path()))
        archive_db.row_factory = sqlite3.Row
    except sqlite3.OperationalError:
        return

    try:
        rows = list(archive_db.execute(
            "SELECT id, name, status, notes, position FROM status_items ORDER BY position"
        ).fetchall())
    except sqlite3.OperationalError:
        archive_db.close()
        return

    if not rows:
        archive_db.close()
        return

    get_archives_dir().mkdir(exist_ok=True)
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    filename = get_archives_dir() / f"{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"

    snapshot_data = {
        "timestamp": ts,
        "items": [{"id": r["id"], "name": r["name"], "status": r["status"],
                   "notes": r["notes"], "position": r["position"]} for r in rows],
    }

    archive_db.close()

    # Write atomically so partial restarts don't corrupt archives
    fd, tmp_path = tempfile.mkstemp(dir=str(get_archives_dir()), prefix=".archive_", suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(snapshot_data, fh, indent=2)
    os.replace(tmp_path, str(filename))

    reds = sum(1 for r in snapshot_data["items"] if r["status"] == "red")
    total = len(snapshot_data["items"])
    print(f"Archived {total} items ({reds} red) -> {filename.name}")


# ── Queries ─────────────────────────────────────────────────────────

def get_all_items(db: sqlite3.Connection):
    """Red first, then degraded, then green — each group keeps its config-file position."""
    return db.execute(
        "SELECT * FROM status_items ORDER BY CASE status WHEN 'red' THEN 0 WHEN 'degraded' THEN 1 ELSE 2 END, position"
    ).fetchall()


def get_item_by_id(db: sqlite3.Connection, item_id: int):
    return db.execute("SELECT id, name, status, notes, position FROM status_items WHERE id=?", (item_id,)).fetchone()


def get_item_name(db: sqlite3.Connection, item_id: int) -> str | None:
    row = db.execute("SELECT name FROM status_items WHERE id=?", (item_id,)).fetchone()
    return row["name"] if row else None


# ── Mutations ───────────────────────────────────────────────────────

def toggle_status(db: sqlite3.Connection, item_id: int) -> str:
    """Cycle: green → degraded → red → green (also persists to yaml)."""
    row = db.execute(
        "SELECT id, name, status FROM status_items WHERE id=?", (item_id,)
    ).fetchone()
    if not row:
        return "green"

    current = row["status"]
    # Use STATUS_SORT_ORDER to determine next (green=2, degraded=1, red=0)
    # But the cycle is green → degraded → red → green
    cycle_order = ["green", "degraded", "red"]
    next_idx = (cycle_order.index(current) + 1) % len(cycle_order)
    new_status = cycle_order[next_idx]
    db.execute(
        "UPDATE status_items SET status=? WHERE id=?",
        (new_status, item_id),
    )
    return new_status


def update_name(db: sqlite3.Connection, item_id: int, name: str) -> tuple[bool, str]:
    row = db.execute("SELECT name FROM status_items WHERE id=?", (item_id,)).fetchone()
    if not row:
        return False, "Not found"
    old_name = row["name"]
    if old_name == name:
        return True, "No change"

    # Check for name conflict
    conflict = db.execute("SELECT id FROM status_items WHERE name=? AND id!=?", (name, item_id)).fetchone()
    if conflict:
        return False, "Item already exists"

    db.execute(
        "UPDATE status_items SET name = ? WHERE id = ?", (name, item_id)
    )
    return True, "OK"


def reorder(db: sqlite3.Connection, order_map: dict[int, int]) -> None:
    for item_id, order in order_map.items():
        db.execute(
            "UPDATE status_items SET position = ? WHERE id = ?",
            (order, item_id),
        )


def set_notes(db: sqlite3.Connection, item_id: int, notes: str) -> None:
    # Get current notes for history tracking
    current_row = db.execute("SELECT id, name, notes FROM status_items WHERE id=?", (item_id,)).fetchone()
    if not current_row:
        return
    old_notes = current_row["notes"] or ""

    # Persist notes to yaml _runtime.notes (if non-empty)
    if current_row and notes.strip():
        from statuspage.config import _load_runtime, _save_runtime
        rt = _load_runtime()
        item_name = current_row["name"]
        rt.setdefault("notes", {})[item_name] = notes
        _save_runtime(rt)

    db.execute(
        "UPDATE status_items SET notes = ? WHERE id = ?",
        (notes, item_id),
    )


def add_item(db: sqlite3.Connection, name: str) -> int:
    max_pos = db.execute("SELECT COALESCE(MAX(position), 0) FROM status_items").fetchone()[0]
    cursor = db.execute(
        "INSERT INTO status_items (name, status, position) VALUES (?, 'green', ?)",
        (name, max_pos + 1),
    )
    new_id = cursor.lastrowid
    # Persist item names to _runtime.items so they survive restarts
    all_names = [r["name"] for r in db.execute("SELECT name FROM status_items ORDER BY position").fetchall()]
    from statuspage.config import _load_runtime, _save_runtime
    rt = _load_runtime()
    rt["items"] = all_names
    _save_runtime(rt)
    return new_id


def delete_item(db: sqlite3.Connection, item_id: int) -> str | None:
    row = db.execute("SELECT name FROM status_items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        return None
    name = row["name"]
    db.execute("DELETE FROM status_history WHERE item_id = ?", (item_id,))
    db.execute("DELETE FROM status_items WHERE id = ?", (item_id,))
    # Re-index positions to fill the gap
    remaining = db.execute("SELECT id, position FROM status_items ORDER BY position").fetchall()
    for i, r in enumerate(remaining):
        db.execute("UPDATE status_items SET position = ? WHERE id = ?", (i, r["id"]))

    # Update runtime config to prune deleted item
    from statuspage.config import _load_runtime, _save_runtime
    rt = _load_runtime()
    if "items" in rt and name in rt["items"]:
        rt["items"] = [n for n in rt["items"] if n != name]
    if "status" in rt:
        rt["status"].pop(name, None)
    if "notes" in rt:
        rt["notes"].pop(name, None)
    if "history" in rt:
        rt["history"].pop(name, None)
    _save_runtime(rt)

    # Prune healthcheck config for the deleted item so admin endpoints and
    # the worker don't keep probing a name that no longer maps to an item.
    from statuspage.config import _load_healthchecks, _save_healthchecks
    hcs = _load_healthchecks()
    if name in hcs:
        del hcs[name]
        _save_healthchecks(hcs)

    return name


# ── History ─────────────────────────────────────────────────────────

def record_history(db: sqlite3.Connection, item_id: int, event_type: str, old_value: str, new_value: str) -> None:
    """Insert a history row and prune old entries. Called inside same transaction as mutation."""
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    db.execute(
        "INSERT INTO status_history (item_id, event_type, old_value, new_value, occurred) VALUES (?, ?, ?, ?, ?)",
        (item_id, event_type, old_value, new_value, ts),
    )
    # Prune oldest entries beyond retention limit FOR THIS ITEM only.
    # (The outer item_id filter is required: the subquery only lists this
    # item's kept ids, so without it every OTHER item's history would be
    # wiped the moment this item records an event.)
    db.execute(
        "DELETE FROM status_history WHERE item_id = ? AND id NOT IN ("
        "  SELECT id FROM status_history WHERE item_id = ? ORDER BY id DESC LIMIT ?"
        ")",
        (item_id, item_id, MAX_HISTORY_PER_ITEM),
    )

    # ── Persist to YAML _runtime.history so it survives restarts ──
    row_name = db.execute(
        "SELECT name FROM status_items WHERE id=?", (item_id,)
    ).fetchone()
    if row_name:
        from statuspage.config import _load_runtime, _save_runtime
        rt = _load_runtime()
        hist = rt.setdefault("history", {})
        item_hist = hist.setdefault(row_name["name"], [])
        item_hist.append({
            "event_type": event_type,
            "old_value": old_value,
            "new_value": new_value,
            "occurred": ts,
        })
        # Keep only the most recent HISTORY_RUNTIME_CAP entries per item in YAML
        hist[row_name["name"]] = item_hist[-HISTORY_RUNTIME_CAP:]
        _save_runtime(rt)


def get_history(db: sqlite3.Connection, item_id: int):
    """Return history for a service. Public read — anyone can see status timeline."""
    row = db.execute(
        "SELECT id, name FROM status_items WHERE id=?", (item_id,)
    ).fetchone()
    if not row:
        return None

    entries = db.execute(
        "SELECT event_type, old_value, new_value, occurred "
        "FROM status_history WHERE item_id = ? ORDER BY id DESC",
        (item_id,),
    ).fetchall()

    return {
        "service": row["name"],
        "entries": [
            {
                "event_type": e["event_type"],
                "old_value": e["old_value"],
                "new_value": e["new_value"],
                "occurred": e["occurred"],
            }
            for e in entries
        ]
    }


# Import os at module level for archive_db_snapshot
import os