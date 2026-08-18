"""Structural tests for status-my-page API endpoints and database logic."""

import sqlite3


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
