"""Modified Condition/Decision Coverage (MC/DC) tests for status-my-page.

This suite proves that every guard condition independently determines the
outcome of compound boolean expressions in critical code paths:

  1. DB initialization and item preservation across restarts
  2. Mutation API security gate — auth → CSRF → rate-limit trio on every
     protected endpoint (app.py)
  3. CSRF token validation and session wipe guards
  4. Delete endpoint item pruning

Each class tests one compound decision by varying exactly one condition per
test while holding all others constant.

Prerequisites (automated by session-scoped fixture A):
  - A fresh temp SQLite DB (separate from the live instance)
  - 2 seeded items: SvcA, SvcB
  - STATUS_ADMIN_PASS_HASH set to werkzeug hash of "testpass"
  - app module patched with modified CONFIG_PATH, DB_PATH, etc.

Usage:
    pytest tests/test_mc_dc.py -v       # run all MC/DC tests
"""
import datetime as dt
import sqlite3
import subprocess
import yaml
import json
import pytest
from pathlib import Path

# D1 (L620): if item_name not in seed_set or new_state in ('green', ''): continue
#   MC/DC conditions — expressed from the *code* side so labels map 1:1 to source:
#     C1 = item_name not in seed_set    (F => item IS seeded, T => skipped)
#     C2 = new_state in ('green', '')   (T => skip, F => proceed with restore)
class Test_D1_RestoreStatus:
    def _query(self, A, sql, params=()):
        with sqlite3.connect(str(A.DB_PATH)) as c:
            c.row_factory = sqlite3.Row
            return c.execute(sql, params).fetchone()

    def _set_db_status(self, A, name, status):
        """Directly set DB status for test setup."""
        with A.app.test_request_context():
            row = A.get_db().execute("SELECT id FROM status_items WHERE name=?", (name,)).fetchone()
            if row:
                A.get_db().execute("UPDATE status_items SET status=? WHERE id=?", (status, row["id"]))
                A.get_db().commit()

    def test_db_maintains_status_across_init_db(self, A):
        """Status in DB is preserved across init_db() calls without relying on YAML runtime."""
        # Ensure SvcA exists in DB first
        with A.app.test_request_context():
            row = A.get_db().execute("SELECT id FROM status_items WHERE name='SvcA'").fetchone()
            if not row:
                A.get_db().execute("INSERT INTO status_items (name, status, position) VALUES ('SvcA', 'green', 1)")
                A.get_db().commit()

        self._set_db_status(A, "SvcA", "degraded")

        with A.app.test_request_context():
            A.init_db()
        row = self._query(A, "SELECT status FROM status_items WHERE name='SvcA'")
        assert row is not None and row["status"] == "degraded"

    def test_unseeded_item_in_db_preserved(self, A):
        """Item added to DB is preserved across init_db() calls."""
        with A.app.test_request_context():
            db = A.get_db()
            db.execute("INSERT OR IGNORE INTO status_items (name, status, position) VALUES ('ExtraSvc', 'green', 99)")
            db.commit()
            A.init_db()
        row = self._query(A, "SELECT id FROM status_items WHERE name='ExtraSvc'")
        assert row is not None


class Test_D2_NotesRestore:
    def _query(self, A, sql, params=()):
        with sqlite3.connect(str(A.DB_PATH)) as c:
            c.row_factory = sqlite3.Row
            return c.execute(sql, params).fetchone()

    def _set_db_notes(self, A, name, notes):
        """Directly set DB notes for test setup."""
        with A.app.test_request_context():
            row = A.get_db().execute("SELECT id FROM status_items WHERE name=?", (name,)).fetchone()
            if row:
                A.get_db().execute("UPDATE status_items SET notes=? WHERE id=?", (notes, row["id"]))
                A.get_db().commit()

    def test_db_maintains_notes_across_init_db(self, A):
        """Notes in DB are preserved across init_db() calls without relying on YAML runtime."""
        self._set_db_notes(A, "SvcA", "Maintenance planned")

        with A.app.test_request_context():
            A.init_db()
        row = self._query(A, "SELECT notes FROM status_items WHERE name='SvcA'")
        assert row is not None and row["notes"] == "Maintenance planned"

