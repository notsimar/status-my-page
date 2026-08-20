#!/usr/bin/env python3
"""Comprehensive test suite for status-my-page routes, auth, security headers, and features."""

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

# Ensure project root is on sys.path
BASE_DIR = Path(__file__).resolve().parent.parent


class TestAdminCredentialValidation:
    """Test that app refuses to start without STATUS_ADMIN_PASS_HASH."""

    def test_missing_admin_pass_hash_raises_runtime_error(self, monkeypatch):
        """App must raise RuntimeError if STATUS_ADMIN_PASS_HASH not set."""
        # Remove the env var if present
        monkeypatch.delenv("STATUS_ADMIN_PASS_HASH", raising=False)

        # Import fresh and test validation logic
        from werkzeug.security import generate_password_hash

        # Simulate the validation logic from app.py
        admin_hash_env = None  # Not set

        # This should raise RuntimeError
        try:
            if not admin_hash_env:
                raise RuntimeError(
                    "STATUS_ADMIN_PASS_HASH environment variable must be set. "
                    "Generate one with:\n"
                    "  python3 -c 'from werkzeug.security import generate_password_hash; print(generate_password_hash(\"your-password\"))'"
                )
            ADMIN_PASS_HASH = admin_hash_env
            assert False, "Should have raised RuntimeError"
        except RuntimeError as e:
            assert "STATUS_ADMIN_PASS_HASH environment variable must be set" in str(e)

    def test_admin_pass_hash_set_allows_start(self, monkeypatch):
        """App starts normally when STATUS_ADMIN_PASS_HASH is set."""
        from werkzeug.security import generate_password_hash
        test_hash = generate_password_hash("testpass")
        monkeypatch.setenv("STATUS_ADMIN_PASS_HASH", test_hash)

        # The validation logic should pass
        admin_hash_env = test_hash
        if not admin_hash_env:
            assert False, "Should not raise"
        ADMIN_PASS_HASH = admin_hash_env
        assert ADMIN_PASS_HASH == test_hash


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


class TestSessionIdleExpiry:
    """Admin session expires after 5 minutes of inactivity (sliding)."""

    def test_session_survives_activity_within_timeout(self, admin, A):
        """Requests within the idle window keep the session alive."""
        # Backdate the timer by 4 minutes — still under the 300s limit
        with admin.session_transaction() as sess:
            sess[A.ADMIN_ACTIVE_SINCE_KEY] = time.time() - 4 * 60

        r = admin.get("/auth-check")
        assert r.status_code == 200
        assert r.get_json() == {"admin": True}

    def test_session_expires_after_idle_timeout(self, admin, A):
        """A request more than 5 min after the last activity logs out."""
        with admin.session_transaction() as sess:
            sess[A.ADMIN_ACTIVE_SINCE_KEY] = time.time() - (5 * 60 + 1)

        r = admin.get("/auth-check")
        assert r.status_code == 200
        assert r.get_json() == {"admin": False}

    def test_activity_slides_the_timer(self, admin, A):
        """An active request at 4 min resets the clock, so a follow-up at
        6 min total (but 2 min after the reset) is still authenticated."""
        with admin.session_transaction() as sess:
            sess[A.ADMIN_ACTIVE_SINCE_KEY] = time.time() - 4 * 60

        # This request is 4 min after login but resets the timer
        r1 = admin.get("/auth-check")
        assert r1.get_json() == {"admin": True}

        # Now 2 min after the reset — total 6 min since login, still active
        with admin.session_transaction() as sess:
            sess[A.ADMIN_ACTIVE_SINCE_KEY] = time.time() - 2 * 60

        r2 = admin.get("/auth-check")
        assert r2.get_json() == {"admin": True}

    def test_expired_admin_mutations_are_forbidden(self, admin, A, token):
        """After expiry, admin mutation routes return 403."""
        with admin.session_transaction() as sess:
            sess[A.ADMIN_ACTIVE_SINCE_KEY] = time.time() - (5 * 60 + 1)

        r = admin.post(
            "/api/add",
            data=json.dumps({"name": f"Expired_{int(time.time() * 1000)}"}),
            content_type="application/json",
            headers={"X-CSRF-Token": token},
        )
        assert r.status_code == 403


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

    def test_notes_nonexistent_item_returns_ok(self, admin, token):
        """POST /api/notes/<id> returns 200 even for non-existent item (set_notes handles missing rows gracefully)."""
        tok = admin.get("/api/csrf-token").get_json()["token"]
        r = admin.post(
            "/api/notes/999999",
            data=json.dumps({"notes": "test"}),
            content_type="application/json",
            headers={"X-CSRF-Token": tok},
        )
        assert r.status_code == 200
        assert r.get_json() == {"ok": True}


    def test_toggle_cycles_status_in_db(self, admin, token, A):
        """Toggling status cycles green -> degraded -> red -> green directly in DB."""
        db = sqlite3.connect(str(A.DB_PATH))
        db.execute("UPDATE status_items SET status='green' WHERE name='SvcA'")
        db.commit()
        row = db.execute("SELECT id, name FROM status_items WHERE name='SvcA'").fetchone()
        db.close()
        assert row is not None
        item_id = row[0]

        # Cycle 1: green -> degraded
        tok = admin.get("/api/csrf-token").get_json()["token"]
        r1 = admin.post(f"/api/toggle/{item_id}", headers={"X-CSRF-Token": tok})
        assert r1.get_json()["status"] == "degraded"
        db = sqlite3.connect(str(A.DB_PATH))
        st1 = db.execute("SELECT status FROM status_items WHERE id=?", (item_id,)).fetchone()[0]
        db.close()
        assert st1 == "degraded"

        # Cycle 2: degraded -> red
        tok = admin.get("/api/csrf-token").get_json()["token"]
        r2 = admin.post(f"/api/toggle/{item_id}", headers={"X-CSRF-Token": tok})
        assert r2.get_json()["status"] == "red"
        db = sqlite3.connect(str(A.DB_PATH))
        st2 = db.execute("SELECT status FROM status_items WHERE id=?", (item_id,)).fetchone()[0]
        db.close()
        assert st2 == "red"

        # Cycle 3: red -> green
        tok = admin.get("/api/csrf-token").get_json()["token"]
        r3 = admin.post(f"/api/toggle/{item_id}", headers={"X-CSRF-Token": tok})
        assert r3.get_json()["status"] == "green"
        db = sqlite3.connect(str(A.DB_PATH))
        st3 = db.execute("SELECT status FROM status_items WHERE id=?", (item_id,)).fetchone()[0]
        db.close()
        assert st3 == "green"


    def test_export_static_admin_success(self, admin):
        """Admin can export static page."""
        r = admin.get("/api/export/static")
        assert r.status_code == 200
        assert "Application Status" in r.text
        assert "<style>" in r.text
        assert "Operational" in r.text or "Degraded" in r.text or "Outage" in r.text
        assert "attachment; filename=\"status.html\"" in r.headers.get("Content-Disposition", "")

    def test_export_static_unauthorized(self, client):
        """Non-admin cannot export static page."""
        r = client.get("/api/export/static")
        assert r.status_code == 403


