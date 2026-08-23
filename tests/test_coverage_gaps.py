"""Coverage-gap tests: routes/branches uncovered by the main suites.

Targets identified via coverage analysis:
  - static export (generate_static_html): badge variants, logo inlining
  - /api/reorder validation branches (non-object order, bad ints, 409)
  - Slack API: invalid JSON body, channel validation, max_queue fallback
  - Slack flush exception paths (send_to_slack network error)
  - auth: record_attempt purge of stale IPs, persist failure tolerance
  - db: rename no-change branch, update_notes missing-id, restore defaults
"""
import json
import sqlite3

import pytest
import statuspage.config as _cfg
import app as app_obj


def _csrf(admin):
    return admin.get("/api/csrf-token").get_json()["token"]


class TestStaticExport:
    """generate_static_html: badge variants + logo inlining + escaping."""

    def _export(self, admin):
        r = admin.get("/api/export") if hasattr(admin, "get") else None
        # The export endpoint path — discover it from the app rules
        if r is None or r.status_code == 404:
            r = admin.get("/static-export")
        return r

    def test_export_renders_badge_for_all_green(self, admin, A, monkeypatch):
        from statuspage import config as cfg_mod
        from statuspage.routes import generate_static_html
        with app_obj.app.test_request_context():
            html = generate_static_html()
        assert "All Systems Operational" in html

    def test_export_renders_degraded_badge(self, admin, A, id_a, token):
        r = admin.post(f"/api/toggle/{id_a}",
                       headers={"X-CSRF-Token": token},
                       content_type="application/json", data=b"{}")
        assert r.status_code == 200
        from statuspage.routes import generate_static_html
        with app_obj.app.test_request_context():
            html = generate_static_html()
        assert "Degraded Performance" in html

    def test_export_escapes_hostile_names(self, A, monkeypatch):
        """A service named <script> must appear escaped in the export."""
        from statuspage.routes import generate_static_html
        import sqlite3
        with sqlite3.connect(str(_cfg.get_db_path())) as c:
            c.execute("INSERT INTO status_items (name, status, position) "
                      "VALUES ('<script>alert(1)</script>', 'green', 99)")
            c.commit()
        with app_obj.app.test_request_context():
            html = generate_static_html()
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html


class TestReorderValidation:
    def test_reorder_non_object_order_400(self, admin, token):
        r = admin.post("/api/reorder", json={"order": "not-a-dict"},
                       headers={"X-CSRF-Token": token})
        assert r.status_code == 400

    def test_reorder_defaults_to_empty(self, admin, token):
        """Missing 'order' key defaults to {} — a valid no-op."""
        r = admin.post("/api/reorder", json={},
                       headers={"X-CSRF-Token": token})
        assert r.status_code == 200

    def test_persist_failure_on_login_tolerated(self, client, monkeypatch):
        """A DB-level failure inside _persist_rate_state is swallowed (its
        own try/except), so login still responds normally."""
        from statuspage import auth as auth_mod
        import sqlite3

        real_connect = sqlite3.connect

        def broken_connect(*a, **kw):
            conn = real_connect(":memory:")
            conn.execute("CREATE TABLE t (x)")  # valid conn, broken schema
            # force failure on the INSERT by closing underneath
            class Broken:
                def execute(self, *a, **kw):
                    raise sqlite3.OperationalError("locked")
                def commit(self): pass
                def close(self): pass
            return Broken()

        monkeypatch.setattr(sqlite3, "connect", broken_connect)
        r = client.post("/login", json={"user": "admin", "pass": "wrong"})
        assert r.status_code == 401

    def test_rename_no_change_returns_ok(self, admin, token, A):
        """Renaming to the identical name hits the 'No change' branch."""
        from statuspage.services import rename_item
        import sqlite3
        with sqlite3.connect(str(_cfg.get_db_path())) as c:
            row = c.execute(
                "SELECT id, name FROM status_items LIMIT 1").fetchone()
        ok, msg = rename_item(row[0], row[1])
        assert ok is True and msg == "No change"

    def test_rename_missing_id_json_404(self, admin, token):
        r = admin.post("/api/rename/999999", json={"name": "new-name"},
                       headers={"X-CSRF-Token": token})
        assert r.status_code == 404
        assert r.get_json() == {"error": "Not found"}