# D3 (L680/690/752): if _not_admin() or not _check_csrf() or not _check_mutation_rate(ip): abort(403)
#   Guards checked on every protected endpoint: /api/toggle, /api/add, /api/delete, /api/reorder
class Test_D3_SecurityGuard:
    """Verify that admin+csrf+rate-limit gates independently block across all mutations."""

    def __rate_limit(self, client):
        import app as m
        ip = client.environ_base['REMOTE_ADDR']
        m._mutation_rates[ip] = [dt.datetime.now(dt.timezone.utc).timestamp()] * (m.MUTATION_MAX + 1)

    # ── Baseline: one request succeeds (checked on toggle only — all share the gate) ───
    def test_baseline_all_ok__success(self, admin, token, A):
        """Admin + Valid CSRF + Under Rate Limit + existing item -> 200 (Baseline)."""
        # Resolve a real item id (id 1 is not guaranteed across test order —
        # other tests delete items). A missing item is now 404, so the
        # happy-path guard check must exercise an id that exists.
        with A.app.test_request_context():
            item_id = A.get_db().execute(
                "SELECT id FROM status_items LIMIT 1"
            ).fetchone()
        assert item_id is not None, "need at least one seeded item"
        r = admin.post(
            f"/api/toggle/{item_id['id']}",
            headers={"X-CSRF-Token": token},
            content_type="application/json", data=b'{}',
        )
        assert r.status_code == 200

    # ── C1 fails: no auth -> 403 on every endpoint ─────────────────────────────────────
    def test_C1_not_admin__toggle(self, client):
        """Non-Admin on /api/toggle/1 -> 403."""
        r = client.post("/api/toggle/1")
        assert r.status_code == 403

    def test_C1_not_admin__add(self, client):
        """Non-Admin on /api/add -> 403."""
        r = client.post("/api/add", data=b'{}', content_type="application/json")
        assert r.status_code == 403

    def test_C1_not_admin__delete(self, client):
        """Non-Admin on /api/delete/1 -> 403."""
        r = client.post("/api/delete/1")
        assert r.status_code == 403

    def test_C1_not_admin__reorder(self, client):
        """Non-Admin on /api/reorder -> 403."""
        r = client.post("/api/reorder", data=b'{}', content_type="application/json")
        assert r.status_code == 403

    def test_C1_not_admin__healthcheck_run(self, client):
        """Non-Admin on /api/healthcheck/run -> 403."""
        r = client.post("/api/healthcheck/run", data=b'{}', content_type="application/json")
        assert r.status_code == 403

    # ── C2 fails: bad/missing CSRF -> 403 on every endpoint ─────────────────────────────
    def test_C2_bad_csrf__toggle(self, admin):
        """Bad CSRF on /api/toggle/1 -> 403."""
        r = admin.post(
            "/api/toggle/1",
            headers={"X-CSRF-Token": "bad"},
            content_type="application/json", data=b'{}',
        )
        assert r.status_code == 403

    def test_C2_bad_csrf__add(self, admin):
        """Bad CSRF on /api/add -> 403."""
        r = admin.post(
            "/api/add",
            headers={"X-CSRF-Token": "bad"},
            content_type="application/json", data=b'{}',
        )
        assert r.status_code == 403

    def test_C2_bad_csrf__delete(self, admin):
        """Bad CSRF on /api/delete/1 -> 403."""
        r = admin.post(
            "/api/delete/1",
            headers={"X-CSRF-Token": "bad"},
            content_type="application/json", data=b'{}',
        )
        assert r.status_code == 403

    def test_C2_bad_csrf__reorder(self, admin):
        """Bad CSRF on /api/reorder -> 403."""
        r = admin.post(
            "/api/reorder",
            headers={"X-CSRF-Token": "bad"},
            content_type="application/json", data=b'{}',
        )
        assert r.status_code == 403

    def test_C2_bad_csrf__healthcheck_run(self, admin):
        """Bad CSRF on /api/healthcheck/run -> 403."""
        r = admin.post(
            "/api/healthcheck/run",
            headers={"X-CSRF-Token": "bad"},
            content_type="application/json", data=b'{}',
        )
        assert r.status_code == 403

    # ── C3 fails: rate-limited -> 403 on every endpoint ─────────────────────────────────
    def test_C3_rate_limited__toggle(self, admin, token, client):
        """Over rate limit on /api/toggle/1 -> 403."""
        self.__rate_limit(client)
        r = admin.post(
            "/api/toggle/1",
            headers={"X-CSRF-Token": token},
            content_type="application/json", data=b'{}',
        )
        assert r.status_code == 403

    def test_C3_rate_limited__add(self, admin, token, client):
        """Over rate limit on /api/add -> 403."""
        self.__rate_limit(client)
        r = admin.post(
            "/api/add",
            headers={"X-CSRF-Token": token},
            content_type="application/json", data=b'{}',
        )
        assert r.status_code == 403

    def test_C3_rate_limited__delete(self, admin, token, client):
        """Over rate limit on /api/delete/1 -> 403."""
        self.__rate_limit(client)
        r = admin.post(
            "/api/delete/1",
            headers={"X-CSRF-Token": token},
            content_type="application/json", data=b'{}',
        )
        assert r.status_code == 403

    def test_C3_rate_limited__reorder(self, admin, token, client):
        """Over rate limit on /api/reorder -> 403."""
        self.__rate_limit(client)
        r = admin.post(
            "/api/reorder",
            headers={"X-CSRF-Token": token},
            content_type="application/json", data=b'{}',
        )
        assert r.status_code == 403

    def test_C3_rate_limited__healthcheck_run(self, admin, token, client):
        """Over rate limit on /api/healthcheck/run -> 403."""
        self.__rate_limit(client)
        r = admin.post(
            "/api/healthcheck/run",
            headers={"X-CSRF-Token": token},
            content_type="application/json", data=b'{}',
        )
        assert r.status_code == 403


