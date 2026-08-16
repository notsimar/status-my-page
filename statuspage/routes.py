"""HTTP routes for status-my-page."""

import sqlite3
from flask import jsonify, render_template, session, request

from statuspage.auth import (
    login_route,
    logout_route,
    auth_check_route,
    csrf_token_route,
    require_admin,
    get_csrf,
)
from statuspage.db import get_connection, get_all_items
from statuspage.healthcheck import (
    get_configured_healthchecks,
    run_healthchecks_once,
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
)
from input_filter import InputRejected, validate_json_data, validate_name, validate_notes, validate_int_param


# ── Public routes ───────────────────────────────────────────────────

def status_page():
    items = get_all_status_items()
    is_admin = session.get("admin", False)
    csrf = get_csrf() if is_admin else ""
    return render_template(
        "index.html", items=items, session_admin=is_admin, csrf_token=csrf
    )


def api_history(item_id: int):
    """Return history for a service. Public read — anyone can see status timeline."""
    result = get_item_history(item_id)
    if result is None:
        from flask import abort
        abort(404)
    return jsonify(result)


def api_healthchecks():
    """Return all configured healthchecks. Public read — no auth required."""
    hc = get_configured_healthchecks()
    return jsonify(hc)


# ── Auth routes ─────────────────────────────────────────────────────

# These are just re-exports
login = login_route
logout = logout_route
auth_check = auth_check_route
api_csrf = csrf_token_route


# ── Admin routes ────────────────────────────────────────────────────

@require_admin()
def api_toggle(item_id: int):
    status = toggle_item(item_id)
    return jsonify(status=status)


@require_admin()
def api_rename(item_id: int):
    data = validate_json_data(request.get_json(silent=True))
    name = validate_name(data.get("name", ""), "name")
    ok, msg = rename_item(item_id, name)
    if not ok:
        from flask import abort
        status_code = 404 if msg == "Not found" else 409
        return jsonify(error=msg), status_code
    return jsonify(ok=True)


@require_admin()
def api_notes(item_id: int):
    data = validate_json_data(request.get_json(silent=True))
    notes = validate_notes(data.get("notes", ""), "notes")
    update_notes(item_id, notes)
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
        from flask import abort
        return jsonify(error="Not found"), 404
    return jsonify(ok=True, name=name)


@require_admin()
def api_healthcheck_run():
    """Trigger a one-shot healthcheck run for all services. Admin only."""
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