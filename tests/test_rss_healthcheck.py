#!/usr/bin/env python3
"""Runtime behaviour of the rss healthcheck check + end-to-end status flipping.

Covers:
  _run_rss_feed_check  — real local HTTP server serving RSS/Atom feeds:
                         red / degraded / green mapping, red precedence,
                         case-insensitivity, Atom <entry> fallback, HTTP 404,
                         malformed XML, fetch failure (closed port),
                         subprocess.TimeoutExpired, missing curl binary
  One-shot entry point — POST /api/healthcheck/run serializes rss results
  _healthcheck_worker  — E2E with a REAL background worker thread: a feed
                         announcing an outage flips the item red, recovery
                         to a clean feed flips it back to green, and each
                         transition is recorded in status_history (which the
                         public RSS feed /feed.xml surfaces to readers)

All tests reuse the A fixture from conftest.py (temp config + DB environment).
"""

import sqlite3
import socket
import subprocess
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml


# ── Helpers ────────────────────────────────────────────────────────

FEED_OK = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Vendor Status</title>
<item><title>Payment API: Operational</title><description>All systems normal</description></item>
</channel></rss>
"""

FEED_RED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Vendor Status</title>
<item><title>Payment API: Major Outage</title><description>Major issue affecting all users</description></item>
</channel></rss>
"""

FEED_DEGRADED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Vendor Status</title>
<item><title>Email Service: Degraded Performance</title><description>Minor performance dip while we are investigating</description></item>
</channel></rss>
"""

ATOM_DEGRADED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Vendor</title>
<entry><title>Service degraded in region eu-west</title><summary>Some users affected</summary></entry>
</feed>
"""

WORDSET = {"red": ["outage", "down"], "degraded": ["degraded", "minor"]}


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FeedHandler(BaseHTTPRequestHandler):
    """Serves self.server.feed for /feed (HTTP 200), 404 otherwise."""

    def do_GET(self):  # noqa: N802
        if self.path == "/feed":
            body = self.server.feed.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # keep test output clean


@pytest.fixture()
def feed_server():
    """ThreadingHTTPServer on a random port.

    Yields a namespace exposing ``.url`` (base URL string) and ``.server``
    (the server object; set ``server.feed`` to change the response body).
    """
    server = ThreadingHTTPServer(("127.0.0.1", _free_port()), FeedHandler)
    server.feed = FEED_OK  # type: ignore[attr-defined]
    t = threading.Thread(target=server.serve_forever, daemon=True, name="test-feed")
    t.start()
    ns = types.SimpleNamespace(
        url=f"http://127.0.0.1:{server.server_address[1]}",
        server=server,
    )
    yield ns
    server.shutdown()
    server.server_close()


def _set_feed(server, body: str):
    server.feed = body  # type: ignore[attr-defined]


@pytest.fixture()
def clean_hc(A):
    """Reset the healthchecks section to {} before, restore after each test."""
    from statuspage.config import _load_healthchecks, _save_healthchecks
    before = _load_healthchecks()
    _save_healthchecks({})
    yield A
    _save_healthchecks(before)


# ── _run_rss_feed_check: runtime status mapping ────────────────────