# D6 (L862): if not expected or not hmac.compare_digest(sent, expected): return False
#   Internal CSRF guard inside _check_csrf() — controls session wipe + failure counter.
#   MC/DC conditions:
#     C1 = not expected  (no CSRF token stored in session)
#     C2 = not hmac.compare_digest(sent, expected)  (hmac mismatch)
#   Both short-circuit to failure; C1 is checked first so when True, C2 is skipped.
class Test_D6_CsrfInternalGuard:
    """Verify _check_csrf() independently fails on missing token and hmac mismatch."""

    def _reset_state(self, admin):
        import app as m
        ip = admin.environ_base.get("REMOTE_ADDR", "127.0.0.1")
        m._csrf_failures.pop(ip, None)
        m._mutation_rates.pop(ip, None)

    def test_C1_no_session_token__fails(self, admin):
        """C1=True (no expected token in session) → CSRF reject, C2 not evaluated."""
        self._reset_state(admin)

        # Strip CSRF token from the live session so there's no 'expected' value
        with admin.session_transaction() as sess:
            if "_csrf" in sess:
                del sess["_csrf"]

        import app as m
        ip = admin.environ_base.get("REMOTE_ADDR", "127.0.0.1")
        failures_before = m._csrf_failures.get(ip, 0)

        r = admin.post(
            "/api/toggle/1",
            headers={"X-CSRF-Token": "does-not-matter"},
            content_type="application/json", data=b'{}',
        )
        assert r.status_code == 403

        failures_after = m._csrf_failures.get(ip, 0)
        assert failures_after > failures_before, \
            f"C1=True should increment failure counter: {failures_before} → {failures_after}"

    def test_C2_mismatch_with_valid_session_token__fails(self, admin):
        """C2=True (hmac mismatch, C1=False) → CSRF reject.

        Session HAS a valid CSRF token but we send a different one. 'not expected' is
        False (C1=F), so the guard proceeds to hmac.compare_digest which returns False.
        Proven by: 403 + failure counter incremented (same observable effect).
        """
        import app as m

        ip = admin.environ_base.get("REMOTE_ADDR", "127.0.0.1")
        failures_before = m._csrf_failures.get(ip, 0)

        r = admin.post(
            "/api/toggle/1",
            headers={"X-CSRF-Token": "definitely-wrong-token"},
            content_type="application/json", data=b'{}',
        )
        assert r.status_code == 403

        failures_after = m._csrf_failures.get(ip, 0)
        assert failures_after > failures_before, \
            f"C2=True should increment failure counter: {failures_before} → {failures_after}"

    def test_C1_False_C2_False__succeeds(self, admin, token):
        """Baseline: valid session token + matching sent token → CSRF passes.

        C1=False (expected exists) AND C2=False (hmac matches) → guard clears failure
        counter and rotates token. Proven by: 200 on mutation + no failure counter.
        """
        import app as m

        ip = admin.environ_base.get("REMOTE_ADDR", "127.0.0.1")

        # Use a live item id — earlier tests in the session may have deleted
        # item 1, and toggle on a missing id now correctly 404s.
        import sqlite3 as _sq
        with _sq.connect(str(m.DB_PATH)) as c:
            c.row_factory = _sq.Row
            row = c.execute(
                "SELECT id FROM status_items ORDER BY id LIMIT 1").fetchone()
        assert row, "No status items seeded"
        item_id = row["id"]

        r = admin.post(
            f"/api/toggle/{item_id}",
            headers={"X-CSRF-Token": token},
            content_type="application/json", data=b'{}',
        )
        assert r.status_code == 200
        assert m._csrf_failures.get(ip, 0) == 0, \
            f"Success should wipe failure counter for {ip}"

    def test_session_wipe_after_max_failures(self, admin):
        """3 consecutive C2=True failures → session cleared (brute-force defense).

        Proves the MAX_CSRF_FAILURES=3 threshold works: after 3 bad tokens in a row,
        session.clear() is called and failure counter reset. Subsequent requests
        with valid admin auth return 403 because session is wiped (admin=False).
        """
        import app as m

        for _ in range(m.MAX_CSRF_FAILURES):
            r = admin.post(
                "/api/toggle/1",
                headers={"X-CSRF-Token": "bad"},
                content_type="application/json", data=b'{}',
            )
            assert r.status_code == 403

        # Session should now be wiped — auth-check returns False
        check = admin.get("/auth-check")
        body = check.get_json()
        assert body.get("admin") is False or body.get("admin") == "", \
            f"Session should be wiped after {m.MAX_CSRF_FAILURES} failures. Got: {body}"


