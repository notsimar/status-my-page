#!/usr/bin/env python3
"""Functional test suite for the Status History feature.

Validates that every mutation to a service's state is recorded with full
fidelity — capturing pre/post values, timestamps, and event type — and that
the persistence layer survives across multiple operations including notes
updates, multi-step state cycles, cleanup, and public read access.

Test matrix (T0–T11 + Cleanup + Pruning):
  ┌─────┬───────────────────────────────────────────────────┐
  │ID   │ What it validates                                │
  ├─────┼───────────────────────────────────────────────────┤
  │ T0  │ Server responds to GET / with HTTP 200           │
  │ T1  │ Admin login succeeds; session established         │
  │ T2  │ Adding a unique test service creates an item     │
  │ T3  │ Fresh item starts with empty history list        │
  │ T4  │ Status toggle records (event_type=status) entry  │
  │ T5  │ History entry contains all 4 required fields     │
  │     │   + valid ISO-8601 UTC timestamp                 │
  │ T6  │ Notes update records (event_type=notes) entry    │
  │ T7  │ Entries returned newest-first (DESC by occurred) │
  │ T8  │ Multiple toggles each produce unique transitions │
  │     │   (green-degraded, degraded-red, red-green)      │
  │ T9  │ Non-existent item_id -> HTTP 404                 │
  │ T10 │ Old vs new notes values differ in history        │
  │ T11 │ History endpoint works without authentication    │
  │ T12 │ Item deletion cascades to status_history table    │
  │ T13 │ History pruning respects MAX_HISTORY_PER_ITEM    │
  └─────┴───────────────────────────────────────────────────┘
"""

import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

import pytest

# Ensure project root is on sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


# ── Pytest Test Suite ────────────────────────────────────────────────

@pytest.fixture(scope="module", autouse=True)
def _history_on(A):
    """This suite exercises the history feature — enable it first.

    History is OFF by default for the public page; the suite turns it on
    via the same admin-side persistence path (config.yaml settings
    section) that the admin UI uses, and restores the default afterwards
    so later test modules see pristine state.
    """
    A.config._save_settings({**A.config._load_settings(), "history_enabled": True})
    yield
    A.config._save_settings({**A.config._load_settings(), "history_enabled": False})