class TestRssFeedCheckRuntime:
    """Real HTTP server, real curl subprocess, real XML parse."""

    def test_clean_feed_green(self, A, feed_server):
        result, code = A._run_rss_feed_check(feed_server.url + "/feed", 5, WORDSET)
        assert (result, code) == ("green", 200)

    def test_outage_feed_red(self, A, feed_server):
        _set_feed(feed_server.server, FEED_RED)
        result, code = A._run_rss_feed_check(feed_server.url + "/feed", 5, WORDSET)
        assert (result, code) == ("red", 200)

    def test_degraded_feed(self, A, feed_server):
        _set_feed(feed_server.server, FEED_DEGRADED)
        result, code = A._run_rss_feed_check(feed_server.url + "/feed", 5, WORDSET)
        assert (result, code) == ("degraded", 200)

    def test_red_takes_precedence_over_degraded(self, A, feed_server):
        _set_feed(
            feed_server.server,
            "<rss><channel><item>"
            "<title>Both major outage and degraded performance</title>"
            "<description/>"
            "</item></channel></rss>",
        )
        result, _ = A._run_rss_feed_check(feed_server.url + "/feed", 5, WORDSET)
        assert result == "red", "red keywords must beat degraded keywords"

    def test_atom_entry_fallback(self, A, feed_server):
        _set_feed(feed_server.server, ATOM_DEGRADED)
        result, code = A._run_rss_feed_check(feed_server.url + "/feed", 5, WORDSET)
        assert (result, code) == ("degraded", 200)

    def test_case_insensitive_match(self, A, feed_server):
        _set_feed(feed_server.server, FEED_RED)  # "Major Outage" (mixed case)
        result, _ = A._run_rss_feed_check(feed_server.url + "/feed", 5, WORDSET)
        assert result == "red"

    def test_http_404_is_none(self, A, feed_server):
        result, code = A._run_rss_feed_check(feed_server.url + "/missing", 5, WORDSET)
        assert (result, code) == (None, 404)

    def test_malformed_xml_is_none(self, A, feed_server):
        _set_feed(feed_server.server, "this is not xml <<<")
        result, code = A._run_rss_feed_check(feed_server.url + "/feed", 5, WORDSET)
        assert (result, code) == (None, 200)

    def test_connection_refused_is_none(self, A):
        port = _free_port()  # nothing listening
        result, code = A._run_rss_feed_check(f"http://127.0.0.1:{port}/feed", 5, WORDSET)
        assert result is None

    def test_subprocess_timeout(self, A, monkeypatch):
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="curl", timeout=5)

        monkeypatch.setattr(subprocess, "run", boom)
        result, code = A._run_rss_feed_check("http://127.0.0.1:1/feed", 5, WORDSET)
        assert (result, code) == (None, None)

    def test_missing_curl_binary(self, A, monkeypatch):
        def boom(*a, **k):
            raise FileNotFoundError("curl missing")

        monkeypatch.setattr(subprocess, "run", boom)
        result, code = A._run_rss_feed_check("http://127.0.0.1:1/feed", 5, WORDSET)
        assert (result, code) == (None, None)

    def test_no_keywords_clean_fetch_is_green(self, A, feed_server):
        _set_feed(feed_server.server, FEED_RED)
        result, code = A._run_rss_feed_check(
            feed_server.url + "/feed", 5, {"red": [], "degraded": []}
        )
        assert (result, code) == ("green", 200)


# ── _run_rss_feed_check: feed-shape edge cases ─────────────────────

class TestRssFeedCheckEdgeCases:
    """Entry cap, empty feed, and oversized-feed handling. The docstring
    claims these; pin the actual behaviour against a real local server."""

    EMPTY_FEED = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>Vendor Status</title>'
        '</channel></rss>'
    )
    # Red keyword buried in a title/description
    RED_ENTRY = '<item><title>Major outage in us-east</title></item>'

    def test_empty_feed_is_green(self, A, feed_server):
        """Valid XML with zero entries: nothing to scan -> healthy (green).

        An empty vendor feed (no active incidents) must NOT read as a fetch
        failure — the fetch succeeded, there is simply no signal.
        """
        _set_feed(feed_server.server, self.EMPTY_FEED)
        result, code = A._run_rss_feed_check(feed_server.url + "/feed", 5, WORDSET)
        assert (result, code) == ("green", 200), \
            f"empty feed should be green/200, got {result!r}/{code!r}"

    def test_entries_beyond_cap_are_not_scanned(self, A, feed_server):
        """Only the first RSS_MAX_ITEMS entries are scanned.

        A red keyword in an entry PAST the cap is invisible (stale/old
        incident), while the same keyword within the cap still trips red.
        This proves the cap actually bounds the scan window.
        """
        import healthcheck as hc
        cap = hc.RSS_MAX_ITEMS
        clean = '<item><title>All systems normal</title></item>'

        # Control: red within the cap -> red (confirms the probe works).
        within = "<rss><channel>" + clean + self.RED_ENTRY + "</channel></rss>"
        _set_feed(feed_server.server, within)
        assert A._run_rss_feed_check(feed_server.url + "/feed", 5, WORDSET)[0] == "red"

        # Same red entry pushed to position cap+1 -> must be ignored (green).
        beyond = (
            "<rss><channel>"
            + clean * cap
            + self.RED_ENTRY
            + "</channel></rss>"
        )
        _set_feed(feed_server.server, beyond)
        result = A._run_rss_feed_check(feed_server.url + "/feed", 5, WORDSET)[0]
        assert result == "green", (
            f"entry #{cap+1} past RSS_MAX_ITEMS must not be scanned, got {result!r}"
        )

    def test_oversized_feed_is_fetch_failure(self, A, feed_server):
        """A feed larger than RSS_MAX_BYTES is truncated by curl
        (--max-filesize) so the XML is malformed mid-stream -> treated as a
        fetch failure (None), NOT a green. Prevents a huge valid feed from
        being silently read as 'all clear'."""
        import healthcheck as hc
        # Pad well past the cap with a clean entry title.
        pad = "x" * (hc.RSS_MAX_BYTES + 4096)
        oversized = (
            "<rss><channel>"
            f'<item><title>preamble {pad}</title></item>'
            "</channel></rss>"
        )
        assert len(oversized.encode()) > hc.RSS_MAX_BYTES
        _set_feed(feed_server.server, oversized)
        result, code = A._run_rss_feed_check(feed_server.url + "/feed", 5, WORDSET)
        assert result is None, (
            f"oversized (> {hc.RSS_MAX_BYTES} B) feed must be a fetch failure, "
            f"got {result!r}"
        )