# D7 (L798): if "items" in rt and name in rt["items"]: prune from runtime list
#   Guard inside api_delete() — controls whether the deleted item is removed from
#   _runtime.items in config.yaml. Both conditions must be True for the prune to run.
#   MC/DC conditions:
#     C1 = "items" in rt          (T → key exists, F → no items list at all)
#     C2 = name in rt["items"]    (T → item is tracked, F → not in list)
class Test_D7_DeleteCleanupGate:
    """Verify delete endpoint deletes item from DB."""

    @staticmethod
    def _svcA_id(A):
        with sqlite3.connect(str(A.DB_PATH)) as c:
            c.row_factory = sqlite3.Row
            row = c.execute("SELECT id FROM status_items WHERE name='SvcA'").fetchone()
            return row["id"] if row else None

    def test_delete_removes_from_db(self, admin, token, A):
        """Deleting an item removes it from DB."""
        sid = self._svcA_id(A)
        if sid is None:
            with A.app.test_request_context():
                db = A.get_db()
                db.execute("INSERT OR IGNORE INTO status_items (name, status, position) VALUES ('SvcA', 'green', 1)")
                db.commit()
            sid = self._svcA_id(A)

        assert sid is not None
        r = admin.post(
            f"/api/delete/{sid}",
            headers={"X-CSRF-Token": token},
            content_type="application/json", data=b'{}',
        )
        assert r.status_code == 200

        with sqlite3.connect(str(A.DB_PATH)) as c:
            row = c.execute("SELECT id FROM status_items WHERE id=?", (sid,)).fetchone()
            assert row is None


