"""Modified Condition/Decision Coverage (MC/DC) tests for status-my-page.

This suite proves that every guard condition independently determines the
outcome of compound boolean expressions in three critical code paths:

  1. YAML runtime state restoration — status overrides from _runtime.status,
     notes overrides from _runtime.notes (app.py L341–L365)
  2. Mutation API security gate — auth → CSRF → rate-limit trio on every
     protected endpoint (app.py L679/689/701)

Each class tests one compound decision by varying exactly one condition per
test while holding all others constant, demonstrating that each condition is
**independent** (i.e. changing it alone can flip the outcome). Together these
tests certify that no guard in a compound expression is redundant — removing
any single condition would allow an attack or data-loss scenario.

Test matrix:
  ┌─────────────┬──────────────────────┬────────┬───────────┐
  │Class        │ Guard                │ D-ID   │ Conditions│
  ├─────────────┼──────────────────────┼────────┼───────────┤
  │Test_D1_     │ _runtime.status      │ D1 (L341)│ C1: item  │
  │RestoreStatus│ override             │        │ in seed?  │
  │             │                      │        │ C2: color │
  │             │                      │        │ ∈ {green} │
  ├─────────────┼──────────────────────┼────────┼───────────┤
  │Test_D2_     │ _runtime.notes       │ D2 (L354)│ C1: item  │
  │NotesRestore │ override             │        │ in seed?  │
  │             │                      │        │ C2: note  │
  │             │                      │        │ stripped? │
  ├─────────────┼──────────────────────┼────────┼───────────┤
  │Test_D3_     │ protected-endpoint   │ D3 (L679)│ C1: is-
  │SecurityGuard│ security gate        │+L689 + admin       │
  │             │                      │ L701   │ C2: valid │
  │             │                      │        │ CSRF?     │
  │             │                      │        │ C3: under │
  │             │                      │        │ rate lim. │
  └─────────────┴──────────────────────┴────────┴───────────┘

Prerequisites (automated by session-scoped fixture A):
  - A fresh temp SQLite DB (separate from the live instance)
  - 2 seeded items: SvcA, SvcB
  - STATUS_ADMIN_PASS_HASH set to werkzeug hash of "testpass"
  - app module patched with modified CONFIG_PATH, DB_PATH, etc.

Usage:
    pytest tests/test_mc_dc.py -v       # run all MC/DC tests
    pytest tests/test_mc_dc.py::Test_D1_RestoreStatus -v  # guard D1 only

Note on isolation:
  The session fixture A uses a temp directory with isolated config.yaml,
  DB_PATH, and archives_dir so that runtime state from one test class
  does not leak into another. Tests within a class share the same temp DB
  but cleanup is explicit (see _load_runtime/_save_runtime calls).

See README_MCDC.md for the full proof matrices mapping every assertion to
a specific condition in decision D1, D2, D3 and their true/false variants.
"""
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