# ── One-shot entry point ───────────────────────────────────────────

class TestRssFeedOneShot:
    """POST /api/healthcheck/run includes rss results (dry run, no DB writes)."""

    def test_one_shot_result_shape(self, A, feed_server, admin, token, clean_hc):
        """One-shot run serializes rss results and never writes the DB.

        No status item needs to exist: run_healthchecks_once() is a pure
        dry-run over the healthchecks config (clean_hc keeps the shared
        session config pristine)."""
        from statuspage.config import _load_healthchecks, _save_healthchecks

        name = "RssOneShot"
        before = _load_healthchecks()
        before[name] = {
            "type": "rss",
            "url": feed_server.url + "/feed",
            "keywords": WORDSET,
        }
        _save_healthchecks(before)
        import healthcheck as hc
        hc.configure_healthcheck(A.get_base_dir(), A.get_db_path(),
                                 A.get_config_path(), A.load_config,
                                 A.MAX_HISTORY_PER_ITEM)
        try:
            r = admin.post("/api/healthcheck/run",
                           headers={"X-CSRF-Token": token})
            assert r.status_code == 200, r.data
            body = r.get_json()
            assert body[name]["type"] == "rss"
            assert body[name]["result"] == "green"
            assert body[name]["healthy"] is True
            assert body[name]["status_code"] == 200

            # One-shot is a dry run: no item rows nor status history created
            conn = sqlite3.connect(str(A.DB_PATH))
            items = conn.execute(
                "SELECT COUNT(*) FROM status_items WHERE name = ?", [name]
            ).fetchone()[0]
            count = conn.execute(
                "SELECT COUNT(*) FROM status_history h "
                "JOIN status_items i ON i.id = h.item_id WHERE i.name = ?", [name]
            ).fetchone()[0]
            conn.close()
            assert items == 0 and count == 0
        finally:
            del before[name]
            _save_healthchecks(before)


# ── History retention: per-item pruning (regression) ────────────────
# Both history writers (record_history in statuspage/db.py for admin
# mutations, and _set_health_status in healthcheck.py for the worker) used
# to prune with `id NOT IN (SELECT … WHERE item_id = <this>)` — which only
# lists THIS item's kept ids and therefore deleted every OTHER item's
# history the moment this item flipped. That silently destroys the timeline
# the /feed.xml publisher is built from. These tests pin the fix.

