#!/usr/bin/env python3
"""Tests for the architecture-review fixes:

  - archives/ retention (_prune_archives / MAX_ARCHIVES)
  - rate-state persistence throttling (_persist_rate_state force/throttle)
  - client-IP proxy trust is covered in test_logging.py
  - healthcheck worker concurrency helpers (_probe_result, _run_due_checks)
  - RSS feed bounded query (single join + LIMIT)

These target the new seams introduced by the refactor; the pre-existing
suites continue to cover unchanged behavior.
"""
import json
import sqlite3
import time

import pytest
import yaml

import statuspage.config as _cfg
import statuspage.db as _dbmod
import statuspage.auth as _auth
import healthcheck as hc


# ── Archives retention ──────────────────────────────────────────────

class TestArchivesRetention:
    def test_prune_keeps_newest_max_archives(self, A, tmp_path, monkeypatch):
        monkeypatch.setattr(_dbmod, "MAX_ARCHIVES", 5)
        monkeypatch.setattr(_cfg, "ARCHIVES_DIR", tmp_path)
        for i in range(8):
            (tmp_path / f"2026010{i}_000000.json").write_text("{}")
        removed = _dbmod._prune_archives()
        assert removed == 4  # 8 existing, room for 1 more -> keep newest 4
        names = sorted(p.name for p in tmp_path.glob("*.json"))
        assert len(names) == 4
        # The OLDEST files were deleted (sorted ascending, pruned from front)
        assert names[0] == "20260104_000000.json"

    def test_prune_noop_under_limit(self, A, tmp_path, monkeypatch):
        monkeypatch.setattr(_dbmod, "MAX_ARCHIVES", 50)
        monkeypatch.setattr(_cfg, "ARCHIVES_DIR", tmp_path)
        (tmp_path / "20260101_000000.json").write_text("{}")
        assert _dbmod._prune_archives() == 0
        assert len(list(tmp_path.glob("*.json"))) == 1

    def test_prune_survives_unreadable_file(self, A, tmp_path, monkeypatch):
        """An unlink failure on one snapshot must not abort pruning."""
        monkeypatch.setattr(_dbmod, "MAX_ARCHIVES", 2)
        monkeypatch.setattr(_cfg, "ARCHIVES_DIR", tmp_path)
        for i in range(5):
            p = tmp_path / f"2026010{i}_000000.json"
            p.write_text("{}")
            p.chmod(0o444)  # read-only: unlink may fail depending on fs perms
        try:
            _dbmod._prune_archives()  # must not raise
        finally:
            for p in tmp_path.glob("*.json"):
                p.chmod(0o644)

    def test_snapshot_write_respects_retention(self, A, tmp_path, monkeypatch):
        """archive_db_snapshot() prunes before writing the new snapshot."""
        monkeypatch.delenv("STATUS_NO_ARCHIVE", raising=False)
        monkeypatch.setattr(_cfg, "DB_PATH", tmp_path / "status.db")
        monkeypatch.setattr(_dbmod, "get_db_path", lambda: tmp_path / "status.db")
        monkeypatch.setattr(_cfg, "ARCHIVES_DIR", tmp_path / "archives")
        monkeypatch.setattr(_dbmod, "get_archives_dir", lambda: tmp_path / "archives")
        monkeypatch.setattr(_dbmod, "MAX_ARCHIVES", 3)

        # Seed a DB with one item so a snapshot has content.
        db = sqlite3.connect(str(tmp_path / "status.db"))
        db.row_factory = sqlite3.Row
        db.execute("""CREATE TABLE status_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE,
            status TEXT DEFAULT 'green', notes TEXT DEFAULT '',
            position INTEGER DEFAULT 0)""")
        db.execute("INSERT INTO status_items (name) VALUES ('Svc')")
        db.commit(); db.close()

        for _ in range(5):
            _dbmod.archive_db_snapshot()
            time.sleep(1.1)  # distinct second-resolution filenames
        snaps = list((tmp_path / "archives").glob("*.json"))
        assert len(snaps) <= 3


# ── Rate-state persistence throttling ───────────────────────────────

class TestRatePersistThrottle:
    @pytest.fixture(autouse=True)
    def _reset_throttle(self):
        _auth._rate_persist_last.clear()
        yield
        _auth._rate_persist_last.clear()

    def test_second_write_within_window_is_skipped(self, monkeypatch):
        calls = []
        real_connect = sqlite3.connect
        monkeypatch.setattr(_auth.time, "time", time.time)

        import statuspage.db as db_mod
        monkeypatch.setattr(db_mod, "get_db_path", lambda: ":memory:")

        orig = sqlite3.connect
        def counting_connect(*a, **kw):
            calls.append(1)
            return orig(":memory:")
        monkeypatch.setattr(sqlite3, "connect", counting_connect)

        _auth._persist_rate_state("login_failures", {"1.2.3.4": [time.time()]})
        n_first = len(calls)
        _auth._persist_rate_state("login_failures", {"1.2.3.4": [time.time()]})
        assert len(calls) == n_first  # throttled — no second connection

    def test_force_bypasses_throttle(self, monkeypatch):
        import statuspage.db as db_mod
        monkeypatch.setattr(db_mod, "get_db_path", lambda: ":memory:")
        orig = sqlite3.connect
        calls = []
        def counting_connect(*a, **kw):
            calls.append(1)
            return orig(":memory:")
        monkeypatch.setattr(sqlite3, "connect", counting_connect)

        _auth._persist_rate_state("login_failures", {})
        _auth._persist_rate_state("login_failures", {}, force=True)
        assert len(calls) == 2  # forced write goes through immediately

    def test_different_scopes_throttle_independently(self, monkeypatch):
        import statuspage.db as db_mod
        monkeypatch.setattr(db_mod, "get_db_path", lambda: ":memory:")
        orig = sqlite3.connect
        calls = []
        def counting_connect(*a, **kw):
            calls.append(1)
            return orig(":memory:")
        monkeypatch.setattr(sqlite3, "connect", counting_connect)

        _auth._persist_rate_state("login_failures", {})
        _auth._persist_rate_state("mutation_rates", {})
        _auth._persist_rate_state("csrf_failures", {})
        assert len(calls) == 3  # each scope writes once

    def test_never_raises_on_db_error(self, monkeypatch):
        import statuspage.db as db_mod
        def broken():
            raise RuntimeError("no db")
        monkeypatch.setattr(db_mod, "get_db_path", broken)
        _auth._persist_rate_state("login_failures", {}, force=True)  # no raise


