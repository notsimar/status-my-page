"""Business logic services for status-my-page.

Contains the core service layer operations that are independent of HTTP.
"""
from __future__ import annotations

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
        # Record history if status actually changed
        if status != old_status:
            record_history(db, item_id, "status", old_status, status)
            # Keep display order consistent with the red→degraded→green
            # policy: a newly degraded/red item moves to the top of its
            # group; recovering to green goes to the bottom of the greens.
            _move_to_group_edge(db, item_id, status)
        db.commit()
    slack_mod.enqueue_status_change(row["name"], old_status, status)
    return {"status": status}


def _move_to_group_edge(db, item_id: int, new_status: str) -> None:
    """Reposition item_id within the position ordering for its new status.

    red/degraded → front of their group; green → end of the green group.
    Positions are renumbered densely afterwards.
    """
    rank = {"red": 0, "degraded": 1, "green": 2}[new_status]
    rows = db.execute(
        "SELECT id, status FROM status_items ORDER BY "
        "CASE status WHEN 'red' THEN 0 WHEN 'degraded' THEN 1 ELSE 2 END, position"
    ).fetchall()
    others = [r["id"] for r in rows if r["id"] != item_id]
    groups: dict[int, list[int]] = {0: [], 1: [], 2: []}
    for rid in others:
        st = next(r["status"] for r in rows if r["id"] == rid)
        groups[{"red": 0, "degraded": 1, "green": 2}[st]].append(rid)
    if new_status == "green":
        groups[2].append(item_id)      # recovered: bottom of the greens
    else:
        groups[rank].insert(0, item_id)  # incident: top of its group
    ordered = groups[0] + groups[1] + groups[2]
    for pos, rid in enumerate(ordered):
        db.execute("UPDATE status_items SET position = ? WHERE id = ?", (pos, rid))


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
    """Persist a drag-reorder.

    Display policy: red items first, then degraded, then green — dragging
    only adjusts ordering WITHIN those groups (an attempt to place a
    degraded item among greens snaps it back to the degraded group on the
    next render, so we normalize here to match what will be displayed).
    """
    with get_connection() as db:
        rows = db.execute("SELECT id, status FROM status_items").fetchall()
        status_by_id = {row["id"]: row["status"] for row in rows}
        # Stable-partition the client's requested order into status groups.
        requested = [
            item_id for item_id, _pos in sorted(order_map.items(), key=lambda kv: kv[1])
            if item_id in status_by_id
        ]
        ordered: list[int] = []
        for status in ("red", "degraded", "green"):
            ordered.extend(i for i in requested if status_by_id[i] == status)
        # Any item the client didn't mention keeps its place at the end.
        ordered.extend(i for i in status_by_id if i not in set(requested))
        db_reorder(db, {item_id: pos for pos, item_id in enumerate(ordered)})
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