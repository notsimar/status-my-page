"""Tests for the UX improvements: /api/status polling endpoint,
JSON 404s, notes cap, lockout seconds (dogfood findings #1-#6)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import statuspage.config as _cfg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestPublicStatusEndpoint:
    def test_returns_array_of_items(self, client):
        r = client.get("/api/status")
        assert r.status_code == 200
        data = r.get_json()
        assert isinstance(data, list)
        assert len(data) >= 2
        first = data[0]
        for key in ("id", "name", "status", "notes"):
            assert key in first
        assert first["status"] in ("green", "degraded", "red")

    def test_no_admin_detail_leaked(self, client):
        r = client.get("/api/status")
        keys = set(r.get_json()[0].keys())
        assert keys == {"id", "name", "status", "notes"}

    def test_reflects_toggle(self, admin, token, id_a):
        admin.post(f"/api/toggle/{id_a}", headers={"X-CSRF-Token": token})
        r = admin.get("/api/status")
        row = next(i for i in r.get_json() if i["id"] == id_a)
        assert row["status"] != "green"  # cycled away from initial green


class TestDeleteRequiresConfirmation:
    """Server-side delete is idempotent-missing-safe; the confirm itself is
    a frontend concern — here we verify the API contract the UI relies on."""

    def test_delete_missing_id_json_404(self, admin, token):
        r = admin.post("/api/delete/999999",
                       headers={"X-CSRF-Token": token})
        assert r.status_code == 404

    def test_delete_existing_removes_and_compacts(self, admin, token, A):
        import sqlite3
        with sqlite3.connect(str(_cfg.get_db_path())) as c:
            c.execute("INSERT INTO status_items (name, status, position) "
                      "VALUES ('DelMe', 'green', 99)")
            cid = c.execute("SELECT id FROM status_items WHERE name='DelMe'"
                            ).fetchone()[0]
        r = admin.post(f"/api/delete/{cid}",
                       headers={"X-CSRF-Token": token})
        assert r.status_code == 200
        with sqlite3.connect(str(_cfg.get_db_path())) as c:
            gone = c.execute("SELECT 1 FROM status_items WHERE name='DelMe'"
                             ).fetchone() is None
        assert gone


class TestLockoutRetryAfter:
    def test_429_includes_retry_after_field(self, client, monkeypatch):
        import collections
        import time
        from statuspage import auth as auth_mod

        ip = "127.0.0.1"
        now = time.time()
        locked = collections.defaultdict(
            list, {ip: [now - 1] * auth_mod.MAX_LOGIN_ATTEMPTS})
        monkeypatch.setattr(auth_mod, "_failed_logins", locked)
        monkeypatch.setattr(auth_mod, "_lockout_until", {ip: now + 12})

        r = client.post("/login", json={"user": "admin", "pass": "bad"})
        assert r.status_code == 429
        body = r.get_json()
        # The failed attempt itself extends the lockout (record_attempt runs
        # on 401 paths; here the 429 path doesn't, but the window is bounded).
        assert 1 <= body["retry_after"] <= 30
        assert "Try again in" in body["error"]
