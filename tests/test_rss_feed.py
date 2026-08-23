#!/usr/bin/env python3
"""Tests for the RSS status feed.

Covers:
  get_rss_config()          — defaults, config read, max_items clamping,
                              malformed values
  _save_rss / _load_rss     — config persistence round-trip
  build_feed_xml()          — RSS 2.0 structure, status-event filtering,
                              max_items, resolved-vs-current description
  feed_xml route (/feed.xml)— 200 + correct mimetype when enabled,
                              404 when disabled
  /rss alias                — same feed via the alternate path
  api_rss_status (GET)      — public metadata
  api_rss_toggle (POST)     — admin toggle persists; rejects non-bool;
                              unauthenticated 403
  end-to-end                — an admin status change produces a new feed item
"""
import sqlite3
import time
import xml.etree.ElementTree as ET

import pytest
import yaml
import statuspage.config as _cfg
import statuspage.auth as _auth
import app as app_obj


# ── Helpers ────────────────────────────────────────────────────────

def _rss_on_disk(A) -> dict:
    """Raw rss section from the temp config.yaml ({} when absent)."""
    from statuspage.config import load_config
    sec = load_config().get("rss")
    return sec if isinstance(sec, dict) else {}


def _write_rss(A, section: dict) -> None:
    """Persist an rss section (pass {} to model 'no custom config')."""
    from statuspage.config import _save_rss
    _save_rss(section)


def _mutate(client, method: str, url: str, payload: dict | None = None):
    """Mutation with a fresh CSRF token + clean rate-limit window."""
    import app as m
    _auth._mutation_rates.clear()
    tok = client.get("/api/csrf-token").get_json()["token"]
    headers = {"X-CSRF-Token": tok}
    if payload is None:
        return client.open(url, method=method, headers=headers)
    return client.open(url, method=method, json=payload, headers=headers)


def _root(feed_xml: str) -> ET.Element:
    return ET.fromstring(feed_xml)


@pytest.fixture()
def rss_roundtrip(A):
    """Save the rss section, let the test mutate it, restore afterwards."""
    before = _rss_on_disk(A)
    yield
    _write_rss(A, before)


# ── get_rss_config() ───────────────────────────────────────────────

class TestGetRssConfig:
    def test_defaults_when_absent(self, A, rss_roundtrip):
        _write_rss(A, {})
        from statuspage.rss import get_rss_config
        c = get_rss_config()
        assert c["enabled"] is True            # on by default
        assert c["max_items"] == 50
        assert c["title"] == "Application Status"
        assert c["base_url"].endswith(":8920")

    def test_reads_enabled_false(self, A, rss_roundtrip):
        _write_rss(A, {"enabled": False, "title": "My Status"})
        from statuspage.rss import get_rss_config
        c = get_rss_config()
        assert c["enabled"] is False
        assert c["title"] == "My Status"

    def test_clamps_max_items_upper(self, A, rss_roundtrip):
        _write_rss(A, {"max_items": 10000})
        from statuspage.rss import get_rss_config
        assert get_rss_config()["max_items"] == 500

    def test_clamps_max_items_lower(self, A, rss_roundtrip):
        _write_rss(A, {"max_items": 0})
        from statuspage.rss import get_rss_config
        assert get_rss_config()["max_items"] == 1

    def test_bad_max_items_falls_to_default(self, A, rss_roundtrip):
        _write_rss(A, {"max_items": "lots"})
        from statuspage.rss import get_rss_config
        assert get_rss_config()["max_items"] == 50

    def test_base_url_override_and_trailing_slash(self, A, rss_roundtrip):
        _write_rss(A, {"base_url": "https://status.example.com/"})
        from statuspage.rss import get_rss_config
        assert get_rss_config()["base_url"] == "https://status.example.com"


# ── config persistence ─────────────────────────────────────────────

class TestRssPersistence:
    def test_save_load_roundtrip(self, A, rss_roundtrip):
        _write_rss(A, {"enabled": False, "title": "Corp", "max_items": 10})
        from statuspage.config import _load_rss
        assert _load_rss() == {"enabled": False, "title": "Corp", "max_items": 10}

    def test_save_preserves_other_sections(self, A, rss_roundtrip):
        # Write healthchecks first, then rss must not clobber them.
        from statuspage.config import _save_healthchecks, _load_healthchecks
        _save_healthchecks({"Web": {"type": "curl", "url": "http://a/"}})
        _write_rss(A, {"enabled": True, "title": "T"})
        assert _load_healthchecks() == {"Web": {"type": "curl", "url": "http://a/"}}
        assert _rss_on_disk(A)["title"] == "T"


# ── build_feed_xml() ───────────────────────────────────────────────