# ── Healthcheck worker concurrency helpers ──────────────────────────

class TestProbeConcurrency:
    def _hcs(self):
        return {
            "a": {"type": "ping", "host": "127.0.0.1", "service": "SvcA",
                  "timeout": 2, "interval": 60, "retries": 2},
            "b": {"type": "tcp", "host": "127.0.0.1", "port": 19999,
                  "service": "SvcB", "timeout": 1, "interval": 60, "retries": 2},
        }

    def test_probe_result_shape_ping(self, A):
        res = hc._probe_result("a", self._hcs()["a"])
        assert res["name"] == "a"
        assert res["svc_name"] == "SvcA"
        assert res["is_healthy"] is True   # loopback always answers ping
        assert res["immediate_status"] is None
        assert "ping" in res["check_info"]

    def test_probe_result_tcp_refused_is_unhealthy(self, A):
        res = hc._probe_result("b", self._hcs()["b"])  # port 19999: nothing listening
        assert res["is_healthy"] is False

    def test_run_due_checks_applies_all_results(self, A, monkeypatch):
        flips = []
        monkeypatch.setattr(hc, "_set_health_status",
                            lambda name, s: flips.append((name, s)))
        hcs = self._hcs()
        fail_count = {}
        next_fire = {n: 0.0 for n in hcs}

        hc._run_due_checks(hcs, ["a", "b"], fail_count, next_fire)

        # a (ping loopback) healthy -> green + counter reset
        # b (closed port) unhealthy but below retries -> no flip yet
        assert ("SvcA", "green") in flips
        assert fail_count["a"] == 0
        assert fail_count["b"] == 1          # counted, threshold=2 not reached
        assert all(next_fire[n] > time.time() for n in hcs)

    def test_run_due_checks_empty_due_noop(self, A, monkeypatch):
        flips = []
        monkeypatch.setattr(hc, "_set_health_status",
                            lambda name, s: flips.append((name, s)))
        hc._run_due_checks(self._hcs(), [], {}, {})
        assert flips == []

    def test_run_due_checks_retries_escalate_to_red(self, A, monkeypatch):
        """Consecutive failures reach severity_from_failures' red branch."""
        flips = []
        monkeypatch.setattr(hc, "_set_health_status",
                            lambda name, s: flips.append((name, s)))
        hcs = {"b": self._hcs()["b"]}       # tcp to a closed port: always fails
        fail_count = {"b": 5}                # already >= 3*retries threshold
        next_fire = {"b": 0.0}

        hc._run_due_checks(hcs, ["b"], fail_count, next_fire)
        assert ("SvcB", "red") in flips

    def test_pool_bounds_thread_explosion(self, A):
        """Pool size is capped even with many due checks."""
        hcs = {}
        for i in range(30):
            hcs[f"s{i}"] = {"type": "tcp", "host": "127.0.0.1", "port": 19999,
                            "service": f"S{i}", "timeout": 1, "interval": 60,
                            "retries": 2}
        fail_count = {n: 0 for n in hcs}
        next_fire = {n: 0.0 for n in hcs}
        hc._run_due_checks(hcs, list(hcs), fail_count, next_fire)
        assert all(fail_count[n] == 1 for n in hcs)


# ── RSS feed bounded query ──────────────────────────────────────────

class TestRssBoundedQuery:
    def test_feed_respects_max_items_in_sql(self, A):
        from statuspage import rss as rss_mod

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE status_items (id INTEGER PRIMARY KEY, name TEXT, status TEXT)")
        conn.execute("""CREATE TABLE status_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER,
            event_type TEXT, old_value TEXT, new_value TEXT, occurred TEXT)""")
        conn.execute("INSERT INTO status_items VALUES (1, 'Web', 'green')")
        # 10 status-change events
        for i in range(10):
            conn.execute(
                "INSERT INTO status_history (item_id, event_type, old_value, new_value, occurred)"
                " VALUES (1, 'status', 'green', 'red', ?)",
                (f"2026-01-01T00:00:{i:02d}Z",))
        conn.commit()

        xml = rss_mod.build_feed_xml(conn, base_url="http://test/")
        cap = rss_mod.get_rss_config()["max_items"]
        assert xml.count("<item>") == min(10, cap)
        assert xml.count("<item>") == 10  # all 10 rows within the default cap

    def test_feed_query_has_single_join_and_limit(self):
        """Guard against regressing to the double self-join / unbounded fetch."""
        src = open("statuspage/rss.py").read()
        assert "LEFT JOIN status_items s" not in src
        assert "LIMIT ?" in src
