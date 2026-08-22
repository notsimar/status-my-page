"""Structural tests for status-my-page API endpoints and database logic."""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class Test_D4_ReorderOverride:
    """Verify reorder API updates positions in DB."""

    @staticmethod
    def _position(db, item_name):
        row = db.execute("SELECT position FROM status_items WHERE name=?",
                         [item_name]).fetchone()
        return row["position"] if row else -1

    def test_reorder_updates_db(self, admin, token, A):
        """Reorder API updates position in DB."""
        db = sqlite3.connect(str(A.DB_PATH))
        db.row_factory = sqlite3.Row
        row_a = db.execute("SELECT id FROM status_items WHERE name='SvcA'").fetchone()
        row_b = db.execute("SELECT id FROM status_items WHERE name='SvcB'").fetchone()
        db.close()
        assert row_a and row_b

        tok = admin.get("/api/csrf-token").get_json()["token"]
        r = admin.post(
            "/api/reorder",
            headers={"X-CSRF-Token": tok},
            content_type="application/json",
            data=f'{{"order": {{"{row_b["id"]}": 0, "{row_a["id"]}": 1}}}}',
        )
        assert r.status_code == 200

        db = sqlite3.connect(str(A.DB_PATH))
        db.row_factory = sqlite3.Row
        pos_b = self._position(db, "SvcB")
        pos_a = self._position(db, "SvcA")
        db.close()
        assert pos_b < pos_a


class Test_D5_SetNotesGuard:
    """Verify set_notes updates DB notes directly."""

    def test_set_notes_updates_db(self, admin, token, A):
        db_file = sqlite3.connect(str(A.DB_PATH))
        db_file.row_factory = sqlite3.Row
        row = db_file.execute(
            "SELECT name, id FROM status_items WHERE name='SvcA'"
        ).fetchone()
        db_file.close()
        assert row is not None

        tok = admin.get("/api/csrf-token").get_json()["token"]
        r = admin.post(
            f"/api/notes/{row['id']}",
            headers={"X-CSRF-Token": tok},
            content_type="application/json",
            data='{"notes": "Service is under maintenance"}',
        )
        assert r.status_code == 200

        db_file = sqlite3.connect(str(A.DB_PATH))
        note_db = db_file.execute("SELECT notes FROM status_items WHERE id=?", (row["id"],)).fetchone()[0]
        db_file.close()
        assert note_db == "Service is under maintenance"


class Test_D11_SlackWiring:
    """Structural: Slack enqueue/flush wired into mutation + logout paths."""

    def test_toggle_queues_slack_notification(self, admin, token, A, monkeypatch):
        """Manual toggle enqueues a transition into the outbox."""
        from statuspage import slack as slack_mod

        monkeypatch.setattr(slack_mod, "get_slack_config", lambda: {
            "enabled": True, "webhook_url": "https://hooks.slack.com/services/x",
            "channel": "", "max_queue": 100})
        slack_mod.clear_queue()

        db = sqlite3.connect(str(A.DB_PATH))
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT id FROM status_items WHERE name='SvcA'").fetchone()
        db.close()
        assert row

        r = admin.post(f"/api/toggle/{row['id']}",
                       headers={"X-CSRF-Token": token},
                       content_type="application/json", data=b'{}')
        assert r.status_code == 200
        assert slack_mod.count_queued() == 1
        slack_mod.clear_queue()

    def test_logout_flushes_queue(self, A, fake_slack_url, monkeypatch):
        """Logout route delivers the digest and clears the queue."""
        import conftest as _ct
        from statuspage import slack as slack_mod

        monkeypatch.setattr(slack_mod, "get_slack_config", lambda: {
            "enabled": True, "webhook_url": fake_slack_url,
            "channel": "", "max_queue": 100})
        _ct._FakeSlack.payloads.clear()
        _ct._FakeSlack.fail_with = None

        c = A.app.test_client()
        assert c.post("/login", json={"user": "admin", "pass": "testpass"}
                      ).status_code == 200
        slack_mod.enqueue_status_change("struct_svc", "green", "red")

        r = c.post("/logout")
        assert r.status_code == 200
        assert len(_ct._FakeSlack.payloads) == 1
        assert slack_mod.count_queued() == 0

    def test_healthcheck_flip_queues(self, A, monkeypatch):
        """_set_health_status records history AND queues a notification."""
        from statuspage import slack as slack_mod
        import healthcheck as hc

        hc.configure_healthcheck_module = None  # guard against misuse
        monkeypatch.setattr(slack_mod, "get_slack_config", lambda: {
            "enabled": True, "webhook_url": "https://hooks.slack.com/services/x",
            "channel": "", "max_queue": 100})
        slack_mod.clear_queue()

        # Ensure SvcA exists and is green
        with sqlite3.connect(str(A.DB_PATH)) as c:
            c.row_factory = sqlite3.Row
            row = c.execute("SELECT id FROM status_items WHERE name='SvcA'").fetchone()
            assert row
            c.execute("UPDATE status_items SET status='green' WHERE id=?", (row["id"],))
            c.commit()

        hc._set_health_status("SvcA", "degraded")

        assert slack_mod.count_queued() == 1
        slack_mod.clear_queue()
