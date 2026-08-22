"""Slack integration for status-my-page.

Queues every status transition (manual admin toggles AND healthcheck-worker
flips) into a persistent SQLite outbox table so nothing is lost across
restarts. When the admin logs out, the whole queue is flushed as ONE digest
message to a Slack incoming-webhook URL.

Config lives in config.yaml under ``slack:``::

    slack:
      enabled: true
      webhook_url: https://hooks.slack.com/services/...
      channel: ""            # optional override; empty = webhook default
      max_queue: 200         # oldest rows dropped beyond this

``webhook_url`` may also come from the ``STATUS_SLACK_WEBHOOK_URL`` env var
(config.yaml wins if both are set). Delivery is strictly best-effort: a Slack
outage must never take down the status page or the logout route.
"""

import datetime as dt
import json
import os
import sqlite3
import urllib.error
import urllib.request

from statuspage.config import load_config, get_db_path

# ── Defaults (overridable by the ``slack:`` config section) ─────────
DEFAULT_SLACK_ENABLED = False
MAX_QUEUE = 500
_MAX_WEBHOOK_LEN = 512
_MAX_CHANNEL_LEN = 80

_STATUS_LABEL = {"green": ":large_green_circle: Operational",
                 "degraded": ":large_orange_circle: Degraded",
                 "red": ":red_circle: Outage"}


def get_slack_config() -> dict:
    """Return resolved Slack config with defaults applied for bad sections."""
    cfg = load_config()
    sec = cfg.get("slack")
    if not isinstance(sec, dict):
        sec = {}

    enabled = bool(sec.get("enabled", DEFAULT_SLACK_ENABLED))
    webhook = str(sec.get("webhook_url") or "").strip()[:_MAX_WEBHOOK_LEN]
    if not webhook:
        webhook = os.environ.get("STATUS_SLACK_WEBHOOK_URL", "").strip()[:_MAX_WEBHOOK_LEN]
    channel = str(sec.get("channel") or "").strip()[:_MAX_CHANNEL_LEN]

    try:
        max_queue = int(sec.get("max_queue", MAX_QUEUE))
    except (TypeError, ValueError):
        max_queue = MAX_QUEUE
    max_queue = max(1, min(max_queue, 5000))

    return {"enabled": enabled, "webhook_url": webhook,
            "channel": channel, "max_queue": max_queue}


def is_slack_enabled() -> bool:
    return get_slack_config()["enabled"] and bool(get_slack_config()["webhook_url"])


def _mask_webhook(url: str) -> str:
    """Redact everything after /services/ so tokens never leave the server."""
    if not url:
        return ""
    head, sep, _ = url.partition("/services/")
    return head + sep + "…" if sep else ("…" * min(len(url), 12))


def public_config() -> dict:
    """Safe view for GET /api/slack — never exposes the full webhook token."""
    conf = get_slack_config()
    configured = bool(conf["webhook_url"])
    return {
        "enabled": conf["enabled"],
        "configured": configured,
        "webhook_masked": _mask_webhook(conf["webhook_url"]),
        "queued": count_queued(),
        "channel": conf["channel"],
    }


# ── Outbox queue (persistent) ───────────────────────────────────────

