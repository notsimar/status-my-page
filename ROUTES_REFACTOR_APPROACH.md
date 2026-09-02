# Refactoring approach for statuspage/routes.py

> **Status (2026-09-01): superseded.** The planned `routes_public.py` split
> and the companion `_healthcheck_{parsing,probing,worker}.py` split were
> started, never wired in, and **deleted** (commit `9a98f46`) — the two
> copies had already drifted (broken DTD guard regex in the split). `routes.py`
> is still the single route file; this doc remains the reference for a
> future, completed split.

## Overview

The 984-line `routes.py` contains all HTTP route handlers mixed with business logic,
validation, and healthcheck management. This refactor splits it into three focused
modules based on responsibility:

## Proposed Module Structure

### 1. `statuspage/routes_public.py` — Public (unauthenticated) routes
- `status_page()` — main page rendering (GET /)
- `feed_xml()` — RSS feed generation (GET /feed.xml)
- `api_rss_status()` — feed availability metadata (GET /api/rss)
- `api_history(item_id)` — public history timeline (GET /api/history/<id>)
- `api_status_public()` — lightweight status for polling (GET /api/status)
- `api_settings_status()` — UI settings (GET /api/settings)
- `api_rss_toggle()` — toggle RSS on/off (POST /api/rss)

### 2. `statuspage/routes_admin.py` — Admin-only routes
- `api_toggle(item_id)` — cycle status (POST /api/toggle/<id>)
- `api_rename(item_id)` — rename service (POST /api/rename/<id>)
- `api_notes(item_id)` — update notes (POST /api/notes/<id>)
- `api_add()` — add new service (POST /api/add)
- `api_delete(item_id)` — delete service (POST /api/delete/<id>)
- `api_healthcheck_run()` — run healthchecks once (POST /api/healthcheck/run)
- `api_reorder()` — reorder services (POST /api/reorder)
- `api_export_static()` — export static HTML (GET/POST /api/export/static)
- `api_healthchecks()` — list healthchecks (GET /api/healthchecks)
- `api_healthchecks_create()` — create healthcheck (POST /api/healthchecks)
- `api_healthchecks_update(name)` — update healthcheck (PUT /api/healthchecks/<name>)
- `api_healthchecks_delete(name)` — delete healthcheck (DELETE /api/healthchecks/<name>)
- `api_rss_toggle()` — toggle RSS (POST /api/rss) [duplicate but admin-scoped]
- `api_slack_status()` — Slack state (GET /api/slack)
- `api_slack_update()` — toggle Slack (POST /api/slack)
- `login_route()` — admin login (POST /login)
- `logout_route()` — admin logout (POST /logout)
- `auth_check_route()` — check auth status (GET /auth-check)
- `csrf_token_route()` — CSRF token (GET /api/csrf-token)

### 3. `statuspage/routes_api.py` — API/internal routes
- `api_csrf()` — CSRF token (GET /api/csrf-token)
- `api_history_clear(item_id)` — clear history (POST /api/history/<id>/clear)
- `api_toggle(item_id)` — cycle status (also used by admin, but shared logic)
- `api_rename(item_id)` — also used by admin
- Shared helpers that both admin and public may need

## Key Refactoring Steps

### Step 1: Extract route handler functions
Each `def function_name():` block in routes.py becomes its own function in the
appropriate new module. Preserve exact function signatures and decorators.

### Step 2: Move imports declaratively
Each new module imports only what it needs from `statuspage.*` and `flask`:
- `routes_public.py`: needs `healthchecks_enabled()`, `history_enabled()`, `get_logo_url()`
- `routes_admin.py`: needs `require_admin`, `check_csrf`, `check_mutation_rate`, all service functions
- `routes_api.py`: minimal imports, mostly validation helpers

### Step 3: Consolidate duplicate decorator logic
The `require_admin(require_csrf=True, require_rate_limit=True)` decorator pattern
appears throughout. After splitting:
- `routes_admin.py` applies `@require_admin()` to all functions
- `routes_public.py` has no auth decorators
- Consider creating `@public_route` and `@api_route` base decorators

### Step 4: Address the `_redact_healthcheck()` helper
Currently defined in `routes.py` at line 120. After splitting:
- Move to `statuspage/routes_admin.py` since it's admin-facing
- Or create `statuspage/healthcheck_helpers.py` if used across modules

### Step 5: Update `app.py` route registrations
Currently routes are registered in `app.py` via:
```python
from statuspage.routes import status_page, feed_xml, ...
```
After refactoring, update to import from the specific modules:
```python
from statuspage.routes_public import status_page, feed_xml, api_rss_status, ...
from statuspage.routes_admin import api_toggle, api_rename, ...
from statuspage.routes_api import api_csrf, api_history_clear, ...
```
Then register each group:
```python
# Public
app.add_url_rule("/", "status_page", status_page)
# Admin
app.add_url_rule("/api/toggle/<int:item_id>", "api_toggle", api_toggle, methods=["POST"])
# API
app.add_url_rule("/api/csrf-token", "api_csrf", api_csrf)
```

### Step 6: Handle shared state
Several modules import from `statuspage`:
- `routes.py` imports `from statuspage import rss as rss_mod` and `from statuspage import slack as slack_mod`
- These are used in route handlers
- After splitting, ensure each new module imports what it needs
- Consider moving shared re-exports to `statuspage/__init__.py`

## Priority Order

1. **Split routes_public.py first** — least complex, no auth decorators, tests can
   verify public endpoints independently
2. **Split routes_admin.py second** — most complex, has `@require_admin()` on every
   function, but follows the same pattern
3. **Split routes_api.py last** — smallest, mostly validation + shared helpers

## Estimated Effort

- **routes_public.py**: ~2-3 hours ( ~20 route functions, no auth )
- **routes_admin.py**: ~4-5 hours ( ~30 route functions, `@require_admin()` on each )
- **routes_api.py**: ~1-2 hours ( ~8 route functions, shared validators )
- **Update app.py**: ~1 hour
- **Total**: ~8-11 hours

## Risk Mitigation

- Keep the old `statuspage/routes.py` intact as a fallback until new modules are verified
- Write unit tests for each new module before removing from routes.py
- Use git branches: `refactor/routes-split` — if anything breaks, `git reset --hard main`
- Run the existing test suite after each module is created to catch import breakage early