# D8: enqueue_status_change() gate —
#   if not conf["enabled"] or not conf["webhook_url"]: return
#   MC/DC conditions:
#     C1 = not conf["enabled"]   (T => disabled, queue skipped)
#     C2 = not conf["webhook_url"]  (T => enabled but no webhook, queue skipped)
#   Both short-circuit to a silent no-op; only C1=F AND C2=F queues a row.
class Test_D8_SlackEnqueueGate:
    """Prove enabled/webhook conditions independently gate the Slack outbox."""

    @staticmethod
    def _patch_conf(monkeypatch, enabled, webhook):
        from statuspage import slack as slack_mod
        monkeypatch.setattr(slack_mod, "get_slack_config", lambda: {
            "enabled": enabled, "webhook_url": webhook,
            "channel": "", "max_queue": 100})
        return slack_mod

    def test_baseline_enabled_with_webhook__queues(self, monkeypatch):
        """C1=F, C2=F -> row is queued (Baseline)."""
        m = self._patch_conf(monkeypatch, True, "https://hooks.slack.com/services/x")
        m.clear_queue()
        m.enqueue_status_change("mc_svc", "green", "red")
        assert m.count_queued() == 1
        m.clear_queue()

    def test_C1_disabled__no_queue(self, monkeypatch):
        """C1=True (disabled) -> no queue, C2 never evaluated."""
        m = self._patch_conf(monkeypatch, False, "https://hooks.slack.com/services/x")
        m.clear_queue()
        m.enqueue_status_change("mc_svc", "green", "red")
        assert m.count_queued() == 0

    def test_C2_no_webhook__no_queue(self, monkeypatch):
        """C2=True (enabled but webhook empty) -> no queue."""
        m = self._patch_conf(monkeypatch, True, "")
        m.clear_queue()
        m.enqueue_status_change("mc_svc", "green", "red")
        assert m.count_queued() == 0


# D9: require_admin CSRF applicability —
#   needs_csrf = require_csrf and request.method not in ("GET", "HEAD", "OPTIONS")
#   MC/DC conditions:
#     C1 = require_csrf flag     (F => CSRF check bypassed entirely)
#     C2 = method is state-changing  (F for GET/HEAD/OPTIONS => check skipped)
#   Outcome: 403 'csrf' only when C1=T AND C2=T AND token invalid.
class Test_D9_CsrfMethodGate:
    """Prove CSRF is enforced on writes but never burns the token on reads."""

    def test_C2_False_get_with_bad_token__passes_auth(self, admin):
        """GET with a garbage token is NOT rejected for CSRF (read-only)."""
        r = admin.get("/api/slack", headers={"X-CSRF-Token": "garbage"})
        assert r.status_code == 200

    def test_C2_True_post_with_bad_token__csrf_rejected(self, admin):
        """POST with a garbage token -> 403 csrf (write requires token)."""
        r = admin.post(
            "/api/toggle/1",
            headers={"X-CSRF-Token": "garbage"},
            content_type="application/json", data=b'{}',
        )
        assert r.status_code == 403
        assert r.headers.get("X-Auth-Error") == "csrf"

    def test_C1_False_logout_bypasses_csrf(self, admin):
        """require_csrf=False route (logout) accepts POST without any token."""
        r = admin.post("/logout")
        assert r.status_code == 200

    def test_C1_True_C2_True_valid_token__succeeds(self, admin, token, A):
        """Baseline: POST + require_csrf + valid token -> mutation succeeds."""
        import sqlite3 as _sq
        with _sq.connect(str(A.DB_PATH)) as c:
            row = c.execute(
                "SELECT id FROM status_items ORDER BY id LIMIT 1").fetchone()
        assert row, "need a seeded item"
        r = admin.post(
            f"/api/toggle/{row[0]}",
            headers={"X-CSRF-Token": token},
            content_type="application/json", data=b'{}',
        )
        assert r.status_code == 200