class TestStatusHistory:
    def test_t0_server_reachable(self, client):
        """T0: Server responds with HTTP 200."""
        r = client.get("/")
        assert r.status_code == 200

    def test_t1_admin_login(self, client):
        """T1: Admin login succeeds and returns ok: true."""
        r = client.post(
            "/login",
            data=json.dumps({"user": "admin", "pass": "testpass"}),
            content_type="application/json",
        )
        assert r.status_code == 200
        assert r.get_json()["ok"] is True

    def test_t2_add_unique_service(self, admin, token):
        """T2: Adding a unique test service creates an item."""
        name = f"HistSvc_{int(time.time() * 1000)}"
        r = admin.post(
            "/api/add",
            data=json.dumps({"name": name}),
            content_type="application/json",
            headers={"X-CSRF-Token": token},
        )
        assert r.status_code == 200
        data = r.get_json()
        assert "item" in data
        assert data["item"]["name"] == name
        assert isinstance(data["item"]["id"], int)

    def test_t3_fresh_item_empty_history(self, admin, token, client):
        """T3: Fresh item starts with empty history list."""
        name = f"FreshHist_{int(time.time() * 1000)}"
        r_add = admin.post(
            "/api/add",
            data=json.dumps({"name": name}),
            content_type="application/json",
            headers={"X-CSRF-Token": token},
        )
        assert r_add.status_code == 200
        item_id = r_add.get_json()["item"]["id"]

        r_hist = client.get(f"/api/history/{item_id}")
        assert r_hist.status_code == 200
        data = r_hist.get_json()
        assert data["service"] == name
        assert data["entries"] == []

    def test_t4_status_toggle_records_history(self, admin, token, client):
        """T4: Status toggle records (event_type=status) entry."""
        name = f"ToggleHist_{int(time.time() * 1000)}"
        r_add = admin.post(
            "/api/add",
            data=json.dumps({"name": name}),
            content_type="application/json",
            headers={"X-CSRF-Token": token},
        )
        item_id = r_add.get_json()["item"]["id"]

        # Fetch fresh token
        tok = admin.get("/api/csrf-token").get_json()["token"]
        r_tog = admin.post(f"/api/toggle/{item_id}", headers={"X-CSRF-Token": tok})
        assert r_tog.status_code == 200
        assert r_tog.get_json()["status"] == "degraded"

        r_hist = client.get(f"/api/history/{item_id}")
        assert r_hist.status_code == 200
        entries = r_hist.get_json()["entries"]
        assert len(entries) == 1
        assert entries[0]["event_type"] == "status"
        assert entries[0]["old_value"] == "green"
        assert entries[0]["new_value"] == "degraded"

    def test_t5_history_entry_fields_and_timestamp(self, admin, token, client):
        """T5: History entry contains all required fields + valid ISO-8601 timestamp."""
        name = f"FieldCheck_{int(time.time() * 1000)}"
        r_add = admin.post(
            "/api/add",
            data=json.dumps({"name": name}),
            content_type="application/json",
            headers={"X-CSRF-Token": token},
        )
        item_id = r_add.get_json()["item"]["id"]

        tok = admin.get("/api/csrf-token").get_json()["token"]
        admin.post(f"/api/toggle/{item_id}", headers={"X-CSRF-Token": tok})

        r_hist = client.get(f"/api/history/{item_id}")
        entries = r_hist.get_json()["entries"]
        assert len(entries) > 0
        entry = entries[0]

        for key in ["event_type", "old_value", "new_value", "occurred"]:
            assert key in entry

        raw_ts = entry["occurred"]
        parsed_dt = dt.datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        assert parsed_dt.year >= 2024

    def test_t6_notes_update_records_history(self, admin, token, client):
        """T6: Notes update records (event_type=notes) entry."""
        name = f"NotesHist_{int(time.time() * 1000)}"
        r_add = admin.post(
            "/api/add",
            data=json.dumps({"name": name}),
            content_type="application/json",
            headers={"X-CSRF-Token": token},
        )
        item_id = r_add.get_json()["item"]["id"]

        tok = admin.get("/api/csrf-token").get_json()["token"]
        note_text = "Investigating elevated error rate"
        r_notes = admin.post(
            f"/api/notes/{item_id}",
            data=json.dumps({"notes": note_text}),
            content_type="application/json",
            headers={"X-CSRF-Token": tok},
        )
        assert r_notes.status_code == 200

        r_hist = client.get(f"/api/history/{item_id}")
        entries = r_hist.get_json()["entries"]
        assert len(entries) == 1
        assert entries[0]["event_type"] == "notes"
        assert entries[0]["old_value"] == ""
        assert entries[0]["new_value"] == note_text

    def test_t7_entries_returned_newest_first(self, admin, token, client):
        """T7: Entries returned newest-first (DESC order)."""
        name = f"OrderHist_{int(time.time() * 1000)}"
        r_add = admin.post(
            "/api/add",
            data=json.dumps({"name": name}),
            content_type="application/json",
            headers={"X-CSRF-Token": token},
        )
        item_id = r_add.get_json()["item"]["id"]

        tok = admin.get("/api/csrf-token").get_json()["token"]
        admin.post(f"/api/toggle/{item_id}", headers={"X-CSRF-Token": tok})

        tok = admin.get("/api/csrf-token").get_json()["token"]
        admin.post(
            f"/api/notes/{item_id}",
            data=json.dumps({"notes": "First note"}),
            content_type="application/json",
            headers={"X-CSRF-Token": tok},
        )

        r_hist = client.get(f"/api/history/{item_id}")
        entries = r_hist.get_json()["entries"]
        assert len(entries) >= 2
        # Newer event (notes) should precede older event (status)
        assert entries[0]["event_type"] == "notes"
        assert entries[1]["event_type"] == "status"

    def test_t8_multiple_toggles_transitions(self, admin, token, client):
        """T8: Multiple toggles capture full cycle (green->degraded->red->green)."""
        name = f"CycleHist_{int(time.time() * 1000)}"
        r_add = admin.post(
            "/api/add",
            data=json.dumps({"name": name}),
            content_type="application/json",
            headers={"X-CSRF-Token": token},
        )
        item_id = r_add.get_json()["item"]["id"]

        # 3 toggles: green -> degraded -> red -> green
        for _ in range(3):
            tok = admin.get("/api/csrf-token").get_json()["token"]
            admin.post(f"/api/toggle/{item_id}", headers={"X-CSRF-Token": tok})

        r_hist = client.get(f"/api/history/{item_id}")
        entries = [e for e in r_hist.get_json()["entries"] if e["event_type"] == "status"]
        assert len(entries) == 3

        transitions = {(e["old_value"], e["new_value"]) for e in entries}
        expected = {("green", "degraded"), ("degraded", "red"), ("red", "green")}
        assert transitions == expected

    def test_t9_nonexistent_item_404(self, client):
        """T9: Non-existent item_id returns 404."""
        r = client.get("/api/history/999999")
        assert r.status_code == 404

    def test_t10_notes_history_distinct_values(self, admin, token, client):
        """T10: Notes history only records when note value changes."""
        name = f"DupNotes_{int(time.time() * 1000)}"
        r_add = admin.post(
            "/api/add",
            data=json.dumps({"name": name}),
            content_type="application/json",
            headers={"X-CSRF-Token": token},
        )
        item_id = r_add.get_json()["item"]["id"]

        tok = admin.get("/api/csrf-token").get_json()["token"]
        admin.post(
            f"/api/notes/{item_id}",
            data=json.dumps({"notes": "Initial"}),
            content_type="application/json",
            headers={"X-CSRF-Token": tok},
        )

        # Same note again
        tok = admin.get("/api/csrf-token").get_json()["token"]
        admin.post(
            f"/api/notes/{item_id}",
            data=json.dumps({"notes": "Initial"}),
            content_type="application/json",
            headers={"X-CSRF-Token": tok},
        )

        r_hist = client.get(f"/api/history/{item_id}")
        notes_entries = [e for e in r_hist.get_json()["entries"] if e["event_type"] == "notes"]
        assert len(notes_entries) == 1

    def test_t11_history_public_read_no_auth(self, client, admin, token):
        """T11: History accessible without admin authentication."""
        name = f"PublicHist_{int(time.time() * 1000)}"
        r_add = admin.post(
            "/api/add",
            data=json.dumps({"name": name}),
            content_type="application/json",
            headers={"X-CSRF-Token": token},
        )
        item_id = r_add.get_json()["item"]["id"]

        tok = admin.get("/api/csrf-token").get_json()["token"]
        admin.post(f"/api/toggle/{item_id}", headers={"X-CSRF-Token": tok})

        # Unauthenticated client
        r_hist = client.get(f"/api/history/{item_id}")
        assert r_hist.status_code == 200
        assert len(r_hist.get_json()["entries"]) == 1

    def test_t12_delete_item_removes_history(self, admin, token, client, A):
        """T12: Deleting an item removes its history from status_history table and _runtime."""
        import sqlite3
        name = f"DeleteCascade_{int(time.time() * 1000)}"
        r_add = admin.post(
            "/api/add",
            data=json.dumps({"name": name}),
            content_type="application/json",
            headers={"X-CSRF-Token": token},
        )
        item_id = r_add.get_json()["item"]["id"]

        tok = admin.get("/api/csrf-token").get_json()["token"]
        admin.post(f"/api/toggle/{item_id}", headers={"X-CSRF-Token": tok})

        # Delete item
        tok = admin.get("/api/csrf-token").get_json()["token"]
        r_del = admin.post(f"/api/delete/{item_id}", headers={"X-CSRF-Token": tok})
        assert r_del.status_code == 200

        # Verify DB history table is cleaned up
        db = sqlite3.connect(str(A.DB_PATH))
        rows = db.execute("SELECT id FROM status_history WHERE item_id=?", (item_id,)).fetchall()
        db.close()
        assert len(rows) == 0

        # Verify runtime config history is cleaned up
        rt = A._load_runtime()
        assert name not in rt.get("history", {})

    def test_t13_history_pruning_cap(self, admin, token, client, A):
        """T13: Pruning caps status_history entries at MAX_HISTORY_PER_ITEM."""
        import sqlite3
        name = f"PruneTest_{int(time.time() * 1000)}"
        r_add = admin.post(
            "/api/add",
            data=json.dumps({"name": name}),
            content_type="application/json",
            headers={"X-CSRF-Token": token},
        )
        item_id = r_add.get_json()["item"]["id"]

        # Insert 105 entries directly into status_history
        db = sqlite3.connect(str(A.DB_PATH))
        for i in range(105):
            db.execute(
                "INSERT INTO status_history (item_id, event_type, old_value, new_value, occurred) VALUES (?, ?, ?, ?, ?)",
                (item_id, "status", "green", "red", f"2026-01-01T00:00:{i:02d}Z")
            )
        db.commit()
        db.close()

        # Trigger one mutation via _record_history
        tok = admin.get("/api/csrf-token").get_json()["token"]
        admin.post(f"/api/toggle/{item_id}", headers={"X-CSRF-Token": tok})

        db = sqlite3.connect(str(A.DB_PATH))
        count = db.execute("SELECT COUNT(*) FROM status_history WHERE item_id=?", (item_id,)).fetchone()[0]
        db.close()
        assert count <= A.MAX_HISTORY_PER_ITEM


# ── Standalone CLI Entry Point ────────────────────────────────────────

if __name__ == "__main__":
    import http.client
    import http.cookiejar
    import urllib.error
    import urllib.parse
    import urllib.request

    target = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8920"
    print(f"Running standalone history checks against {target}...")
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    req = urllib.request.Request(f"{target}/", method="GET")
    try:
        with opener.open(req, timeout=5) as resp:
            print(f"Server reachable: HTTP {resp.status}")
    except Exception as exc:
        print(f"Cannot reach server: {exc}")
        sys.exit(1)
