#!/usr/bin/env python3
"""HTTP routes for status-my-page."""
import sqlite3
from flask import jsonify, render_template, session, request, abort

import healthcheck as hc_module  # shared validation helpers
from statuspage.auth import (
    login_route,
    logout_route,
    auth_check_route,
    csrf_token_route,
    require_admin,
    get_csrf,
)
from statuspage.db import get_connection
from statuspage.healthcheck import (
    get_configured_healthchecks,
    run_healthchecks_once,
    is_configured as get_configured_healthchecks_module_state,
)
from statuspage.services import (
    get_all_status_items,
    toggle_item,
    rename_item,
    reorder_items,
    update_notes,
    add_item,
    delete_item,
    get_item_history,
    clear_item_history,
)
from statuspage.config import (
    HEALTHCHECKS_CFG_LOCK,
    _load_healthchecks, _save_healthchecks, _load_rss, _save_rss,
    _load_settings, _save_settings, history_enabled, healthchecks_enabled,
)
from statuspage import rss as rss_mod
from statuspage import slack as slack_mod
from statuspage.config import _save_slack
import re
from input_filter import InputRejected, validate_json_data, validate_name, validate_notes, validate_int_param


# ── Public routes ───────────────────────────────────────────────────

def status_page():
    items = get_all_status_items()
    is_admin = session.get("admin", False)
    csrf = get_csrf() if is_admin else ""
    from statuspage.config import get_logo_url
    return render_template(
        "index.html", items=items, session_admin=is_admin, csrf_token=csrf,
        history_enabled=history_enabled(), healthchecks_enabled=healthchecks_enabled(), logo_url=get_logo_url()
    )


def feed_xml():
    """RSS 2.0 status-change feed. Public read.

    Generated on demand from status_history so it always reflects the live
    DB and the newest <item> / lastBuildDate advance when a status changes.
    """
    from flask import Response, abort

    if not rss_mod.is_rss_enabled():
        abort(404)

    with get_connection() as db:
        xml = rss_mod.build_feed_xml(db, base_url=request.host_url)

    resp = Response(xml, mimetype="application/rss+xml; charset=utf-8")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


def api_rss_status():
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


def api_history(item_id: int):
    """Return history for a service. Public read — anyone can see status timeline.

    Respect the admin-configurable history_enabled setting; when disabled
    the endpoint 404s so the timeline is unreachable, not just hidden.
    """
    if not history_enabled():
        abort(404)
    result = get_item_history(item_id)
    if result is None:
        abort(404)
    return jsonify(result)


@require_admin()
def api_history_clear(item_id: int):
    """Clear all history entries for a service. Admin + CSRF only.

    The history feature setting does NOT gate this (an admin may want to wipe
    the timeline while the public view is disabled); the button simply only
    renders when it's enabled. Returns how many rows were removed.
    """
    removed = clear_item_history(item_id)
    if removed is None:
        abort(404)
    return jsonify(ok=True, removed=removed)


def _redact_healthcheck(d: dict) -> dict:
    """Strip internal targets from a healthcheck for public display.

    A public visitor sees the check name, type, and tuning numbers —
    never the url/host/port/soap_action (internal network topology), nor
    the keyword lists that would expose how this page interprets vendor
    feeds. Admins get the full object via the gated view.
    """
    safe = {
        "type": d.get("type", ""),
        "interval": d.get("interval"),
        "timeout": d.get("timeout"),
        "retries": d.get("retries"),
    }
    if d.get("service"):
        safe["service"] = d["service"]
    # healthy_codes are public: they are HTTP status codes, not topology
    if d.get("healthy_codes"):
        safe["healthy_codes"] = sorted(d["healthy_codes"])
    return safe


def api_healthchecks():
    """List configured healthchecks.

    Admins get the full config (the admin panel needs url/host/port to render
    and edit). Everyone else gets a redacted summary — the endpoint is
    reachable without auth, and probe targets are internal-network detail.

    Returns an empty payload (not 500) when healthchecking is disabled via
    STATUS_DISABLE_HEALTHCHECKS — the admin panel renders its empty state.
    """
    if not get_configured_healthchecks_module_state():
        return jsonify({})
    hc = get_configured_healthchecks()
    if session.get("admin"):
        return jsonify(hc)
    return jsonify({name: _redact_healthcheck(d) for name, d in hc.items()})