class TestBuildFeedXml:
    """Feed generation from status_history."""

    def _hist(self, A, item, old, new, occurred):
        """Insert a status-history row directly (surgical, no admin/CSRF)."""
        c = sqlite3.connect(str(_cfg.get_db_path()))
        c.row_factory = sqlite3.Row
        iid = c.execute(
            "SELECT id FROM status_items WHERE name=?", (item,)).fetchone()["id"]
        c.execute(
            "INSERT INTO status_history "
            "(item_id, event_type, old_value, new_value, occurred) VALUES (?,?,?,?,?)",
            (iid, "status", old, new, occurred))
        c.commit()
        c.close()
        return occurred

    def _cleanup(self, A, *occurred):
        c = sqlite3.connect(str(_cfg.get_db_path()))
        c.executemany("DELETE FROM status_history WHERE occurred=?",
                      [(o,) for o in occurred])
        c.commit()
        c.close()

    def _feed(self, A, base=None):
        from statuspage.rss import build_feed_xml
        c = sqlite3.connect(str(_cfg.get_db_path()))
        c.row_factory = sqlite3.Row
        try:
            # base_url only matters for the <link>; pass a stable one in tests.
            return build_feed_xml(c, base_url=base or "http://localhost:8920")
        finally:
            c.close()

    def test_is_valid_rss2_with_channel(self, A, rss_roundtrip):
        xml = self._feed(A)
        root = _root(xml)
        assert root.tag == "rss"
        assert root.get("version") == "2.0"
        channel = root.find("channel")
        assert channel is not None
        assert channel.find("title") is not None
        assert channel.find("link") is not None
        assert channel.find("lastBuildDate") is not None

    def test_status_change_becomes_item(self, A, rss_roundtrip):
        occ = "2999-01-01T00:00:00.000000Z"
        self._hist(A, "SvcA", "green", "red", occ)
        try:
            root = _root(self._feed(A))
            items = root.find("channel").findall("item")
            titles = [i.find("title").text for i in items]
            assert any("SvcA: Operational → Outage" in t for t in titles)
        finally:
            self._cleanup(A, occ)

    def test_only_status_events_included(self, A, rss_roundtrip):
        """Notes / rename events are excluded; only status transitions appear."""
        occ_note = "2999-01-02T00:00:00.000000Z"
        occ_status = "2999-01-03T00:00:00.000000Z"
        c = sqlite3.connect(str(_cfg.get_db_path()))
        c.row_factory = sqlite3.Row
        iid = c.execute(
            "SELECT id FROM status_items WHERE name='SvcB'").fetchone()["id"]
        c.execute("INSERT INTO status_history (item_id, event_type, old_value, new_value, occurred) "
                  "VALUES (?,?,?,?,?)", (iid, "notes", "x", "y", occ_note))
        c.execute("INSERT INTO status_history (item_id, event_type, old_value, new_value, occurred) "
                  "VALUES (?,?,?,?,?)", (iid, "status", "green", "degraded", occ_status))
        c.commit()
        c.close()
        try:
            root = _root(self._feed(A))
            items = root.find("channel").findall("item")
            # Newest-first (status row is the later timestamp) and only status rows.
            assert items, "expected at least the status item"
            new = items[0]
            assert new.find("title").text == "SvcB: Operational → Degraded"
        finally:
            self._cleanup(A, occ_note, occ_status)

    def test_max_items_limits_feed(self, A, rss_roundtrip):
        _write_rss(A, {"max_items": 1})
        occs = []
        for i in range(3):
            occs.append(self._hist(A, "SvcA", "green", "red", f"2999-02-0{i}T00:00:00.000000Z"))
        try:
            root = _root(self._feed(A))
            items = root.find("channel").findall("item")
            assert len(items) == 1  # clamped to max_items=1
        finally:
            self._cleanup(A, *occs)

    def test_description_notes_resolution(self, A, rss_roundtrip):
        """If current status != event's new_value, description flags it resolved."""
        occ = "2999-03-01T00:00:00.000000Z"
        self._hist(A, "SvcA", "green", "red", occ)  # event says -> Outage
        # But the item row is still 'green' => the feed should note it's resolved.
        try:
            root = _root(self._feed(A))
            item = [i for i in root.find("channel").findall("item")
                    if (i.find("title").text or "").startswith("SvcA")][0]
            assert "since resolved" in item.find("description").text
        finally:
            self._cleanup(A, occ)


# ── feed_xml route ─────────────────────────────────────────────────

class TestFeedRoute:
    def _mk_history(self, A, item="SvcA", old="green", new="degraded"):
        occ = f"{time.time_ns()}Z"
        c = sqlite3.connect(str(_cfg.get_db_path()))
        c.row_factory = sqlite3.Row
        iid = c.execute(
            "SELECT id FROM status_items WHERE name=?", (item,)).fetchone()["id"]
        c.execute("INSERT INTO status_history (item_id, event_type, old_value, new_value, occurred) "
                  "VALUES (?,?,?,?,?)", (iid, "status", old, new, occ))
        c.commit()
        c.close()
        return occ

    def test_200_and_mimetype_when_enabled(self, client, A, rss_roundtrip):
        _write_rss(A, {"enabled": True})
        r = client.get("/feed.xml")
        assert r.status_code == 200
        assert r.content_type.startswith("application/rss+xml")
        # Parseable XML.
        _root(r.get_data(as_text=True))

    def test_404_when_disabled(self, client, A, rss_roundtrip):
        _write_rss(A, {"enabled": False})
        assert client.get("/feed.xml").status_code == 404

    def test_rss_alias_serves_same_feed(self, client, A, rss_roundtrip):
        _write_rss(A, {"enabled": True})
        a = client.get("/feed.xml").get_data(as_text=True)
        b = client.get("/rss").get_data(as_text=True)
        assert a.rstrip().split("<lastBuildDate>")[:1] == b.rstrip().split("<lastBuildDate>")[:1]
        # Both parse and share the channel title.
        assert _root(a).find("channel").find("title").text == \
            _root(b).find("channel").find("title").text

    def test_link_uses_request_host(self, client, A, rss_roundtrip):
        _write_rss(A, {"enabled": True})
        r = client.get("/feed.xml")  # test client Host = localhost
        root = _root(r.get_data(as_text=True))
        assert "localhost" in root.find("channel").find("link").text


