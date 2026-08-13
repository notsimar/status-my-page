"""MC/DC tests for status-my-page."""
import datetime as dt
import sqlite3
import yaml
import json

# D1 (L341): if item_name not in seed_set or new_state in ('green',''): continue
class Test_D1_RestoreStatus:
    def _db(self, A):
        c = sqlite3.connect(str(A.DB_PATH))
        c.row_factory = sqlite3.Row; return c

    def test_C1_false_C2_false__restores_degraded(self, A):
        """item in seed_set, state=degraded -> enters block -> status restored."""
        rt = A._load_runtime()
        rt["status"] = {"SvcA": "degraded"}; A._save_runtime(rt)

        with A.app.test_request_context():
            A.init_db() 
        st = self._db(A).execute("SELECT status FROM status_items WHERE name='SvcA'").fetchone()["status"]
        assert st == "degraded"

    def test_C1_true_unknown_item__skipped(self, A):
        """item NOT in seed_set -> continue early (proves C1 independent)."""
        rt = A._load_runtime()
        rt["status"] = {"GHOST_SVC": "red"}; A._save_runtime(rt)

        with A.app.test_request_context():
            A.init_db()
        row = self._db(A).execute("SELECT id FROM status_items WHERE name='GHOST_SVC'").fetchone()
        assert row is None

    def test_C2_true_green_state__skipped(self, A):
        """item in seed_set but state=green -> skip (proves C2 independent)."""
        rt = A._load_runtime()
        rt["status"] = {"SvcA": "green"}; A._save_runtime(rt)

        with A.app.test_request_context():
            A.init_db() 
        st = self._db(A).execute("SELECT status FROM status_items WHERE name='SvcA'").fetchone()["status"]
        assert st == "green"

# D2 (L354): if item_name not in seed_set or not note_text.strip(): continue
class Test_D2_NotesRestore:
    def _db(self, A):
        c = sqlite3.connect(str(A.DB_PATH))
        c.row_factory = sqlite3.Row; return c

    def test_C1_false_C2_false__restores_notes(self, A):
        """item in seed_set, note has text -> entered block -> notes restored."""
        rt = A._load_runtime()
        rt["notes"] = {"SvcA": "Maintenance planned"}; A._save_runtime(rt)

        with A.app.test_request_context():
            A.init_db()
        nt = self._db(A).execute("SELECT notes FROM status_items WHERE name='SvcA'").fetchone()["notes"]
        assert nt == "Maintenance planned"

    def test_C1_true_unknown_item__skipped(self, A):
        """item NOT in seed_set -> continue early (proves C1 independent)."""
        rt = A._load_runtime()
        rt["notes"] = {"GHOST_SVC": "Some note"}; A._save_runtime(rt)

        with A.app.test_request_context():
            A.init_db()
        row = self._db(A).execute("SELECT id FROM status_items WHERE name='GHOST_SVC'").fetchone()
        assert row is None

    def test_C2_true_empty_note__skipped(self, A):
        """item in seed_set but note is blank -> skip (proves C2 independent)."""
        rt = A._load_runtime()
        rt["notes"] = {"SvcA": "  "}; A._save_runtime(rt)

        with A.app.test_request_context():
            A.init_db()
        nt = self._db(A).execute("SELECT notes FROM status_items WHERE name='SvcA'").fetchone()["notes"]
        assert nt == ""

# D3 (L679/689/701): if _not_admin() or not _check_csrf() or not _check_mutation_rate(ip): abort(403)
class Test_D3_SecurityGuard:
    def test_baseline_all_ok__success(self, admin, token):
        """Admin + Valid CSRF + Under Rate Limit -> 200 (Baseline)."""
        r = admin.post(
            "/api/toggle/1", 
            headers={"X-CSRF-Token": token},
            content_type="application/json"
        )
        assert r.status_code == 200

    def test_C1_not_admin__403(self, client):
        """Non-Admin -> 403 (Proves C1 independent)."""
        r = client.post("/api/toggle/1")
        assert r.status_code == 403

    def test_C2_bad_csrf__403(self, admin):
        """Admin + Bad CSRF -> 403 (Proves C2 independent)."""
        r = admin.post(
            "/api/toggle/1", 
            headers={"X-CSRF-Token": "wrong_token"},
            content_type="application/json"
        )
        assert r.status_code == 403

    def test_C3_rate_limited__403(self, admin, token, client):
        """Admin + Valid CSRF + Over Rate Limit -> 403 (Proves C3 independent)."""
        # Use app module from session import to mutate state
        import app as m
        ip = client.environ_base['REMOTE_ADDR']
        m._mutation_rates[ip] = [dt.datetime.now().timestamp()] * (m.MUTATION_MAX + 1)
        
        r = admin.post(
            "/api/toggle/1", 
            headers={"X-CSRF-Token": token},
            content_type="application/json"
        )
        assert r.status_code == 403