class TestHistoryPrunePerItem:
    """History pruning must be per-item, never cross-item."""

    @staticmethod
    def _make_db(path, n_items=2):
        conn = sqlite3.connect(str(path))
        conn.execute(
            """CREATE TABLE status_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'green', notes TEXT DEFAULT '',
                position INTEGER NOT NULL DEFAULT 0)"""
        )
        conn.execute(
            """CREATE TABLE status_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL,
                event_type TEXT NOT NULL DEFAULT 'status',
                old_value TEXT DEFAULT '', new_value TEXT DEFAULT '',
                occurred TEXT NOT NULL)"""
        )
        ids = []
        for i in range(n_items):
            cur = conn.execute("INSERT INTO status_items (name) VALUES (?)", [f"Item{i}"])
            ids.append(cur.lastrowid)
        conn.commit()
        conn.close()
        return ids

    def test_record_history_keeps_other_items(self, A, tmp_path, monkeypatch):
        """statuspage.db.record_history: updating item A must not delete
        item B's rows."""
        import statuspage.config as config_mod
        monkeypatch.setattr(config_mod, "_load_runtime", lambda: {})
        monkeypatch.setattr(config_mod, "_save_runtime", lambda rt: None)
        from statuspage.db import record_history

        ids = self._make_db(tmp_path / "prune.db")
        a, b = ids[0], ids[1]
        conn = sqlite3.connect(str(tmp_path / "prune.db"))
        conn.row_factory = sqlite3.Row
        for i in range(3):
            record_history(conn, a, "status", "green", "red")
        record_history(conn, b, "status", "green", "degraded")
        ca = conn.execute(
            "SELECT COUNT(*) FROM status_history WHERE item_id = ?", [a]
        ).fetchone()[0]
        cb = conn.execute(
            "SELECT COUNT(*) FROM status_history WHERE item_id = ?", [b]
        ).fetchone()[0]
        conn.close()
        assert ca == 3, f"item A rows lost: {ca}"
        assert cb == 1, f"item B rows wiped by item A's prune: {cb}"

    def test_set_health_status_keeps_other_items(self, A, tmp_path, feed_server, monkeypatch):
        """healthcheck._set_health_status: same property via the worker path."""
        import healthcheck as hc

        ids = self._make_db(tmp_path / "prune2.db")
        base = tmp_path / "rss_prune"
        (base / "instance").mkdir(parents=True, exist_ok=True)
        cfg = base / "config.yaml"
        cfg.write_text(yaml.dump({"healthchecks": {}}))

        monkeypatch.setattr(hc, "_BASE_DIR", base)
        monkeypatch.setattr(hc, "_DB_PATH", tmp_path / "prune2.db")
        monkeypatch.setattr(hc, "_CONFIG_PATH", cfg)
        monkeypatch.setattr(hc, "_LOAD_CONFIG", lambda: yaml.safe_load(cfg.read_text()))

        hc._set_health_status("Item0", "red")
        hc._set_health_status("Item1", "degraded")
        conn = sqlite3.connect(str(tmp_path / "prune2.db"))
        c0 = conn.execute(
            "SELECT COUNT(*) FROM status_history WHERE item_id = ?", [ids[0]]
        ).fetchone()[0]
        c1 = conn.execute(
            "SELECT COUNT(*) FROM status_history WHERE item_id = ?", [ids[1]]
        ).fetchone()[0]
        conn.close()
        assert c0 == 1, f"Item0 rows lost: {c0}"
        assert c1 == 1, f"Item1 rows wiped by Item0's prune: {c1}"


# ── E2E: the real worker thread flips the item off the feed ────────