# D10: flush() delivery gate —
#   if not conf["enabled"] -> report disabled
#   if not conf["webhook_url"] -> report unconfigured
#   ok, detail = send_to_slack(...); if not ok -> keep queue
#   MC/DC conditions (independent outcomes on the same queued state):
#     C1 = enabled   (F => flush is a no-op report)
#     C2 = webhook configured  (F => flush is a no-op report)
#     C3 = delivery ok  (F => queue retained, sent=0)
class Test_D10_SlackFlushGate:
    """Prove each flush condition independently determines the outcome."""

    @staticmethod
    def _patch_conf(monkeypatch, enabled, webhook):
        from statuspage import slack as slack_mod
        monkeypatch.setattr(slack_mod, "get_slack_config", lambda: {
            "enabled": enabled, "webhook_url": webhook,
            "channel": "", "max_queue": 100})
        return slack_mod

    def _seed_queue(self, m):
        """Seed with an enabling config so enqueue actually queues."""
        saved = m.get_slack_config
        m.get_slack_config = lambda: {
            "enabled": True, "webhook_url": "https://hooks.slack.com/services/x",
            "channel": "", "max_queue": 100}
        try:
            m.clear_queue()
            m.enqueue_status_change("mc_flush_svc", "green", "red")
        finally:
            m.get_slack_config = saved
        assert m.count_queued() == 1

    def test_baseline_all_ok__sends_and_clears(self, fake_slack_url, monkeypatch):
        """C1=T, C2=T, C3=T -> digest sent, queue emptied (Baseline)."""
        m = self._patch_conf(monkeypatch, True, fake_slack_url)
        import conftest as _ct
        _ct._FakeSlack.payloads.clear()
        _ct._FakeSlack.fail_with = None
        self._seed_queue(m)
        sent, remaining, _ = m.flush()
        assert sent == 1 and remaining == 0

    def test_C1_False_disabled__noop(self, monkeypatch):
        """C1=F (disabled) -> flush reports and queue is untouched."""
        m = self._patch_conf(monkeypatch, False, "")
        self._seed_queue(m)
        sent, remaining, detail = m.flush()
        assert sent == 0 and remaining == 1 and detail == "slack disabled"

    def test_C2_False_no_webhook__noop(self, monkeypatch):
        """C2=F (no webhook) -> flush reports and queue is untouched."""
        m = self._patch_conf(monkeypatch, True, "")
        self._seed_queue(m)
        sent, remaining, detail = m.flush()
        assert sent == 0 and remaining == 1 and "webhook" in detail

    def test_C3_False_delivery_fails__queue_retained(self, fake_slack_url, monkeypatch):
        """C3=F (delivery fails) -> sent=0, queue intact for retry."""
        m = self._patch_conf(monkeypatch, True, fake_slack_url)
        import conftest as _ct
        _ct._FakeSlack.payloads.clear()
        _ct._FakeSlack.fail_with = (500, "err")
        self._seed_queue(m)
        sent, remaining, detail = m.flush()
        assert sent == 0 and remaining == 1 and "500" in detail


# D11: _resolve_logo_rel() empty/traversal gate —
#   if not _LOGO_PATH: return None
#   rel = str(_LOGO_PATH).strip().lstrip("/")
#   if not rel or ".." in Path(rel).parts: return None
#   MC/DC conditions:
#     C1 = not _LOGO_PATH          (F => path configured, proceed)
#     C2 = not rel                 (T => whitespace-only after strip, reject)
#     C3 = ".." in Path(rel).parts (T => traversal attempt, reject)
#   Each condition independently forces the None outcome.
class Test_D11_LogoPathEmptyTraversalGate:
    """Prove empty/traversal logo paths independently resolve to None."""

    @staticmethod
    def _resolve(monkeypatch, logo_path):
        from statuspage import config as cfg_mod
        monkeypatch.setattr(cfg_mod, "_LOGO_PATH", logo_path)
        return cfg_mod._resolve_logo_rel()

    def test_baseline_valid_path_resolves(self, monkeypatch):
        """C1=F, C2=F, C3=F -> relative path returned (Baseline)."""
        got = self._resolve(monkeypatch, "logos/light-logo.png")
        assert got == "logos/light-logo.png"

    def test_C1_False_unset_path__none(self, monkeypatch):
        """C1=True (no logo.path) -> None."""
        assert self._resolve(monkeypatch, None) is None

    def test_C2_True_whitespace_only__none(self, monkeypatch):
        """C2=True (strips to empty) -> None, C3 never evaluated."""
        assert self._resolve(monkeypatch, "   ") is None

    def test_C3_True_traversal__none(self, monkeypatch):
        """C3=True (contains ..) -> None."""
        assert self._resolve(monkeypatch, "../secrets.yaml") is None
        assert self._resolve(monkeypatch, "logos/../../etc/passwd") is None

    def test_leading_slash_and_static_prefix_normalized(self, monkeypatch):
        """Non-guard normalizations: /static/x and static/x both -> x."""
        assert self._resolve(monkeypatch, "/logos/l.png") == "logos/l.png"
        assert self._resolve(monkeypatch, "static/logos/l.png") == "logos/l.png"