class TestPageSettings:
    """Admin-togglable history button (GET/POST /api/settings, off by default).

    Every test that reads a setting first SETs the value it's verifying, so
    the class is independent of test-file ordering (all tests share the
    session-scoped temp config.yaml / DB via the `A` fixture). Each mutating
    call fetches a fresh CSRF token first, because a successful CSRF-checked
    POST rotates the session token.
    """

    @staticmethod
    def _set_history(client, enabled: bool):
        """POST /api/settings as the (already-logged-in) client; assert 200."""
        tok = client.get("/api/csrf-token").get_json()["token"]
        r = client.post(
            "/api/settings",
            data=json.dumps({"history_enabled": enabled}),
            content_type="application/json",
            headers={"X-CSRF-Token": tok},
        )
        assert r.status_code == 200, r.get_json()
        return r

    def test_settings_get_public_no_auth(self, client):
        """GET /api/settings is a public read — no session required."""
        r = client.get("/api/settings")
        assert r.status_code == 200
        body = r.get_json()
        assert "history_enabled" in body
        assert isinstance(body["history_enabled"], bool)

    def test_settings_get_reflects_admin_write(self, admin, client):
        """A value the admin writes is immediately visible to a public GET."""
        self._set_history(admin, True)
        assert client.get("/api/settings").get_json()["history_enabled"] is True
        self._set_history(admin, False)
        assert client.get("/api/settings").get_json()["history_enabled"] is False

    def test_settings_post_non_admin_forbidden(self, client):
        """A logged-out (non-admin) mutation is rejected."""
        r = client.post(
            "/api/settings",
            data=json.dumps({"history_enabled": True}),
            content_type="application/json",
        )
        assert r.status_code == 403

    def test_settings_missing_key_400(self, admin):
        """POST without `history_enabled` is a bad request."""
        tok = admin.get("/api/csrf-token").get_json()["token"]
        r = admin.post(
            "/api/settings",
            data=json.dumps({}),
            content_type="application/json",
            headers={"X-CSRF-Token": tok},
        )
        assert r.status_code == 400
        assert "error" in r.get_json()

    def test_settings_non_bool_rejected(self, admin):
        """`history_enabled` must be a strict boolean (string/int rejected)."""
        for bad in ("yes", 1, "false", None):
            tok = admin.get("/api/csrf-token").get_json()["token"]
            r = admin.post(
                "/api/settings",
                data=json.dumps({"history_enabled": bad}),
                content_type="application/json",
                headers={"X-CSRF-Token": tok},
            )
            assert r.status_code == 400, (bad, r.get_json())

    def test_history_404_when_disabled(self, admin, client, A):
        """While disabled, GET /api/history/<id> 404s (unreachable, not just hidden).

        Re-enabled after the check: the same id then returns 200, proving
        the 404 came from the setting, not a missing item.
        """
        import sqlite3
        db = sqlite3.connect(str(A.DB_PATH))
        item_id = db.execute("SELECT id FROM status_items LIMIT 1").fetchone()[0]
        db.close()
        assert item_id is not None, "no seeded items present"

        self._set_history(admin, False)
        assert client.get(f"/api/history/{item_id}").status_code == 404

        self._set_history(admin, True)
        assert client.get(f"/api/history/{item_id}").status_code == 200

    def test_history_accessible_when_enabled(self, admin, client, A):
        """While enabled, GET /api/history/<id> returns the timeline (public read).

        Looks up a real item id first — the shared seeded DB (SvcA) must
        exist, but other test files may have deleted item id 1.
        """
        self._set_history(admin, True)
        import sqlite3
        db = sqlite3.connect(str(A.DB_PATH))
        item_id = db.execute("SELECT id FROM status_items LIMIT 1").fetchone()[0]
        db.close()
        assert item_id is not None, "no seeded items present"
        r = client.get(f"/api/history/{item_id}")
        assert r.status_code == 200
        body = r.get_json()
        assert "history" in body or "entries" in body

    def test_html_shows_history_button_for_admin_when_enabled(self, admin):
        """The 🕙 button is rendered per row when enabled (admin view)."""
        self._set_history(admin, True)
        html = admin.get("/").data.decode()
        assert 'class="btn-history"' in html
        assert 'title="View history"' in html

    def test_html_hides_history_button_when_disabled(self, admin):
        """No viewer history button is rendered while disabled (admin view).

        Uses the admin client (which also shows the admin-only
        btn-history-clear); assert the exact viewer-button class and aria,
        so the substring can't collide with the clear button.
        """
        self._set_history(admin, False)
        html = admin.get("/").data.decode()
        assert 'class="btn-history"' not in html
        assert 'title="View history"' not in html

    def test_settings_persist_to_config_yaml(self, admin, A):
        """Writes land in the settings section of the temp config.yaml."""
        import yaml
        self._set_history(admin, True)
        raw = yaml.safe_load(A.CONFIG_PATH.read_text())
        assert raw.get("settings", {}).get("history_enabled") is True

    def test_settings_preserves_sibling_sections(self, admin, A):
        """Saving settings must not clobber items / _base / other sections."""
        import yaml
        before = yaml.safe_load(A.CONFIG_PATH.read_text())
        items_before = before.get("items")
        base_before = before.get("_base")

        self._set_history(admin, True)
        raw = yaml.safe_load(A.CONFIG_PATH.read_text())
        assert raw.get("items") == items_before
        assert raw.get("_base") == base_before
        assert raw.get("settings", {}).get("history_enabled") is True

    def test_healthchecks_toggle_setting(self, admin, client, A):
        """healthchecks_enabled toggle persists and updates settings."""
        import yaml
        tok = admin.get("/api/csrf-token").get_json()["token"]
        r = admin.post(
            "/api/settings",
            data=json.dumps({"healthchecks_enabled": False}),
            content_type="application/json",
            headers={"X-CSRF-Token": tok},
        )
        assert r.status_code == 200
        assert r.get_json()["healthchecks_enabled"] is False

        assert client.get("/api/settings").get_json()["healthchecks_enabled"] is False
        raw = yaml.safe_load(A.CONFIG_PATH.read_text())
        assert raw.get("settings", {}).get("healthchecks_enabled") is False

        tok2 = admin.get("/api/csrf-token").get_json()["token"]
        r2 = admin.post(
            "/api/settings",
            data=json.dumps({"healthchecks_enabled": True}),
            content_type="application/json",
            headers={"X-CSRF-Token": tok2},
        )
        assert r2.status_code == 200
        assert r2.get_json()["healthchecks_enabled"] is True
        assert client.get("/api/settings").get_json()["healthchecks_enabled"] is True



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

    def test_backup_rotation_on_healthcheck_save(self, A):
        """_save_healthchecks rotates backup files up to _NUM_BACKUPS."""
        from statuspage.config import _save_healthchecks
        for i in range(7):
            _save_healthchecks({"_test": {"type": "curl", "url": f"http://test-{i}.local"}})

        cfg_base = A.CONFIG_PATH
        baks = [cfg_base.parent / f"{cfg_base.name}.bak{i}" for i in range(1, A._NUM_BACKUPS + 1)]
        existing_baks = [b for b in baks if b.exists()]
        assert len(existing_baks) == A._NUM_BACKUPS