def _outbox_db() -> sqlite3.Connection:
    # busy_timeout: callers may already hold the DB write lock (e.g. the
    # healthcheck worker enqueues inside its own transaction).
    conn = sqlite3.connect(str(get_db_path()), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS slack_outbox (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            item_name TEXT    NOT NULL,
            old_value TEXT    DEFAULT '',
            new_value TEXT    DEFAULT '',
            occurred  TEXT    NOT NULL
        )"""
    )
    return conn


def enqueue_status_change(item_name: str, old_status: str, new_status: str,
                          occurred: str | None = None) -> None:
    """Queue one transition. Never raises; silently no-ops when disabled."""
    try:
        conf = get_slack_config()
        if not conf["enabled"] or not conf["webhook_url"]:
            return
        ts = occurred or dt.datetime.now(dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ")
        conn = _outbox_db()
        try:
            conn.execute(
                "INSERT INTO slack_outbox (item_name, old_value, new_value, occurred)"
                " VALUES (?, ?, ?, ?)",
                (str(item_name)[:128], str(old_status)[:32],
                 str(new_status)[:32], ts),
            )
            conn.execute(
                "DELETE FROM slack_outbox WHERE id NOT IN ("
                "  SELECT id FROM slack_outbox ORDER BY id DESC LIMIT ?"
                ")",
                (conf["max_queue"],),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # notification must never break the mutation path
        print(f"Slack warning: could not queue status change ({exc})")


def count_queued() -> int:
    try:
        conn = _outbox_db()
        try:
            row = conn.execute("SELECT COUNT(*) AS n FROM slack_outbox").fetchone()
            return int(row["n"]) if row else 0
        finally:
            conn.close()
    except Exception:
        return 0


def clear_queue() -> int:
    """Drop the queue without sending (admin escape hatch). Returns removed."""
    conn = _outbox_db()
    try:
        cur = conn.execute("DELETE FROM slack_outbox")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ── Digest building & delivery ──────────────────────────────────────

def build_digest_message(rows: list[sqlite3.Row], base_url: str = "") -> dict:
    """Build the Slack Blocks payload for queued transitions (oldest first)."""
    lines = []
    for r in rows:  # chronological order (rows arrive oldest-first)
        label_old = _STATUS_LABEL.get(r["old_value"], r["old_value"] or "?")
        label_new = _STATUS_LABEL.get(r["new_value"], r["new_value"])
        lines.append(f"{label_old} → {label_new} — *{r['item_name']}*")
    text = "\n".join(lines)

    blocks = [{
        "type": "section",
        "text": {"type": "mrkdwn",
                 "text": f"*Status page update* — {len(rows)} change(s)\n{text}"},
    }]
    if base_url:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"<{base_url.rstrip('/')}/|Open status page>"}]})
    return {"text": f"Status page update — {len(rows)} change(s)", "blocks": blocks}


def send_to_slack(payload: dict, webhook: str, channel: str = "") -> tuple[bool, str]:
    """POST payload to the incoming webhook. Returns (ok, detail)."""
    body = dict(payload)
    if channel:
        body["channel"] = channel
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=data, headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            txt = resp.read().decode("utf-8", errors="replace")[:200]
            return resp.status == 200, txt or "ok"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}"
    except Exception as exc:
        return False, str(exc)


def flush(base_url: str = "") -> tuple[int, int, str]:
    """Send ONE digest of all queued changes. Returns (sent, remaining, detail).

    Rows are only deleted after a confirmed successful post, so a Slack outage
    leaves the queue intact for the next logout.
    """
    conf = get_slack_config()
    if not conf["enabled"]:
        n = count_queued()
        return 0, n, "slack disabled"
    if not conf["webhook_url"]:
        return 0, count_queued(), "no webhook configured"

    conn = _outbox_db()
    try:
        rows = conn.execute(
            "SELECT id, item_name, old_value, new_value, occurred "
            "FROM slack_outbox ORDER BY id ASC LIMIT ?", (conf["max_queue"],)
        ).fetchall()
        if not rows:
            return 0, 0, "nothing queued"

        ok, detail = send_to_slack(build_digest_message(rows, base_url),
                                   conf["webhook_url"], conf["channel"])
        if not ok:
            return 0, len(rows), f"delivery failed: {detail}"

        ids = [r["id"] for r in rows]
        conn.executemany("DELETE FROM slack_outbox WHERE id = ?",
                         [(i,) for i in ids])
        conn.commit()
        remaining = conn.execute("SELECT COUNT(*) AS n FROM slack_outbox").fetchone()
        return len(ids), int(remaining["n"]) if remaining else 0, detail
    finally:
        conn.close()
