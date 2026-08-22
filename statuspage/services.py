"""Business logic services for status-my-page.

Contains the core service layer operations that are independent of HTTP.
"""

from statuspage.db import (
    get_connection,
    get_all_items,
    get_item_by_id,
    toggle_status as db_toggle_status,
    update_name as db_update_name,
    reorder as db_reorder,
    set_notes as db_set_notes,
    add_item as db_add_item,
    delete_item as db_delete_item,
    record_history,
    get_history,
    clear_history as db_clear_history,
)
from statuspage import slack as slack_mod


# ── Status Service ──────────────────────────────────────────────────

def toggle_item(item_id: int) -> dict | None:
    """Cycle: green → degraded → red → green.

    Returns {"status": <new-status>} on success, or None when the item id
    does not exist (caller maps that to 404 — before the fix, a bad id
    just returned 'green', indistinguishable from a real toggle)."""
    with get_connection() as db:
        row = get_item_by_id(db, item_id)
        if not row:
            return None
        old_status = row["status"]  # must be captured BEFORE the toggle
        status = db_toggle_status(db, item_id)
        # Record history + queue Slack notification (best-effort)
        record_history(db, item_id, "status", old_status, status)
        db.commit()
    slack_mod.enqueue_status_change(row["name"], old_status, status)
    return {"status": status}


def rename_item(item_id: int, name: str) -> tuple[bool, str]:
    with get_connection() as db:
        row = get_item_by_id(db, item_id)
        if not row:
            return False, "Not found"
        ok, msg = db_update_name(db, item_id, name)
        if not ok:
            return ok, msg
        db.commit()
    return ok, msg


def reorder_items(order_map: dict[int, int]) -> None:
    with get_connection() as db:
        db_reorder(db, order_map)
        db.commit()


def update_notes(item_id: int, notes: str) -> bool:
    """Set the notes for an item. Returns False when the item id does not
    exist (caller maps that to 404)."""
    with get_connection() as db:
        # Get current notes for history tracking
        row = get_item_by_id(db, item_id)
        if not row:
            return False
        old_notes = row["notes"] or ""

        # Record history if notes actually changed
        if old_notes != notes:
            record_history(db, item_id, "notes", old_notes, notes)

        db_set_notes(db, item_id, notes)
        db.commit()
    return True


def add_item(name: str) -> dict:
    with get_connection() as db:
        new_id = db_add_item(db, name)
        db.commit()
        row = get_item_by_id(db, new_id)
        return {"id": new_id, "name": name, "status": "green", "notes": "", "position": row["position"]}


def delete_item(item_id: int) -> str | None:
    with get_connection() as db:
        name = db_delete_item(db, item_id)
        db.commit()
        return name


# ── History Service ─────────────────────────────────────────────────

def get_item_history(item_id: int) -> dict | None:
    with get_connection() as db:
        return get_history(db, item_id)


def clear_item_history(item_id: int) -> int | None:
    """Delete all status_history rows for an item. Returns rows removed, or
    None when the item does not exist. Caller commits via get_connection()."""
    with get_connection() as db:
        removed = db_clear_history(db, item_id)
        db.commit()
        return removed


# ── Public query service ────────────────────────────────────────────

def get_all_status_items() -> list:
    with get_connection() as db:
        return get_all_items(db)