def generate_static_html() -> str:
    """Generate standalone static HTML containing the current status page state.

    Inlines CSS and embeds the complete static markup without login or admin elements,
    suitable for hosting on mass-delivery static web servers, S3/CloudFront, GitHub Pages, etc.
    """
    import datetime as dt
    import base64
    import html as _html
    from pathlib import Path
    from flask import current_app

    items = get_all_status_items()

    # Determine overall status badge
    has_red = any(it["status"] == "red" for it in items)
    has_degraded = any(it["status"] == "degraded" for it in items)

    if has_red:
        badge_class = "red"
        badge_text = "System Outage"
    elif has_degraded:
        badge_class = "degraded"
        badge_text = "Degraded Performance"
    else:
        badge_class = "green"
        badge_text = "All Systems Operational"

    static_dir = Path(current_app.root_path) / "static"
    css_path = static_dir / "css" / "style.css"
    css_content = ""
    if css_path.exists():
        css_content = css_path.read_text(encoding="utf-8")

    # Inline the logo as a base64 data URI so the static HTML is standalone,
    # honoring the same resolution + traversal guard as the live page
    # (``logo.path`` from config.yaml, via get_logo_local_path()). No configured
    # logo, or file missing/empty/traversal -> an empty <div class="logo-wrap">.
    from statuspage.config import get_logo_local_path
    logo_path = get_logo_local_path()
    if logo_path is not None:
        b64 = base64.b64encode(logo_path.read_bytes()).decode("ascii")
        logo_html = f"""
            <div class="logo-wrap">
                <img src="data:image/png;base64,{b64}" alt="Logo" class="logo-img">
            </div>"""
    else:
        logo_html = """
            <div class="logo-wrap">
            </div>"""

    generated_time = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    status_rows_html = []
    for item in items:
        status = item["status"]
        status_label = "Operational" if status == "green" else ("Degraded" if status == "degraded" else "Outage")
        # Escape admin-controlled text — this HTML bypasses Jinja autoescaping
        notes = _html.escape((item["notes"] or "").strip()) if "notes" in item.keys() else ""
        show_notes_class = " show-notes" if status != "green" and notes else ""

        notes_html = f'<div class="static-notes">{notes}</div>' if notes else ''

        row_html = f"""
        <div class="status-row{show_notes_class}" data-id="{item['id']}">
            <div class="status-main">
                <span class="status-dot {status}"></span>
                <span class="status-name">{_html.escape(item['name'])}</span>
                <span class="status-label {status}">{status_label}</span>
            </div>
            {notes_html}
        </div>"""
        status_rows_html.append(row_html)

    status_list_html = "\n".join(status_rows_html)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Application Status</title>
    <script>
        /* Apply the saved theme before first paint (same behaviour as the live page). */
        (function () {{
            try {{
                if (localStorage.getItem('theme') === 'light') {{
                    document.documentElement.setAttribute('data-theme', 'light');
                }}
            }} catch (e) {{ /* localStorage unavailable — stay dark */ }}
        }})();
    </script>
    <style>
{css_content}
        /* Standalone static additions */
        .static-notes {{
            flex: 1;
            padding: 0.8rem 1rem;
            color: var(--text-muted);
            font-size: 0.875rem;
            line-height: 1.4;
            display: flex;
            align-items: center;
        }}
        .status-row.show-notes .static-notes {{
            display: flex;
        }}
        .status-row:not(.show-notes) .static-notes {{
            display: none;
        }}
        .top-bar-placeholder {{
            width: 95px;
            flex-shrink: 0;
        }}
        .static-footer {{
            margin-top: 2rem;
            text-align: center;
            font-size: 0.75rem;
            color: var(--text-muted);
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="top-bar">
            <button id="themeToggle" class="theme-btn" type="button" aria-label="Switch to light mode">☀️ Light mode</button>{logo_html}
            <div class="top-bar-placeholder" aria-hidden="true"></div>
        </div>
        <header>
            <h1>Application Status</h1>
            <div class="overall-badge {badge_class}">{badge_text}</div>
        </header>

        <div class="status-list">
{status_list_html}
        </div>

        <div class="static-footer">
            Generated: {generated_time}
        </div>
    </div>
    <script>
        /* Theme toggle — mirrors the live page's behaviour. */
        (function () {{
            var btn = document.getElementById('themeToggle');
            if (!btn) return;
            function current() {{
                return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
            }}
            function sync() {{
                var isLight = current() === 'light';
                btn.textContent = isLight ? '🌙 Dark mode' : '☀️ Light mode';
                btn.setAttribute('aria-label', isLight ? 'Switch to dark mode' : 'Switch to light mode');
            }}
            btn.addEventListener('click', function () {{
                var next = current() === 'light' ? 'dark' : 'light';
                if (next === 'light') document.documentElement.setAttribute('data-theme', 'light');
                else document.documentElement.removeAttribute('data-theme');
                try {{
                    if (next === 'light') localStorage.setItem('theme', 'light');
                    else localStorage.removeItem('theme');
                }} catch (e) {{}}
                sync();
            }});
            sync();
        }})();
    </script>
</body>
</html>"""
    return html


@require_admin(require_csrf=False, require_rate_limit=False)
def api_export_static():
    """Admin endpoint to download or export the current page as standalone static HTML."""
    from flask import Response
    html_content = generate_static_html()
    as_attachment = request.args.get("download", "true").lower() == "true"
    resp = Response(html_content, mimetype="text/html; charset=utf-8")
    if as_attachment:
        resp.headers["Content-Disposition"] = 'attachment; filename="status.html"'
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp

# These are just re-exports
login = login_route
logout = logout_route
auth_check = auth_check_route
api_csrf = csrf_token_route


# ── Admin routes ────────────────────────────────────────────────────

@require_admin()
def api_toggle(item_id: int):
    result = toggle_item(item_id)
    if result is None:
        return jsonify(error="Service not found"), 404
    return jsonify(result)


@require_admin()
def api_rename(item_id: int):
    data = validate_json_data(request.get_json(silent=True))
    name = validate_name(data.get("name", ""), "name")
    ok, msg = rename_item(item_id, name)
    if not ok:
        status_code = 404 if msg == "Not found" else 409
        return jsonify(error=msg), status_code
    return jsonify(ok=True)


@require_admin()
def api_notes(item_id: int):
    data = validate_json_data(request.get_json(silent=True))
    notes = validate_notes(data.get("notes", ""), "notes")
    if not update_notes(item_id, notes):
        return jsonify(error="Service not found"), 404
    return jsonify(ok=True)


@require_admin()
def api_add():
    data = validate_json_data(request.get_json(silent=True))
    name = validate_name(data.get("name", ""), "name")
    try:
        item = add_item(name)
        return jsonify(item=item)
    except sqlite3.IntegrityError:
        return jsonify(error="Item already exists"), 409


@require_admin()
def api_delete(item_id: int):
    name = delete_item(item_id)
    if name is None:
        return jsonify(error="Not found"), 404
    return jsonify(ok=True, name=name)


@require_admin()
def api_healthcheck_run():
    """Trigger a one-shot healthcheck run for all services. Admin only.

    Returns 409 with a clear message when the healthcheck module is disabled
    (STATUS_DISABLE_HEALTHCHECKS=1) instead of an unhandled 500.
    """
    if not get_configured_healthchecks_module_state():
        return jsonify(error="Healthchecks are disabled on this deployment "
                             "(STATUS_DISABLE_HEALTHCHECKS is set)."), 409
    results = run_healthchecks_once()
    return jsonify(results)


@require_admin()
def api_reorder():
    data = validate_json_data(request.get_json(silent=True))
    raw_order = data.get("order", {})
    if not isinstance(raw_order, dict):
        raise InputRejected("order must be an object", "order")
    order_map = {validate_int_param(k, "key"): validate_int_param(v, "value") for k, v in raw_order.items()}
    reorder_items(order_map)
    return jsonify(ok=True)


# ── Healthcheck Admin Routes ────────────────────────────────────────

VALID_HC_TYPES = ["curl", "ping", "tcp", "soap", "rss"]
# Numeric bounds: (field, min, max) for admin-supplied tuning values
HC_NUMERIC_BOUNDS = [
    ("interval", 1, 3600),
    ("timeout", 1, 300),
    ("retries", 1, 10),
]


def _validate_rss_keywords(raw) -> tuple[dict[str, list[str]] | None, str | None]:
    """Sanitize rss keywords input.

    Returns (keywords, error). keywords is None when the input was absent;
    the result always has "red"/"degraded" keys with lower-case word lists
    (mirrors _parse_healthchecks). Non-dict or malformed entries are
    rejected with an error for the create endpoint; individual bad words are
    dropped.
    """
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        return None, "keywords must be an object with red and degraded arrays"
    out: dict[str, list[str]] = {"red": [], "degraded": []}
    for level in ("red", "degraded"):
        val = raw.get(level, [])
        if isinstance(val, str):
            val = [val]
        if not isinstance(val, list):
            return None, f"keywords.{level} must be an array of strings"
        for w in val:
            if w is None:
                continue
            w = str(w).strip()
            if len(w) > 64 or not w:
                continue
            out[level].append(w.lower())
    return out, None


def _clean_healthy_codes(raw) -> tuple[list[int] | None, str | None]:
    """Sanitize healthy_codes input.

    Returns (codes, error). codes is None when the input was absent; an
    empty list means "clear existing". Out-of-range or non-numeric entries
    are dropped (mirrors _parse_healthchecks, which falls back to {200}
    when the set ends up empty).
    """
    if raw is None:
        return None, None
    if not isinstance(raw, list):
        return None, "healthy_codes must be an array"
    codes = []
    for c in raw:
        try:
            code = int(c)
        except (TypeError, ValueError):
            continue
        if 100 <= code <= 599:
            codes.append(code)
    return codes, None


def _validate_host(raw) -> tuple[str | None, str | None]:
    """Validate a ping/tcp host. Returns (host, error)."""
    if not isinstance(raw, str) or not raw.strip():
        return None, "host is required"
    host = raw.strip()
    if not hc_module._safe_host(host):
        return None, "invalid host"
    return host, None


def _validate_url(raw) -> tuple[str | None, str | None]:
    """Validate a curl/soap URL. Returns (url, error)."""
    if not isinstance(raw, str) or not raw.strip():
        return None, "url is required for curl/soap type"
    url = raw.strip()
    if not hc_module._safe_url(url):
        return None, "url must be http:// or https:// with a valid hostname"
    return url, None


def _validate_numeric_fields(data: dict) -> tuple[dict[str, int], str | None]:
    """Extract and bound-check interval/timeout/retries. Returns (fields, error)."""
    fields: dict[str, int] = {}
    for field, min_val, max_val in HC_NUMERIC_BOUNDS:
        val = data.get(field)
        if val is None:
            continue
        if isinstance(val, bool):
            return fields, f"{field} must be an integer"
        try:
            val = int(val)
        except (TypeError, ValueError):
            return fields, f"{field} must be an integer"
        if not (min_val <= val <= max_val):
            return fields, f"{field} must be between {min_val} and {max_val}"
        fields[field] = val
    return fields, None


@require_admin()
def api_healthchecks_create():
    """Create a new healthcheck configuration."""
    data = validate_json_data(request.get_json(silent=True))

    # Validate required fields
    name = validate_name(data.get("name", ""), "name")
    raw_type = data.get("type") or ""
    if not isinstance(raw_type, str):
        raw_type = ""
    check_type = raw_type.strip().lower()

    # Validate type
    if check_type and check_type not in VALID_HC_TYPES:
        return jsonify(error=f"invalid type, must be one of: {', '.join(VALID_HC_TYPES)}"), 400

    # Auto-detect type from payload when omitted (mirrors _parse_healthchecks).
    # A healthcheck with no probe target at all is dead config — reject it.
    if not check_type:
        has_url = isinstance(data.get("url"), str) and data["url"].strip()
        has_host = isinstance(data.get("host"), str) and data["host"].strip()
        has_soap = bool(
            (isinstance(data.get("soap_action"), str) and data["soap_action"].strip())
            or (isinstance(data.get("body"), str) and data["body"].strip())
        )
        if has_soap:
            check_type = "soap"
        elif has_host and data.get("port") is not None:
            check_type = "tcp"
        elif has_host:
            check_type = "ping"
        elif has_url:
            check_type = "curl"
        else:
            return jsonify(error="missing probe target (url or host, or soap_action/body)"), 400

    # Build healthcheck config based on type
    hc_config: dict = {}
    if check_type:
        hc_config["type"] = check_type

    if check_type in ("curl", "soap"):
        url, err = _validate_url(data.get("url", ""))
        if err:
            return jsonify(error=err), 400
        hc_config["url"] = url

        if check_type in ("curl", "soap"):
            if check_type == "soap":
                soap_action = data.get("soap_action", "").strip()
                if soap_action:
                    hc_config["soap_action"] = soap_action
                body = data.get("body", "").strip()
                if body:
                    hc_config["body"] = body
                expected_string = data.get("expected_string", "").strip()
                if expected_string:
                    hc_config["expected_string"] = expected_string

            failure_keyword = data.get("failure_keyword", "").strip()
            if failure_keyword:
                hc_config["failure_keyword"] = failure_keyword
            degraded_keyword = data.get("degraded_keyword", "").strip()
            if degraded_keyword:
                hc_config["degraded_keyword"] = degraded_keyword

        codes, err = _clean_healthy_codes(data.get("healthy_codes"))
        if err:
            return jsonify(error=err), 400
        if codes:
            hc_config["healthy_codes"] = codes

    elif check_type == "ping":
        host, err = _validate_host(data.get("host", ""))
        if err:
            return jsonify(error=err), 400
        hc_config["host"] = host

    elif check_type == "tcp":
        host, err = _validate_host(data.get("host", ""))
        if err:
            return jsonify(error=err), 400
        hc_config["host"] = host

        port = data.get("port")
        if port is None or isinstance(port, bool):
            return jsonify(error="port is required for tcp type"), 400
        try:
            port = int(port)
        except (TypeError, ValueError):
            return jsonify(error="port must be an integer"), 400
        if not (1 <= port <= 65535):
            return jsonify(error="port must be between 1 and 65535"), 400
        hc_config["port"] = port

    elif check_type == "rss":
        url, err = _validate_url(data.get("url", ""))
        if err:
            return jsonify(error=err), 400
        hc_config["url"] = url
        # Default keywords: outage words -> red, performance words -> degraded
        hc_config["keywords"] = {
            "red": ["outage", "down", "major issue", "critical"],
            "degraded": ["degraded", "partial", "minor", "investigating"],
        }
        keywords, err = _validate_rss_keywords(data.get("keywords"))
        if err:
            return jsonify(error=err), 400
        if keywords is not None:
            hc_config["keywords"] = keywords

    # Optional target service linking (defaults to check name if omitted)
    service = str(data.get("service", "")).strip()
    if service:
        hc_config["service"] = service

    numeric, err = _validate_numeric_fields(data)
    if err:
        return jsonify(error=err), 400
    hc_config.update(numeric)

    # Check for duplicate name (load-modify-save under lock so concurrent
    # admin requests can't lose updates)
    with HEALTHCHECKS_CFG_LOCK:
        existing = _load_healthchecks()
        if name in existing:
            return jsonify(error="healthcheck with this name already exists"), 409

        # Save
        existing[name] = hc_config
        _save_healthchecks(existing)

    return jsonify(ok=True, name=name, config=hc_config)


@require_admin()
def api_healthchecks_update(name: str):
    """Update an existing healthcheck configuration.

    Partial semantics: only fields present in the payload are changed.
    Type change is full-replace — switching type discards old-type fields.
    """
    data = validate_json_data(request.get_json(silent=True))

    # Hold the lock across the entire load-validate-modify-save sequence
    with HEALTHCHECKS_CFG_LOCK:
        return _apply_healthchecks_update(name, data)


def _apply_healthchecks_update(name: str, data: dict):
    """Apply an update payload to an existing healthcheck config.

    Caller MUST hold HEALTHCHECKS_CFG_LOCK.
    """
    existing = _load_healthchecks()
    if name not in existing:
        return jsonify(error="healthcheck not found"), 404

    hc_config = dict(existing[name])  # copy existing

    # Allow type change
    raw_type = data.get("type") or ""
    if not isinstance(raw_type, str):
        raw_type = ""
    check_type = raw_type.strip().lower()
    if check_type:
        if check_type not in VALID_HC_TYPES:
            return jsonify(error=f"invalid type, must be one of: {', '.join(VALID_HC_TYPES)}"), 400
        new_type = check_type
    else:
        new_type = hc_config.get("type", "")

    changed_type = bool(check_type) and new_type != hc_config.get("type")
    if changed_type:
        # Full replace: keep only name + tuning numbers (which are universal)
        hc_config = {k: vc for k, vc in hc_config.items() if k in ("interval", "timeout", "retries")}
    hc_config["type"] = new_type

    if new_type in ("curl", "soap"):
        url = data.get("url")
        if url is not None:
            url, err = _validate_url(url)
            if err:
                return jsonify(error=err), 400
            hc_config["url"] = url

        service = data.get("service")
        if service is not None:
            service = str(service).strip()
            if service:
                hc_config["service"] = service
            elif "service" in hc_config:
                del hc_config["service"]

        for key in ("failure_keyword", "degraded_keyword"):
            val = data.get(key)
            if val is not None:
                val = str(val).strip()
                if val:
                    hc_config[key] = val
                elif key in hc_config:
                    del hc_config[key]

        if new_type == "soap":
            for key in ("soap_action", "body", "expected_string"):
                val = data.get(key)
                if val is not None:
                    val = str(val).strip()
                    if val:
                        hc_config[key] = val
                    elif key in hc_config:
                        del hc_config[key]

        codes, err = _clean_healthy_codes(data.get("healthy_codes"))
        if err:
            return jsonify(error=err), 400
        if codes is not None:
            if codes:
                hc_config["healthy_codes"] = codes
            elif "healthy_codes" in hc_config:
                del hc_config["healthy_codes"]

    elif new_type == "ping":
        host = data.get("host")
        if host is not None:
            host, err = _validate_host(host)
            if err:
                return jsonify(error=err), 400
            hc_config["host"] = host

    elif new_type == "tcp":
        host = data.get("host")
        if host is not None:
            host, err = _validate_host(host)
            if err:
                return jsonify(error=err), 400
            hc_config["host"] = host

        port = data.get("port")
        if port is not None:
            if isinstance(port, bool):
                return jsonify(error="port must be an integer"), 400
            try:
                port = int(port)
            except (TypeError, ValueError):
                return jsonify(error="port must be an integer"), 400
            if not (1 <= port <= 65535):
                return jsonify(error="port must be between 1 and 65535"), 400
            hc_config["port"] = port

    elif new_type == "rss":
        url = data.get("url")
        if url is not None:
            url, err = _validate_url(url)
            if err:
                return jsonify(error=err), 400
            hc_config["url"] = url
        keywords, err = _validate_rss_keywords(data.get("keywords"))
        if err:
            return jsonify(error=err), 400
        if keywords is not None:
            # Full replace: both levels are always (re)set from the payload
            hc_config["keywords"] = keywords
        elif "keywords" not in hc_config:
            # Type migration into rss: apply the same defaults as create
            hc_config["keywords"] = {
                "red": ["outage", "down", "major issue", "critical"],
                "degraded": ["degraded", "partial", "minor", "investigating"],
            }

    numeric, err = _validate_numeric_fields(data)
    if err:
        return jsonify(error=err), 400
    hc_config.update(numeric)

    # Remove fields no longer relevant for the new type
    if new_type not in ("curl", "soap", "rss"):
        for f in ("url", "healthy_codes", "soap_action", "body", "expected_string", "failure_keyword", "degraded_keyword"):
            hc_config.pop(f, None)
    if new_type in ("curl", "soap"):
        hc_config.pop("host", None)
        hc_config.pop("port", None)
    if new_type != "tcp":
        hc_config.pop("port", None)
    if new_type not in ("ping", "tcp"):
        hc_config.pop("host", None)
    if new_type != "rss":
        hc_config.pop("keywords", None)

    # Save
    existing[name] = hc_config
    _save_healthchecks(existing)

    return jsonify(ok=True, name=name, config=hc_config)


@require_admin()
def api_healthchecks_delete(name: str):
    """Delete a healthcheck configuration."""
    # Load-modify-save under lock so concurrent admin requests can't lose updates
    with HEALTHCHECKS_CFG_LOCK:
        existing = _load_healthchecks()
        if name not in existing:
            return jsonify(error="healthcheck not found"), 404

        del existing[name]
        _save_healthchecks(existing)

    return jsonify(ok=True)


@require_admin()
def api_rss_toggle():
    """Toggle the RSS status feed on/off. Admin only.

    ``POST /api/rss`` with JSON ``{"enabled": bool}``. Persists to config.yaml
    ``rss: {enabled: ...}`` (preserving other rss keys such as title / max_items).
    """
    data = validate_json_data(request.get_json(silent=True))
    if "enabled" not in data:
        return jsonify(error="enabled is required"), 400
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        return jsonify(error="enabled must be a boolean"), 400

    conf = rss_mod.get_rss_config()
    conf["enabled"] = enabled
    # Persist only the stable scalar keys (base_url is derived, never stored).
    to_save = {k: v for k, v in conf.items() if k != "base_url"}
    _save_rss(to_save)

    return jsonify(ok=True, enabled=enabled,
                   url=request.host_url.rstrip("/") + "/feed.xml")


@require_admin()
def api_slack_status():
    """Admin: current Slack integration state (webhook masked)."""
    return jsonify(slack_mod.public_config())


@require_admin()
def api_slack_update():
    """Toggle Slack notifications and/or set the webhook URL.

    ``POST /api/slack`` with JSON ``{"enabled": bool}`` and optionally
    ``{"webhook_url": "https://hooks.slack.com/services/..."}``,
    ``{"channel": "#ops"}``. Persists to config.yaml ``slack: ...``.
    The full webhook token is never returned to the client.
    """
    data = validate_json_data(request.get_json(silent=True))
    if not isinstance(data, dict):
        return jsonify(error="Invalid JSON"), 400

    conf = slack_mod.get_slack_config()
    changed = False

    if "webhook_url" in data:
        wh = str(data.get("webhook_url") or "").strip()
        if wh and not (wh.startswith("https://hooks.slack.com/")
                       or wh.startswith("https://")):
            return jsonify(error="webhook_url must be an https URL"), 400
        conf["webhook_url"] = wh[:512]
        changed = True
    if "channel" in data:
        ch = str(data.get("channel") or "").strip()
        if ch and not INPUT_CHANNEL_RE.match(ch):
            return jsonify(error="channel must match #name or @user"), 400
        conf["channel"] = ch[:80]
        changed = True
    if "clear_queue" in data and data.get("clear_queue") is True:
        removed = slack_mod.clear_queue()
    else:
        removed = 0
    if "enabled" in data:
        if not isinstance(data.get("enabled"), bool):
            return jsonify(error="enabled must be a boolean"), 400
        conf["enabled"] = data["enabled"]
        changed = True

    if changed:
        _save_slack({"enabled": conf["enabled"],
                     "webhook_url": conf["webhook_url"],
                     "channel": conf["channel"],
                     "max_queue": conf["max_queue"]})

    out = slack_mod.public_config()
    if removed:
        out["cleared"] = removed
    return jsonify(ok=True, **out)


# Matches optional # or @ prefix then a valid Slack channel/user name.
INPUT_CHANNEL_RE = re.compile(r"^[#@]?[a-z0-9][a-z0-9._-]{0,78}$", re.IGNORECASE)


def api_settings_status():
    """Public: current UI settings so the template/JS render the right state.

    ``history_enabled`` controls the per-service history timeline (public
    read visibility + API reachability).
    ``healthchecks_enabled`` controls background healthcheck execution.
    """
    return jsonify({
        "history_enabled": history_enabled(),
        "healthchecks_enabled": healthchecks_enabled(),
    })


@require_admin()
def api_settings_update():
    """Update UI settings. Admin only.

    ``POST /api/settings`` with JSON ``{"history_enabled": bool}`` and/or
    ``{"healthchecks_enabled": bool}``.
    Persists to config.yaml ``settings: ...``.
    """
    data = validate_json_data(request.get_json(silent=True))
    if not isinstance(data, dict):
        return jsonify(error="Invalid JSON"), 400

    if "history_enabled" not in data and "healthchecks_enabled" not in data:
        return jsonify(error="history_enabled or healthchecks_enabled is required"), 400

    settings = _load_settings()
    res = {"ok": True}

    if "history_enabled" in data:
        enabled = data.get("history_enabled")
        if not isinstance(enabled, bool):
            return jsonify(error="history_enabled must be a boolean"), 400
        settings["history_enabled"] = enabled
        res["history_enabled"] = enabled

    if "healthchecks_enabled" in data:
        hc_enabled = data.get("healthchecks_enabled")
        if not isinstance(hc_enabled, bool):
            return jsonify(error="healthchecks_enabled must be a boolean"), 400
        settings["healthchecks_enabled"] = hc_enabled
        res["healthchecks_enabled"] = hc_enabled

    _save_settings(settings)
    return jsonify(res)