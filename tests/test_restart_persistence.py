#!/usr/bin/env python3
"""Verify UI-added items survive DB re-initialization (restart simulation).

Tests the round-trip:  add item via API → init_db() re-seeds from config + _runtime.items →
item is still present in the DB.
"""

import json
import os
import re as _re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["STATUS_NO_ARCHIVE"] = "1"

import pytest


@pytest.fixture()
def restart_app():
    """Session-scoped fix from conftest is reused — we only need the app module."""
    # Patch sys.path (idempotent) and import a *fresh* app.
    if str(Path(__file__).resolve().parent.parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    from werkzeug.security import generate_password_hash
    if "STATUS_ADMIN_PASS_HASH" not in os.environ or not os.environ["STATUS_ADMIN PASS_HASH"]:
        os.environ["STATUS_ADMIN_PASS_HASH"] = generate_password_hash("testpass")
    else:
        # Ensure it's valid scrypt — the env might have a stale hash.
        pass

    import app as m

    # Clear rate-limit / lock state so tests don't interfere.
    m._failed_logins.clear()
    m._mutation_rates.clear()
    m._csrf_failures.clear()

    yield m


def test_add_item_survives_restart(restart_app):
    """Add a new item via the API, then init_db() again; verify it persists."""
    A = restart_app
    client = A.app.test_client()

    # 1. Login
    r = client.post(
        "/login",
        data=json.dumps({"user": "admin", "pass": "testpass"}),
        content_type="application/json",
    )
    assert r.status_code == 200

    # 2. Grab CSRF token from the page
    import re as _re
    html = client.get("/").data.decode()
    m = _re.search(r'<meta name="csrf-token" content="([a-f0-9]+)">', html)
    assert m, "No csrf-token meta tag"
    token = m.group(1)

    # 3. Add a new item NOT in the original config.yaml items list
    new_name = "PersistenceTestService"
    r = client.post(
        "/api/add",
        data=json.dumps({"name": new_name}),
        content_type="application/json",
        headers={"X-CSRF-Token": token},
    )
    assert r.status_code in (200, 409), f"Add failed: {r.data}"

    # 4. Simulate restart — re-seed DB from config + _runtime
    with A.app.test_request_context():
        A.init_db()

    # 5. Verify the item is still in the DB after "restart"
    db = sqlite3.connect(str(A.DB_PATH))
    rows = db.execute("SELECT name FROM status_items").fetchall()
    names = {row[0] for row in rows}
    db.close()

    assert new_name in names, (
        f"'{new_name}' was lost after restart. DB items: {sorted(names)}"
    )


def test_delete_runtime_item_doesnt_corrupt(restart_app):
    """Delete a UI-added item; init_db() should NOT crash or re-add it."""
    import sqlite3

    A = restart_app
    client = A.app.test_client()

    # Login + CSRF (same flow as above)
    r = client.post(
        "/login",
        data=json.dumps({"user": "admin", "pass": "testpass"}),
        content_type="application/json",
    )
    assert r.status_code == 200

    import re as _re
    html = client.get("/").data.decode()
    csrf_match = _re.search(r'<meta name="csrf-token" content="([a-f0-9]+)">', html)
    assert csrf_match
    token = csrf_match.group(1)

    # Add then immediately remove
    new_name = "TempDeleteMe"
    client.post("/api/add", data=json.dumps({"name": new_name}),
                content_type="application/json", headers={"X-CSRF-Token": token})

    # Grab item ID and delete
    db = sqlite3.connect(str(A.DB_PATH))
    item_id = db.execute("SELECT id FROM status_items WHERE name=?", (new_name,)).fetchone()
    assert item_id is not None
    item_id = item_id[0]

    r = client.post(f"/api/delete/{item_id}", headers={"X-CSRF-Token": token})
    assert r.status_code == 200
    db.close()

    # Restart simulation
    with A.app.test_request_context():
        A.init_db()

    # Verify it's gone
    db = sqlite3.connect(str(A.DB_PATH))
    names = {row[0] for row in db.execute("SELECT name FROM status_items").fetchall()}
    db.close()
    assert new_name not in names