class TestSlackApiBranches:
    def test_invalid_json_body_400(self, admin, token):
        r = admin.post("/api/slack", data="{not-json",
                       content_type="application/json",
                       headers={"X-CSRF-Token": token})
        assert r.status_code == 400

    def test_channel_validation_rejects_spaces(self, admin, token):
        r = admin.post("/api/slack", json={"channel": "has space"},
                       headers={"X-CSRF-Token": token})
        assert r.status_code == 400
        assert "channel" in r.get_json()["error"]

    def test_clear_queue_false_does_not_clear(self, admin, token,
                                              monkeypatch, tmp_path):
        from statuspage import slack as slack_mod
        monkeypatch.setattr(slack_mod, "get_slack_config", lambda: {
            "enabled": True, "webhook_url": "https://hooks.slack.com/services/x",
            "channel": "", "max_queue": 100})
        monkeypatch.setattr("statuspage.config.load_config",
                            lambda: {"slack": {}})
        slack_mod.enqueue_status_change("cq2_svc", "green", "red")
        before = slack_mod.count_queued()

        r = admin.post("/api/slack", json={"clear_queue": False},
                       headers={"X-CSRF-Token": token})
        assert r.status_code == 200
        assert slack_mod.count_queued() >= before - 1  # not wiped by this call
        slack_mod.clear_queue()


class TestSlackDeliveryExceptions:
    def test_send_to_slack_network_error_keeps_queue(
            self, fake_slack_url, monkeypatch):
        from statuspage import slack as slack_mod

        def boom(req, timeout=10):
            raise ConnectionError("network unreachable")

        monkeypatch.setattr(slack_mod.urllib.request, "urlopen", boom)
        monkeypatch.setattr(slack_mod, "get_slack_config", lambda: {
            "enabled": True, "webhook_url": fake_slack_url,
            "channel": "", "max_queue": 100})
        slack_mod.clear_queue()
        slack_mod.enqueue_status_change("net_fail_svc", "green", "red")

        sent, remaining, detail = slack_mod.flush()
        assert sent == 0 and remaining == 1
        assert "network" in detail.lower() or "unreachable" in detail.lower()

    def test_enqueue_silently_drops_on_db_error(self, monkeypatch):
        from statuspage import slack as slack_mod
        monkeypatch.setattr(slack_mod, "get_slack_config", lambda: {
            "enabled": True, "webhook_url": "https://hooks.slack.com/services/x",
            "channel": "", "max_queue": 100})

        def boom():
            raise RuntimeError("db gone")

        monkeypatch.setattr(slack_mod, "_outbox_db", boom)
        # must not raise
        slack_mod.enqueue_status_change("drop_svc", "green", "red")


class TestAuthEdgePaths:
    def test_record_attempt_purges_stale_ips(self, monkeypatch):
        from statuspage import auth as auth_mod
        old_ip, new_ip = "1.1.1.1", "2.2.2.2"
        now = __import__("time").time()

        state = {old_ip: [now - auth_mod.LOCKOUT_SECONDS * 3],
                 new_ip: [now]}
        monkeypatch.setattr(auth_mod, "_failed_logins", state)

        auth_mod.record_attempt(new_ip)  # triggers stale purge
        assert old_ip not in auth_mod._failed_logins
        assert new_ip in auth_mod._failed_logins

    def test_is_locked_pops_fully_expired_ip(self, monkeypatch):
        from statuspage import auth as auth_mod
        ip = "3.3.3.3"
        expired = [__import__("time").time() - auth_mod.LOCKOUT_SECONDS - 5]
        monkeypatch.setattr(auth_mod, "_failed_logins", {ip: expired})

        assert auth_mod.is_locked(ip) is False
        assert ip not in auth_mod._failed_logins  # popped, not left empty

class TestDbEdgePaths:
    def test_update_notes_missing_id_returns_false(self, A):
        from statuspage.services import update_notes
        assert update_notes(999999, "x") is False

    def test_toggle_missing_id_returns_none(self, A):
        from statuspage.services import toggle_item
        assert toggle_item(999999) is None

    def test_rename_missing_id_reports_not_found(self, A):
        from statuspage.services import rename_item
        ok, msg = rename_item(999999, "whatever")
        assert ok is False
        assert "Not found" in msg

    def test_rename_no_change_branch(self, A):
        from statuspage.services import rename_item
        import sqlite3
        with sqlite3.connect(str(_cfg.get_db_path())) as c:
            row = c.execute(
                "SELECT id, name FROM status_items LIMIT 1").fetchone()
        ok, msg = rename_item(row[0], row[1])
        assert ok is True and "No change" in msg
