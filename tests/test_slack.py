"""Tests for the Slack integration: queueing, digest building, flush, API."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from statuspage import slack as slack_mod  # noqa: E402


# The fake webhook server (_FakeSlack + fake_slack_url fixture) lives in
# conftest.py so test_mc_dc.py's flush tests can share the same instance.


def _fake():
    """The conftest fake webhook class (set when fake_slack_url runs)."""
    import conftest
    return conftest._FakeSlack


@pytest.fixture(autouse=True)
def _empty_outbox(A):
    """Start every Slack test with a clean outbox."""
    try:
        slack_mod.clear_queue()
    except Exception:
        pass
    yield
    try:
        slack_mod.clear_queue()
    except Exception:
        pass


def _cfg(url="", enabled=True, **extra):
    conf = {"enabled": enabled, "webhook_url": url,
            "channel": extra.pop("channel", ""), "max_queue": extra.pop("max_queue", 100)}
    conf.update(extra)
    return conf


# ── Config resolution ───────────────────────────────────────────────

class TestSlackConfig:
    def test_defaults_when_section_missing(self):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_mod, "load_config", lambda: {})
            conf = slack_mod.get_slack_config()
        assert conf["enabled"] is False
        assert conf["webhook_url"] == ""
        assert conf["channel"] == ""

    def test_env_fallback_for_webhook(self, monkeypatch):
        monkeypatch.setenv("STATUS_SLACK_WEBHOOK_URL",
                           "https://hooks.slack.com/services/env/fallback")
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_mod, "load_config",
                       lambda: {"slack": {"enabled": True}})
            conf = slack_mod.get_slack_config()
        assert conf["webhook_url"].startswith("https://hooks.slack.com/services/env")

    def test_config_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("STATUS_SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/env")
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_mod, "load_config",
                       lambda: {"slack": {"enabled": True,
                                          "webhook_url": "https://hooks.slack.com/services/cfg"}})
            conf = slack_mod.get_slack_config()
        assert "cfg" in conf["webhook_url"]

    def test_malformed_section_falls_back(self):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(slack_mod, "load_config", lambda: {"slack": "not-a-dict"})
            conf = slack_mod.get_slack_config()
        assert conf["enabled"] is False and conf["max_queue"] >= 1

    def test_public_config_masks_webhook(self, monkeypatch):
        monkeypatch.setattr(slack_mod, "get_slack_config", lambda: {
            "enabled": True, "webhook_url":
                "https://hooks.slack.com/services/T000/B000/XXXX",
            "channel": "#ops", "max_queue": 100})
        pub = slack_mod.public_config()
        assert "XXXX" not in pub["webhook_masked"]
        assert "T000" not in pub["webhook_masked"]
        assert "webhook_url" not in pub


# ── Queueing ────────────────────────────────────────────────────────

class TestQueueing:
    def test_enqueue_disabled_is_noop(self, monkeypatch):
        monkeypatch.setattr(slack_mod, "get_slack_config",
                            lambda: _cfg(enabled=False))
        slack_mod.enqueue_status_change("api", "green", "red")
        assert slack_mod.count_queued() == 0

    def test_enqueue_and_count(self, monkeypatch):
        monkeypatch.setattr(slack_mod, "get_slack_config",
                            lambda: _cfg("https://hooks.slack.com/services/x"))
        slack_mod.enqueue_status_change("slackq_api", "green", "red")
        slack_mod.enqueue_status_change("slackq_db", "red", "green")
        assert slack_mod.count_queued() == 2

    def test_enqueue_never_raises(self, monkeypatch):
        monkeypatch.setattr(slack_mod, "get_slack_config",
                            lambda: _cfg("https://hooks.slack.com/services/x"))

        def boom():
            raise RuntimeError("boom")

        monkeypatch.setattr(slack_mod, "_outbox_db", boom)
        slack_mod.enqueue_status_change("api", "green", "red")  # must not raise

    def test_queue_pruned_to_max(self, monkeypatch):
        monkeypatch.setattr(slack_mod, "get_slack_config",
                            lambda: _cfg("https://hooks.slack.com/services/x", max_queue=3))
        for i in range(6):
            slack_mod.enqueue_status_change(f"svc{i}", "green", "red")
        assert slack_mod.count_queued() == 3

    def test_clear_queue(self, monkeypatch):
        monkeypatch.setattr(slack_mod, "get_slack_config",
                            lambda: _cfg("https://hooks.slack.com/services/x"))
        slack_mod.enqueue_status_change("api", "green", "red")
        assert slack_mod.clear_queue() == 1
        assert slack_mod.count_queued() == 0


# ── Digest building ─────────────────────────────────────────────────

class TestDigest:
    def _row(self, name, old, new):
        return {"item_name": name, "old_value": old, "new_value": new,
                "occurred": "2026-08-21T00:00:00Z", "id": 1}

    def test_chronological_order_and_counts(self):
        rows = [self._row("svc_alpha", "green", "degraded"),
                self._row("svc_beta", "degraded", "red")]
        msg = slack_mod.build_digest_message(rows, base_url="http://x/")
        text = json.dumps(msg)
        assert "2 change(s)" in text
        assert text.index("svc_alpha") < text.index("svc_beta")

    def test_unknown_status_passthrough(self):
        msg = slack_mod.build_digest_message([self._row("a", "weird", "red")])
        assert "weird" in json.dumps(msg)


# ── Flush / delivery ────────────────────────────────────────────────

class TestFlush:
    def test_flush_nothing_queued(self, fake_slack_url, monkeypatch):
        monkeypatch.setattr(slack_mod, "get_slack_config",
                            lambda: _cfg(fake_slack_url))
        sent, remaining, detail = slack_mod.flush()
        assert sent == 0 and remaining == 0

    def test_flush_posts_one_digest_and_clears(self, fake_slack_url, monkeypatch):
        monkeypatch.setattr(slack_mod, "get_slack_config",
                            lambda: _cfg(fake_slack_url))
        _fake().payloads.clear()
        _fake().fail_with = None
        slack_mod.enqueue_status_change("flush_api", "green", "red")
        slack_mod.enqueue_status_change("flush_db", "red", "green")
        sent, remaining, _ = slack_mod.flush()
        assert sent == 2 and remaining == 0
        assert slack_mod.count_queued() == 0
        assert len(_fake().payloads) == 1  # ONE message, not two
        body = json.dumps(_fake().payloads[0])
        assert "flush_api" in body and "flush_db" in body

    def test_flush_failure_keeps_queue(self, fake_slack_url, monkeypatch):
        monkeypatch.setattr(slack_mod, "get_slack_config",
                            lambda: _cfg(fake_slack_url))
        _fake().payloads.clear()
        _fake().fail_with = (500, "server_error")
        slack_mod.enqueue_status_change("fail_api", "green", "red")
        sent, remaining, detail = slack_mod.flush()
        assert sent == 0 and remaining == 1
        assert "500" in detail
        assert slack_mod.count_queued() == 1  # intact for retry
        # Recovery
        _fake().fail_with = None
        sent, _, _ = slack_mod.flush()
        assert sent == 1

    def test_flush_disabled_reports(self, monkeypatch):
        monkeypatch.setattr(slack_mod, "get_slack_config",
                            lambda: _cfg(enabled=False))
        sent, remaining, detail = slack_mod.flush()
        assert sent == 0 and detail == "slack disabled"

    def test_channel_override_sent(self, fake_slack_url, monkeypatch):
        monkeypatch.setattr(slack_mod, "get_slack_config",
                            lambda: _cfg(fake_slack_url, channel="#ops"))
        _fake().payloads.clear()
        _fake().fail_with = None
        slack_mod.enqueue_status_change("chan_api", "green", "red")
        slack_mod.flush()
        assert _fake().payloads[0].get("channel") == "#ops"


# ── Admin API ───────────────────────────────────────────────────────

class TestSlackAPI:
    def test_requires_admin(self, client):
        assert client.get("/api/slack").status_code == 403
        assert client.post("/api/slack", json={"enabled": True}).status_code == 403

    def test_status_and_toggle(self, admin, token, monkeypatch):
        mutable = {"enabled": False, "webhook_url": "https://hooks.slack.com/services/T/B/C",
                   "channel": "", "max_queue": 100}
        # get_slack_config must read live state so the toggle is reflected
        monkeypatch.setattr(slack_mod, "get_slack_config", lambda: mutable)
        r = admin.get("/api/slack")
        assert r.status_code == 200
        data = r.get_json()
        assert data["configured"] is True
        assert data["enabled"] is False

        r = admin.post("/api/slack", json={"enabled": True},
                       headers={"X-CSRF-Token": token})
        assert r.status_code == 200
        assert r.get_json()["enabled"] is True

    def test_webhook_validation(self, admin, token):
        r = admin.post("/api/slack", json={"webhook_url": "http://insecure"},
                       headers={"X-CSRF-Token": token})
        assert r.status_code == 400

    def test_webhook_https_accepted(self, admin, token, monkeypatch):
        monkeypatch.setattr(slack_mod, "get_slack_config",
                            lambda: _cfg("", enabled=False))
        r = admin.post("/api/slack",
                       json={"webhook_url": "https://hooks.slack.com/services/A/B/C"},
                       headers={"X-CSRF-Token": token})
        assert r.status_code == 200

    def test_channel_validation(self, admin, token):
        r = admin.post("/api/slack", json={"channel": "bad channel!"},
                       headers={"X-CSRF-Token": token})
        assert r.status_code == 400

    def test_clear_queue_endpoint(self, admin, token, monkeypatch):
        monkeypatch.setattr(slack_mod, "get_slack_config",
                            lambda: _cfg("https://hooks.slack.com/services/x"))
        monkeypatch.setattr("statuspage.config.load_config",
                            lambda: {"slack": {}})
        slack_mod.enqueue_status_change("cq_api", "green", "red")
        r = admin.post("/api/slack", json={"clear_queue": True},
                       headers={"X-CSRF-Token": token})
        assert r.status_code == 200
        assert r.get_json().get("cleared") >= 1
        assert slack_mod.count_queued() == 0

    def test_logout_flushes(self, A, fake_slack_url, monkeypatch):
        monkeypatch.setattr(slack_mod, "get_slack_config",
                            lambda: _cfg(fake_slack_url))
        _fake().payloads.clear()
        _fake().fail_with = None
        slack_mod.enqueue_status_change("logout_api", "green", "red")

        c = A.app.test_client()
        r = c.post("/login", json={"user": "admin", "pass": "testpass"})
        assert r.status_code == 200
        r = c.post("/logout")
        assert r.status_code == 200
        assert len(_fake().payloads) == 1
        assert slack_mod.count_queued() == 0