class TestRssFeedWorkerE2E:
    """A live background worker + live feed: status follows the feed text.

    The core behaviour — an RSS feed announcing an outage must flip the item
    red, a recovered feed must flip it back to green, and every transition
    is persisted to status_history (which the public /feed.xml surfaces).

    Runs in a FULLY ISOLATED environment:
      - temp base dir with its own instance/ → separate .healthcheck.lock,
        so the app's own long-lived worker (started by other test files) can
        never block us on the file lock
      - temp DB (schema created directly; no Flask/g context needed — the
        worker uses its own connection) and temp config
      - the healthcheck module is reconfigured to this env for the test and
        restored to the shared session env in the fixture teardown
    """

    @staticmethod
    def _status(env, name):
        c = sqlite3.connect(str(env.db_path))
        c.row_factory = sqlite3.Row
        try:
            row = c.execute(
                "SELECT status FROM status_items WHERE name = ?", [name]
            ).fetchone()
            return row["status"] if row else None
        finally:
            c.close()

    @pytest.fixture()
    def e2e_env(self, A, feed_server, tmp_path):
        import healthcheck as hc

        from types import SimpleNamespace

        base = tmp_path / "rss_e2e"
        inst = base / "instance"
        inst.mkdir(parents=True)
        db_path = inst / "test.db"
        config_path = base / "config.yaml"

        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """CREATE TABLE status_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'green',
                notes TEXT DEFAULT '',
                position INTEGER NOT NULL DEFAULT 0)"""
        )
        conn.execute(
            """CREATE TABLE status_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                event_type TEXT NOT NULL DEFAULT 'status',
                old_value TEXT DEFAULT '',
                new_value TEXT DEFAULT '',
                occurred TEXT NOT NULL)"""
        )
        conn.execute("INSERT INTO status_items (name) VALUES ('RssE2EItem')")
        conn.execute("INSERT INTO status_items (name) VALUES ('RssE2EHist')")
        conn.commit()
        conn.close()

        def _load():
            with open(str(config_path)) as f:
                return yaml.safe_load(f) or {}

        config_path.write_text(yaml.dump({
            "items": ["RssE2EItem", "RssE2EHist"],
            "healthchecks": {
                "RssE2EItem": {
                    "type": "rss", "url": feed_server.url + "/feed",
                    "keywords": WORDSET,
                    "interval": 1, "timeout": 5, "retries": 2,
                },
                "RssE2EHist": {
                    "type": "rss", "url": feed_server.url + "/feed",
                    "keywords": WORDSET,
                    "interval": 1, "timeout": 5, "retries": 2,
                },
            },
        }))

        # Reconfigure the worker module to the isolated env
        hc.configure_healthcheck(base, db_path, config_path, _load, 100)
        try:
            yield SimpleNamespace(base=base, db_path=db_path,
                                  config_path=config_path)
        finally:
            # Restore the worker module to the shared session environment
            hc.configure_healthcheck(A.get_base_dir(), A.get_db_path(),
                                     A.get_config_path(), A.load_config,
                                     A.MAX_HISTORY_PER_ITEM)

    def test_feed_changes_flip_status_and_back(self, feed_server, e2e_env):
        import healthcheck as hc

        name = "RssE2EItem"
        stop = threading.Event()
        start = threading.Thread(
            target=hc._healthcheck_worker,
            kwargs={"stop_event": stop},
            daemon=True, name="rss-e2e-worker"
        )
        start.start()
        try:
            # 1) Feed announces a major OUTAGE -> worker must flip the item red.
            _set_feed(feed_server.server, FEED_RED)
            deadline = time.time() + 15
            status = self._status(e2e_env, name)
            while status != "red" and time.time() < deadline:
                time.sleep(0.2)
                status = self._status(e2e_env, name)
            assert status == "red", "worker did not flip item red on outage feed"

            # 2) Vendor recovers: clean feed -> item back to green.
            _set_feed(feed_server.server, FEED_OK)
            deadline = time.time() + 30
            status = self._status(e2e_env, name)
            while status != "green" and time.time() < deadline:
                time.sleep(0.2)
                status = self._status(e2e_env, name)
            assert status == "green", "worker did not flip item back to green"
        finally:
            stop.set()
            start.join(timeout=10)

    def test_transitions_recorded_in_history(self, feed_server, e2e_env):
        """Both transitions land in status_history (feeds /feed.xml)."""
        import healthcheck as hc

        name = "RssE2EHist"
        feed_server.server.feed = FEED_OK  # start clean (green baseline)
        stop = threading.Event()
        start = threading.Thread(
            target=hc._healthcheck_worker,
            kwargs={"stop_event": stop},
            daemon=True, name="rss-e2e-worker-2"
        )
        start.start()
        try:
            deadline = time.time() + 15
            status = self._status(e2e_env, name)
            while status != "green" and time.time() < deadline:
                time.sleep(0.2)
                status = self._status(e2e_env, name)
            assert status == "green", "worker never reached the green baseline"

            # Now the vendor reports an OUTAGE -> red transition must be recorded
            _set_feed(feed_server.server, FEED_RED)
            deadline = time.time() + 15
            status = self._status(e2e_env, name)
            while status != "red" and time.time() < deadline:
                time.sleep(0.2)
                status = self._status(e2e_env, name)
            assert status == "red", "worker did not flip item red"

            conn = sqlite3.connect(str(e2e_env.db_path))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT old_value, new_value FROM status_history h "
                "JOIN status_items i ON i.id = h.item_id "
                "WHERE i.name = ? AND h.event_type = 'status' "
                "ORDER BY h.id",
                [name],
            ).fetchall()
            conn.close()
            pairs = [(r["old_value"], r["new_value"]) for r in rows]
            assert any(p[1] == "red" for p in pairs), f"no red transition recorded: {pairs}"
        finally:
            stop.set()
            start.join(timeout=10)
