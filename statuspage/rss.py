"""RSS 2.0 status feed for status-my-page.

The feed is generated ON DEMAND from ``status_history`` (every status
transition recorded by either the admin toggle or the background healthcheck
worker — both write ``event_type='status'`` rows) joined with the current
``status_items`` status. There is no cache and no hook into the mutation
paths: the feed always reflects the live DB, and both ``<lastBuildDate>`` and
the newest ``<item>`` advance the instant a status changes.

Only status-change events are surfaced (note edits and renames are filtered
out) so readers get a clean Operational / Degraded / Outage timeline.
"""

import datetime as dt
import xml.etree.ElementTree as ET

from statuspage.config import load_config, get_server_port

# ── Defaults (overridable by the ``rss:`` config section) ──────────
DEFAULT_RSS_ENABLED = True
DEFAULT_RSS_MAX_ITEMS = 50
MAX_RSS_ITEMS = 500
DEFAULT_TITLE = "Application Status"
_MAX_TITLE = 64

_STATUS_LABEL = {"green": "Operational", "degraded": "Degraded", "red": "Outage"}


def _now_rfc822() -> str:
    """UTC now in RFC-822 form, as RSS ``lastBuildDate``/``pubDate`` expect."""
    return dt.datetime.now(dt.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")


def _parse_occurred(raw: str) -> dt.datetime:
    """Parse the app's ISO-8601 ``...Z`` timestamp to an aware UTC datetime."""
    s = (raw or "").strip()
    if not s:
        return dt.datetime.now(dt.timezone.utc)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return dt.datetime.now(dt.timezone.utc)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


def get_rss_config() -> dict:
    """Return the resolved RSS config.

    Always has keys ``enabled`` (bool), ``max_items`` (int), ``title`` (str)
    and ``base_url`` (str). Falls back to defaults when the ``rss:`` section
    is absent or malformed, so a broken section can't take the feed down.
    """
    cfg = load_config()
    rss = cfg.get("rss")
    if not isinstance(rss, dict):
        rss = {}

    enabled = bool(rss.get("enabled", DEFAULT_RSS_ENABLED))

    try:
        max_items = int(rss.get("max_items", DEFAULT_RSS_MAX_ITEMS))
    except (TypeError, ValueError):
        max_items = DEFAULT_RSS_MAX_ITEMS
    max_items = max(1, min(max_items, MAX_RSS_ITEMS))

    title = (rss.get("title") or "").strip() or DEFAULT_TITLE
    title = title[:_MAX_TITLE]

    base_url = (rss.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        base_url = f"http://localhost:{get_server_port()}"

    return {"enabled": enabled, "max_items": max_items, "title": title, "base_url": base_url}


def is_rss_enabled() -> bool:
    return get_rss_config()["enabled"]


def _status_label(s: str) -> str:
    return _STATUS_LABEL.get(s, s)


def _esc(s: str) -> str:
    """ElementText escape for a title (XML entities handled by ElementTree)."""
    return str(s)


def build_feed_xml(db, base_url: str | None = None) -> str:
    """Build the RSS 2.0 document as a string.

    ``db`` is a sqlite3.Connection with sqlite3.Row factory. ``base_url`` is
    the origin for ``<link>`` (pass ``request.host_url`` so the feed's own
    link points wherever the site is actually served).
    """
    conf = get_rss_config()
    origin = (base_url or conf["base_url"]).rstrip("/") or conf["base_url"]
    feed_url = origin + "/feed.xml"

    # Join status-change history to item names, newest first.
    rows = db.execute(
        """SELECT i.name AS name, h.old_value AS old_value,
                  h.new_value AS new_value, h.occurred AS occurred,
                  s.status AS cur_status
           FROM status_history h
           JOIN status_items i ON i.id = h.item_id
           LEFT JOIN status_items s ON s.id = h.item_id
           WHERE h.event_type = 'status'
           ORDER BY h.occurred DESC"""
    ).fetchall()

    items = rows[: conf["max_items"]]

    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")

    title = conf["title"]
    ET.SubElement(channel, "title").text = _esc(title)
    ET.SubElement(channel, "description").text = _esc(
        f"Status change feed for {title}"
    )
    ET.SubElement(channel, "link").text = feed_url
    ET.SubElement(channel, "ttl").text = "1"
    ET.SubElement(channel, "language").text = "en-us"
    ET.SubElement(channel, "lastBuildDate").text = _now_rfc822()

    for r in items:
        name = r["name"] or "?"
        old = _status_label(r["old_value"] or "")
        new = _status_label(r["new_value"] or "")
        cur = _status_label(r["cur_status"] or "")

        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = _esc(f"{name}: {old} \u2192 {new}")
        if cur == new:
            desc = f"{name} changed from {old} to {new}."
        else:
            desc = f"{name} changed from {old} to {new} (current status: {cur} \u2014 since resolved)."
        ET.SubElement(item, "description").text = _esc(desc)
        ET.SubElement(item, "link").text = feed_url
        occurred = _parse_occurred(r["occurred"])
        ET.SubElement(item, "pubDate").text = occurred.strftime(
            "%a, %d %b %Y %H:%M:%S +0000"
        )
        ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = _esc(
            f"{name}|{occurred.isoformat()}"
        )

    tree = ET.ElementTree(rss)
    ET.indent(tree)
    xml_bytes = ET.tostring(rss, encoding="utf-8", xml_declaration=True)
    return xml_bytes.decode("utf-8")