# ── api_rss_status (GET) ───────────────────────────────────────────

class TestRssStatusApi:
    def test_returns_metadata(self, client, A, rss_roundtrip):
        _write_rss(A, {"enabled": True, "title": "Corp Feed", "max_items": 12})
        body = client.get("/api/rss").get_json()
        assert body["enabled"] is True
        assert body["title"] == "Corp Feed"
        assert body["max_items"] == 12
        assert body["url"].endswith("/feed.xml")

    def test_public_no_auth(self, client, A, rss_roundtrip):
        # Unauthenticated client can still read feed metadata.
        assert client.get("/api/rss").status_code == 200


# ── api_rss_toggle (POST) ──────────────────────────────────────────

class TestRssToggleApi:
    def test_toggle_off_persists(self, admin, A, rss_roundtrip):
        _write_rss(A, {"enabled": True, "title": "Keep"})
        r = _mutate(admin, "POST", "/api/rss", {"enabled": False})
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True and body["enabled"] is False
        # Persisted to disk, and other rss keys preserved.
        disk = _rss_on_disk(A)
        assert disk["enabled"] is False
        assert disk.get("title") == "Keep"

    def test_toggle_on(self, admin, A, rss_roundtrip):
        _write_rss(A, {"enabled": False})
        r = _mutate(admin, "POST", "/api/rss", {"enabled": True})
        assert r.status_code == 200 and r.get_json()["enabled"] is True
        assert _rss_on_disk(A)["enabled"] is True

    def test_rejects_missing_field(self, admin, A, rss_roundtrip):
        r = _mutate(admin, "POST", "/api/rss", {})
        assert r.status_code == 400

    def test_rejects_non_bool(self, admin, A, rss_roundtrip):
        r = _mutate(admin, "POST", "/api/rss", {"enabled": "yes"})
        assert r.status_code == 400
        # state unchanged by the rejected write
        assert _rss_on_disk(A) == {}

    def test_feed_404_after_toggling_off(self, admin, A, rss_roundtrip):
        _write_rss(A, {"enabled": True})
        assert app_obj.app.test_client().get("/feed.xml").status_code == 200
        _mutate(admin, "POST", "/api/rss", {"enabled": False})
        assert client_get_feed_404(A)

    def test_unauthenticated_403(self, client, A, rss_roundtrip):
        _auth._mutation_rates.clear()
        r = client.post("/api/rss", json={"enabled": False},
                        content_type="application/json")
        assert r.status_code == 403


def client_get_feed_404(A):
    r = app_obj.app.test_client().get("/feed.xml")
    assert r.status_code == 404
    return r.status_code == 404


# ── End-to-end: real admin status change -> new feed item ──────────

class TestE2EFeedUpdatesOnStatusChange:
    def test_admin_toggle_produces_feed_item(self, admin, A, rss_roundtrip):
        _write_rss(A, {"enabled": True})
        # Insert a temporary item directly (NOT via /api/add — add_item flushes
        # every DB row name into _runtime.items, which would leak into other
        # tests' seed/position assertions in the shared session DB).
        item_name = "RSS Live"
        c = sqlite3.connect(str(_cfg.get_db_path()))
        c.row_factory = sqlite3.Row
        c.execute(
            "INSERT OR IGNORE INTO status_items (name, status, position) "
            "VALUES (?, 'green', 999)", (item_name,))
        iid = c.execute(
            "SELECT id FROM status_items WHERE name=?", (item_name,)).fetchone()["id"]
        c.commit()
        c.close()
        try:
            # Real admin status change (green -> degraded) through the API.
            _mutate(admin, "POST", f"/api/toggle/{iid}", None)

            r = admin.get("/feed.xml")
            assert r.status_code == 200
            root = _root(r.get_data(as_text=True))
            titles = [i.find("title").text for i in root.find("channel").findall("item")]
            assert any("RSS Live: Operational → Degraded" in (t or "") for t in titles), titles
        finally:
            # Direct DB delete (no _runtime.items flush). Prunes history too.
            c2 = sqlite3.connect(str(_cfg.get_db_path()))
            c2.row_factory = sqlite3.Row
            c2.execute("DELETE FROM status_history WHERE item_id=?", (iid,))
            c2.execute("DELETE FROM status_items WHERE id=?", (iid,))
            c2.commit()
            c2.close()
