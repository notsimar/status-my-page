#!/usr/bin/env python3
"""Verify UI-added items survive DB re-initialization (restart simulation).

Tests the round-trip: add item via API → init_db() preserves existing items in DB →
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
import statuspage.config as _cfg
import statuspage.auth as _auth
import statuspage.db as _dbmod
import app as app_obj


@pytest.fixture()
def restart_app(A):
    """Session-scoped fix from conftest is reused — we only need the app module."""
    _auth._failed_logins.clear()
    _auth._mutation_rates.clear()
    _auth._csrf_failures.clear()
    yield A


def test_add_item_survives_restart(restart_app):
    """Add a new item via the API, then init_db() again; verify it persists."""
    A = restart_app
    client = app_obj.app.test_client()

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
    with app_obj.app.test_request_context():
        _dbmod.init_db()

    # 5. Verify the item is still in the DB after "restart"
    db = sqlite3.connect(str(_cfg.get_db_path()))
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
    client = app_obj.app.test_client()

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
    import time
    new_name = f"TempDeleteMe_{int(time.time() * 1000)}"
    add_res = client.post("/api/add", data=json.dumps({"name": new_name}),
                          content_type="application/json", headers={"X-CSRF-Token": token})
    assert add_res.status_code == 200

    # Grab rotated CSRF token
    tok_res = client.get("/api/csrf-token")
    assert tok_res.status_code == 200
    token = tok_res.get_json()["token"]

    # Grab item ID and delete
    db = sqlite3.connect(str(_cfg.get_db_path()))
    item_id = db.execute("SELECT id FROM status_items WHERE name=?", (new_name,)).fetchone()
    assert item_id is not None
    item_id = item_id[0]

    r = client.post(f"/api/delete/{item_id}", headers={"X-CSRF-Token": token})
    assert r.status_code == 200
    db.close()

    # Restart simulation
    with app_obj.app.test_request_context():
        _dbmod.init_db()

    # Verify it's gone
    db = sqlite3.connect(str(_cfg.get_db_path()))
    names = {row[0] for row in db.execute("SELECT name FROM status_items").fetchall()}
    db.close()
    assert new_name not in names
