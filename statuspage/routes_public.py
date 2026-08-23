#!/usr/bin/env python3
"""Public (unauthenticated) HTTP routes for status-my-page.

These routes are accessible to any visitor without login.
"""

from __future__ import annotations

__all__ = [
    "status_page",
    "feed_xml",
    "api_rss_status",
    "api_status_public",
    "api_settings_status",
    "api_history",
]\n\n
from flask import jsonify, render_template, request, abort

from statuspage.config import get_logo_url, history_enabled, healthchecks_enabled
from statuspage import rss as rss_mod


# ── Public routes ───────────────────────────────────────────────────────

def status_page() -> str:
    """Main status page. Public read."""
    from statuspage.services import get_all_status_items
    from statuspage.auth import get_csrf

    items = get_all_status_items()
    is_admin = request.session.get("admin", False) if hasattr(request, "session") else False
    csrf = get_csrf() if is_admin else ""

    from statuspage.config import get_logo_url
    return render_template(
        "index.html",
        items=items,
        session_admin=is_admin,
        csrf_token=csrf,
        history_enabled=history_enabled(),
        healthchecks_enabled=healthchecks_enabled(),
        logo_url=get_logo_url(),
    )


def feed_xml() -> tuple:
    """RSS 2.0 status-change feed. Public read.

    Generated on demand from status_history so it always reflects the live
    DB and the newest <item> / lastBuildDate advance when a status changes.
    """
    from flask import Response, abort

    if not rss_mod.is_rss_enabled():
        abort(404)

    from statuspage.db import get_connection
    with get_connection() as db:
        xml = rss_mod.build_feed_xml(db, request.host_url)

    resp = Response(xml, mimetype="application/rss+xml; charset=utf-8")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp, 200


def api_rss_status() -> dict:
    """Public: feed availability + metadata for the UI.

    Returns feed URL / enabled so non-admin visitors can see the link and so
    the admin panel can render the current state without a second round trip.
    """
    conf = rss_mod.get_rss_config()
    return jsonify({
        "enabled": conf["enabled"],
        "title": conf["title"],
        "max_items": conf["max_items"],
        "url": request.host_url.rstrip("/") + "/feed.xml",
    })


def api_history(item_id: int) -> dict:
    """Return history for a service. Public read — anyone can see status timeline.

    Respect the admin-configurable history_enabled setting; when disabled
    the endpoint 404s so the timeline is unreachable, not just hidden.
    """
    from statuspage.config import history_enabled

    if not history_enabled():
        abort(404)
    from statuspage.services import get_item_history

    result = get_item_history(item_id)
    if result is None:
        abort(404)
    return jsonify(result)


def api_status_public() -> dict:
    """Lightweight public status list for auto-refresh polling.

    Returns id/name/status/notes only — no history, no admin detail.
    Notes are included so open note panes stay in sync for visitors.
    """
    from statuspage.services import get_all_status_items

    items = get_all_status_items()
    return jsonify([
        {
            "id": it["id"],
            "name": it["name"],
            "status": it["status"],
            "notes": it["notes"] if "notes" in it.keys() else "",
        }
        for it in items
    ])


def api_settings_status() -> dict:
    """Public: current UI settings so the template/JS render the right state.

    ``history_enabled`` controls the per-service history timeline (public
    read visibility + API reachability).
    ``healthchecks_enabled`` controls background healthcheck execution.
    """
    from statuspage.config import history_enabled, healthchecks_enabled

    return jsonify({
        "history_enabled": history_enabled(),
        "healthchecks_enabled": healthchecks_enabled(),
    })


# Note: api_rss_toggle is admin-only (requires @require_admin)
# It is NOT included here — it lives in routes_admin.py