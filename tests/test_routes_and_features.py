#!/usr/bin/env python3
"""Comprehensive test suite for status-my-page routes, auth, security headers, and features."""

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

# Ensure project root is on sys.path
BASE_DIR = Path(__file__).resolve().parent.parent


class TestRoutesAndAuth:
    def test_auth_check_unauthenticated(self, client):
        """GET /auth-check returns admin: false when logged out."""
        r = client.get("/auth-check")
        assert r.status_code == 200
        assert r.get_json() == {"admin": False}

    def test_auth_check_authenticated(self, admin):
        """GET /auth-check returns admin: true when logged in."""
        r = admin.get("/auth-check")
        assert r.status_code == 200
        assert r.get_json() == {"admin": True}

    def test_login_invalid_password(self, client, A):
        """POST /login with wrong password returns 401."""
        A._failed_logins.clear()
        r = client.post(
            "/login",
            data=json.dumps({"user": "admin", "pass": "wrongpassword"}),
            content_type="application/json",
        )
        assert r.status_code == 401
        assert r.get_json() == {"ok": False, "error": "Invalid credentials"}

    def test_login_rate_limiting_lockout(self, client, A):
        """5 failed login attempts result in 429 lockout."""
        A._failed_logins.clear()
        r = None
        for _ in range(5):
            r = client.post(
                "/login",
                data=json.dumps({"user": "admin", "pass": "wrongpassword"}),
                content_type="application/json",
            )
        assert r is not None and r.status_code == 401

        # 6th attempt should be 429
        r_locked = client.post(
            "/login",
            data=json.dumps({"user": "admin", "pass": "testpass"}),
            content_type="application/json",
        )
        assert r_locked.status_code == 429
        assert "Too many attempts" in r_locked.get_json().get("error", "")

        # Clean up rate limit state for other tests
        A._failed_logins.clear()

    def test_logout_clears_session(self, admin):
        """POST /logout clears session and resets admin auth."""
        r_out = admin.post("/logout")
        assert r_out.status_code == 200
        assert r_out.get_json() == {"ok": True}

        r_check = admin.get("/auth-check")
        assert r_check.get_json() == {"admin": False}

    def test_csrf_token_non_admin_forbidden(self, client):
        """GET /api/csrf-token returns 403 for non-admin."""
        r = client.get("/api/csrf-token")
        assert r.status_code == 403

    def test_csrf_token_admin_success(self, admin):
        """GET /api/csrf-token returns fresh token for admin."""
        r = admin.get("/api/csrf-token")
        assert r.status_code == 200
        data = r.get_json()
        assert "token" in data
        assert len(data["token"]) == 64  # hex token