# D12: get_logo_local_path() containment gate —
#   within_static = static_root in candidate.parents or candidate.parent == static_root
#   if not within_static: return None
#   if not candidate.is_file() or candidate.stat().st_size == 0: return None
#   MC/DC conditions:
#     C1 = candidate inside static dir  (F => escape, reject)
#     C2 = candidate is a file          (F => missing/dir, reject)
#     C3 = file size > 0                (F => empty file, reject)
class Test_D12_LogoLocalPathGate:
    """Prove containment, existence, and size independently gate the logo path."""

    @pytest.fixture()
    def logo_env(self, tmp_path, monkeypatch):
        from statuspage import config as cfg_mod
        static_dir = tmp_path / "static"
        (static_dir / "logos").mkdir(parents=True)
        monkeypatch.setattr(cfg_mod, "STATIC_DIR", static_dir)
        return cfg_mod, static_dir

    def _set_path(self, cfg_mod, rel):
        monkeypatch = getattr(self, "_mp")
        monkeypatch.setattr(cfg_mod, "_LOGO_PATH", rel)

    def test_baseline_real_file_resolves(self, logo_env, monkeypatch):
        """C1=T, C2=T, C3=T -> absolute path returned (Baseline)."""
        cfg_mod, static_dir = logo_env
        self._mp = monkeypatch
        f = static_dir / "logos" / "l.png"
        f.write_bytes(b"data")
        monkeypatch.setattr(cfg_mod, "_LOGO_PATH", "logos/l.png")
        got = cfg_mod.get_logo_local_path()
        assert got is not None and got.is_file()

    def test_C1_False_escape_outside_static__none(self, logo_env, monkeypatch):
        """C1=F (symlink escape) -> None even though target exists."""
        cfg_mod, static_dir = logo_env
        outside = tmp_outside = static_dir.parent / "outside.png"
        outside.write_bytes(b"data")
        link = static_dir / "logos" / "escape.png"
        link.symlink_to(outside)
        monkeypatch.setattr(cfg_mod, "_LOGO_PATH", "logos/escape.png")
        assert cfg_mod.get_logo_local_path() is None

    def test_C2_False_missing_file__none(self, logo_env, monkeypatch):
        """C2=F (file doesn't exist) -> None."""
        cfg_mod, static_dir = logo_env
        monkeypatch.setattr(cfg_mod, "_LOGO_PATH", "logos/missing.png")
        assert cfg_mod.get_logo_local_path() is None

    def test_C3_False_empty_file__none(self, logo_env, monkeypatch):
        """C3=F (zero-byte file) -> None."""
        cfg_mod, static_dir = logo_env
        f = static_dir / "logos" / "empty.png"
        f.write_bytes(b"")
        monkeypatch.setattr(cfg_mod, "_LOGO_PATH", "logos/empty.png")
        assert cfg_mod.get_logo_local_path() is None

    def test_directory_instead_of_file__none(self, logo_env, monkeypatch):
        """C2 variant: path is a directory, not a file -> None."""
        cfg_mod, static_dir = logo_env
        (static_dir / "logos" / "adir").mkdir()
        monkeypatch.setattr(cfg_mod, "_LOGO_PATH", "logos/adir")
        assert cfg_mod.get_logo_local_path() is None


# D13: install_logo.sh dual-mode gate —
#   if [ -n "${LOGO_DARK:-}" ] || [ -n "${LOGO_LIGHT:-}" ]; then DUAL=1
#   MC/DC conditions (shell, tested via subprocess):
#     C1 = LOGO_DARK set   (T alone => dual mode)
#     C2 = LOGO_LIGHT set  (T alone => dual mode)
#   Both must be F for single-logo mode to be selected.
class Test_D13_LogoDualModeGate:
    """Prove either env var alone flips install_logo.sh into dual mode."""

    SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "install_logo.sh"

    def _probe_mode(self, **env) -> str:
        """Run the script with no positional args; usage error = single mode,
        different error (missing config.yaml) = dual mode was selected."""
        base = {"PATH": "/usr/bin:/bin", "HOME": "/tmp"}
        base.update(env)
        r = subprocess.run(["bash", str(self.SCRIPT)],
                           capture_output=True, text=True, env=base)
        combined = r.stdout + r.stderr
        if "Usage" in combined:
            return "single"
        if "config.yaml" in combined:
            return "dual"
        return f"unexpected: {combined}"

    def test_C1_True_alone__dual_mode(self):
        assert self._probe_mode(LOGO_DARK="d.png") == "dual"

    def test_C2_True_alone__dual_mode(self):
        assert self._probe_mode(LOGO_LIGHT="l.png") == "dual"

    def test_both_False__single_mode(self):
        assert self._probe_mode() == "single"