class TestItemMutations:
    def test_add_duplicate_conflict(self, admin, token):
        """POST /api/add returns 409 Conflict when item name already exists."""
        name = f"DupSvc_{int(time.time() * 1000)}"
        r1 = admin.post(
            "/api/add",
            data=json.dumps({"name": name}),
            content_type="application/json",
            headers={"X-CSRF-Token": token},
        )
        assert r1.status_code == 200

        tok2 = admin.get("/api/csrf-token").get_json()["token"]
        r2 = admin.post(
            "/api/add",
            data=json.dumps({"name": name}),
            content_type="application/json",
            headers={"X-CSRF-Token": tok2},
        )
        assert r2.status_code == 409
        assert r2.get_json() == {"error": "Item already exists"}

    def test_rename_item(self, admin, token, A):
        """POST /api/rename/<id> renames service successfully."""
        orig_name = f"OrigSvc_{int(time.time() * 1000)}"
        new_name = f"RenamedSvc_{int(time.time() * 1000)}"

        r_add = admin.post(
            "/api/add",
            data=json.dumps({"name": orig_name}),
            content_type="application/json",
            headers={"X-CSRF-Token": token},
        )
        item_id = r_add.get_json()["item"]["id"]

        tok = admin.get("/api/csrf-token").get_json()["token"]
        r_rename = admin.post(
            f"/api/rename/{item_id}",
            data=json.dumps({"name": new_name}),
            content_type="application/json",
            headers={"X-CSRF-Token": tok},
        )
        assert r_rename.status_code == 200
        assert r_rename.get_json() == {"ok": True}

        # Verify in DB
        db = sqlite3.connect(str(A.DB_PATH))
        row = db.execute("SELECT name FROM status_items WHERE id=?", (item_id,)).fetchone()
        db.close()
        assert row[0] == new_name

    def test_rename_item_rejected_on_xss(self, admin, token):
        """POST /api/rename/<id> returns 400 when name contains XSS."""
        tok = admin.get("/api/csrf-token").get_json()["token"]
        r = admin.post(
            "/api/rename/1",
            data=json.dumps({"name": "<script>alert(1)</script>"}),
            content_type="application/json",
            headers={"X-CSRF-Token": tok},
        )
        assert r.status_code == 400
        assert "error" in r.get_json()

    def test_reorder_items_success(self, admin, token, A):
        """POST /api/reorder updates positions of items in DB."""
        db = sqlite3.connect(str(A.DB_PATH))
        rows = db.execute("SELECT id FROM status_items ORDER BY position LIMIT 2").fetchall()
        db.close()
        assert len(rows) >= 2
        id1, id2 = rows[0][0], rows[1][0]

        tok = admin.get("/api/csrf-token").get_json()["token"]
        r = admin.post(
            "/api/reorder",
            data=json.dumps({"order": {str(id1): 50, str(id2): 20}}),
            content_type="application/json",
            headers={"X-CSRF-Token": tok},
        )
        assert r.status_code == 200
        assert r.get_json() == {"ok": True}

        db = sqlite3.connect(str(A.DB_PATH))
        pos1 = db.execute("SELECT position FROM status_items WHERE id=?", (id1,)).fetchone()[0]
        pos2 = db.execute("SELECT position FROM status_items WHERE id=?", (id2,)).fetchone()[0]
        db.close()
        assert pos1 == 50
        assert pos2 == 20

    def test_reorder_items_invalid_payload(self, admin, token):
        """POST /api/reorder rejects non-dict order."""
        tok = admin.get("/api/csrf-token").get_json()["token"]
        r = admin.post(
            "/api/reorder",
            data=json.dumps({"order": ["1", "2"]}),
            content_type="application/json",
            headers={"X-CSRF-Token": tok},
        )
        assert r.status_code == 400

    def test_delete_nonexistent_item_404(self, admin, token):
        """POST /api/delete/<id> returns 404 for non-existent item."""
        tok = admin.get("/api/csrf-token").get_json()["token"]
        r = admin.post(f"/api/delete/999999", headers={"X-CSRF-Token": tok})
        assert r.status_code == 404
        assert r.get_json() == {"error": "Not found"}

    def test_toggle_back_to_green_clears_runtime_status(self, admin, token, A):
        """Toggling status back to green removes the entry from _runtime.status."""
        db = sqlite3.connect(str(A.DB_PATH))
        db.execute("UPDATE status_items SET status='green' WHERE name='SvcA'")
        db.commit()
        row = db.execute("SELECT id, name FROM status_items WHERE name='SvcA'").fetchone()
        db.close()
        assert row is not None
        item_id = row[0]

        # Reset runtime status
        rt = A._load_runtime()
        rt.setdefault("status", {}).pop("SvcA", None)
        A._save_runtime(rt)

        # SvcA is in config.yaml items
        # Cycle 1: green -> degraded
        tok = admin.get("/api/csrf-token").get_json()["token"]
        r1 = admin.post(f"/api/toggle/{item_id}", headers={"X-CSRF-Token": tok})
        assert r1.get_json()["status"] == "degraded"
        rt = A._load_runtime()
        assert rt.get("status", {}).get("SvcA") == "degraded"

        # Cycle 2: degraded -> red
        tok = admin.get("/api/csrf-token").get_json()["token"]
        r2 = admin.post(f"/api/toggle/{item_id}", headers={"X-CSRF-Token": tok})
        assert r2.get_json()["status"] == "red"
        rt = A._load_runtime()
        assert rt.get("status", {}).get("SvcA") == "red"

        # Cycle 3: red -> green
        tok = admin.get("/api/csrf-token").get_json()["token"]
        r3 = admin.post(f"/api/toggle/{item_id}", headers={"X-CSRF-Token": tok})
        assert r3.get_json()["status"] == "green"
        rt = A._load_runtime()
        # Should be removed from _runtime.status when back to green
        assert "SvcA" not in rt.get("status", {})


class TestSecurityHeadersAndBackups:
    def test_security_headers(self, client):
        """Response includes all required security headers."""
        r = client.get("/")
        assert r.status_code == 200
        assert r.headers.get("X-Content-Type-Options") == "nosniff"
        assert r.headers.get("X-Frame-Options") == "DENY"
        assert r.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
        assert "camera=()" in r.headers.get("Permissions-Policy", "")
        assert "default-src 'self'" in r.headers.get("Content-Security-Policy", "")

    def test_archive_db_snapshot(self, A, monkeypatch):
        """_archive_db_snapshot creates JSON file in archives/ directory."""
        import app as m
        monkeypatch.delenv("STATUS_NO_ARCHIVE", raising=False)
        m._archive_db_snapshot()

        archives = list(m.ARCHIVES_DIR.glob("*.json"))
        assert len(archives) > 0
        latest = sorted(archives)[-1]

        with open(latest) as f:
            data = json.load(f)
        assert "timestamp" in data
        assert "items" in data
        assert isinstance(data["items"], list)
        assert len(data["items"]) >= 2
        assert "name" in data["items"][0]
        assert "status" in data["items"][0]

    def test_backup_rotation_on_runtime_save(self, A):
        """_save_runtime rotates backup files up to _NUM_BACKUPS."""
        for i in range(7):
            rt = A._load_runtime()
            rt["_test_counter"] = i
            A._save_runtime(rt)

        cfg_base = A.CONFIG_PATH
        baks = [cfg_base.parent / f"{cfg_base.name}.bak{i}" for i in range(1, A._NUM_BACKUPS + 1)]
        existing_baks = [b for b in baks if b.exists()]
        assert len(existing_baks) == A._NUM_BACKUPS
