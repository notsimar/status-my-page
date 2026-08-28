# Status-My-Page — Architecture Review

Review scope: full system (`app.py`, `statuspage/`, `healthcheck`, templates, deploy).
Source: `~/Developer/status-my-page/` @ commit `357d741`.

---
## 1. Module Structure

### Layout
```
app.py                  Flask factory, route registration, security headers
statuspage/
  config.py              YAML config + DB path init; atomic writes; backup rotation
  db.py                   SQLite (WAL) schema, queries, mutations, archive
  auth.py                 Auth decorators (`require_admin`), CSRF, rate limits
  routes.py (984 lines)   All HTTP handlers (public + admin + healthcheck)
  services.py             Business logic layer (toggle, reorder, notes, delete)
  healthcheck.py          Module bridge (`configure_healthcheck`, thread start)
  _healthcheck_impl.py    Actual healthcheck worker + probes
  logging_setup.py        Request/app logging
  rss.py, slack.py        Sub-modules
input_filter.py            Centralized input validation
constants.py              Magic numbers / defaults
```

### Issues Found

| ID | Severity | File | Description | Status |
|---|---|---|---|---|
A1 | High | `routes.py` | 984-line monolith; mixes public routes, admin routes, healthcheck admin, validation helpers (`_redact_healthcheck`), static HTML generator (`generate_static_html`). `ROUTES_REFACTOR_APPROACH.md` proposes split (`routes_public` / `routes_admin` / `routes_api`) — not executed. | Pending |
A2 | High | `routes.py` + `app.js` | `generate_static_html()` (routes.py:160) duplicates CSS/style logic; template `index.html` mirrors same markup. Static export uses `_html.escape()` directly (good) but builds HTML via f-string instead of Jinja render — maintenance divergence risk. | Flag |
A3 | Medium | `app.py` | Dynamic `import app` avoided (good); `init_config_paths()` then `load_config()` called before route imports — order-sensitive but documented. | OK |
A4 | Medium | `healthcheck.py` | Uses `import healthcheck as hc` + `sys.modules` aliasing (`_IMPL_PATH`) to allow monkeypatching (`_BASE_DIR`). Fragile but intentional (tests patch it). `routes.py` imports `healthcheck` directly — same module object, consistent. | OK |
A5 | Low | `routes_admin` | Healthcheck admin routes (`api_healthchecks_create/update/delete`) embedded in `routes.py`; should split per `ROUTES_REFACTOR_APPROACH.md`. | Pending |

### Circular / Fragile Coupling
- `routes.py` imports `statuspage.db.get_connection` and `statuspage.services.*`; services import `statuspage.db`. No cycles.
- `routes.py` imports `healthcheck` (shared module); `healthcheck` imports `statuspage.config`. One-directional.
- `routes.py` uses `import healthcheck as hc_module` — same module, fine.
- **No actual import cycles**, but the 984-line routes file creates logical coupling: changing one admin route requires scanning entire file.

---
## 2. Data Flow

### DB (SQLite + WAL)
- `_new_connection()` sets `PRAGMA journal_mode=WAL` ✅
- Per-request singleton (`g.db`) via `get_connection()`; teardown closes (`_close_db`) ✅
- `delete_item()` removes `status_items`, `status_history`, reindexes positions, and prunes healthcheck config via YAML — all in same transaction ✅
- `archive_db_snapshot()` writes JSON archive before `init_db()` resets; bounded by `MAX_ARCHIVES` ✅
- `init_db()` reads from `config.yaml` (`compute_seed_items()`); does NOT reset DB state unconditionally (preserves existing items, only inserts missing) ✅

### YAML Config (`config.yaml` / `.env.local`)
- `_migrate_config_section()` moves `admin`/`server` into `_base` atomically ✅
- `_write_config_atomic()` uses `tempfile` + `os.replace` + `chmod(0600)` ✅
- `_load_config()` reads from disk on every call (no stale cache for mutations, but `_cfg_cache` exists); `load_config()` = `_load_config_uncached()` — always fresh ✅
- `.env` checked: `STATUS_ADMIN_PASS_HASH` and `STATUS_SECRET_KEY` present (`cat .env`); no truncated `$()` found ✅

### Healthcheck Thread
- Background thread (`_healthcheck_worker`) uses `threading.Lock` (`_HEALTH_LOCK`) for DB writes; opens standalone connection (`_health_db()`) — no Flask `g` contamination ✅
- File advisory lock (`.healthcheck.lock` with `fcntl.flock`) ensures single process worker ✅
- Config reloaded each cycle (`_parse_healthchecks()`); disabled via `STATUS_DISABLE_HEALTHCHECKS` ✅
- `run_healthchecks_once()` (one-shot API endpoint) uses bounded `ThreadPoolExecutor` + hard timeout (`HEALTHCHECK_RUN_HARD_TIMEOUT`) ✅
- Status flip writes DB (`UPDATE`), records history (pruned to `MAX_HISTORY_PER_ITEM`), then queues Slack — transaction closed before Slack enqueue ✅

---
## 3. Auth & Security Architecture

### Auth Flow
```
.login (POST) → validate_json_data → validate_user_input / validate_password
  → check_password_hash (env STATUS_ADMIN_PASS_HASH) → session["admin"] = True
  → set ADMIN_ACTIVE_SINCE_KEY (sliding 5-min idle)
.before_request → enforce_session_idle_expiry() (wipes session if idle > 300s)
```
- Timing-safe comparison via `hmac.compare_digest()` with fixed-length hex ✅
- Rate limit (`is_locked` / `record_attempt`) per IP; `LOCKOUT_SECONDS=30` ✅
- Mutation rate (`check_mutation_rate`) per IP; `MUTATION_MAX=60`, `MUTATION_WINDOW=60` ✅

### CSRF
- Token generated per session (`secrets.token_hex(32)`); stored in `session` (`_csrf`) ✅
- `check_csrf()` reads from `request.headers.get("X-CSRF-Token")` — NOT query param ✅
- Rotates on success; clears session after 3 failures (`MAX_CSRF_FAILURES`) ✅
- `csrfFetch()` in `app.js` reads from `<meta name="csrf-token">` DOM (not global window) ✅

### Route Guards
- `@require_admin()` centralizes auth + CSRF + rate limit; returns uniform 403 with `X-Auth-Error` header (`not-logged-in` / `csrf` / `rate-limited`) ✅
- `api_export_static()` takes `download=true` from `request.args` but requires `@require_admin(require_csrf=False, require_rate_limit=False)` — still admin-only ✅
- No `set_cookie()` leftover for deleted cookie routes (verified via grep) ✅

### Headers
- `security_headers` after_request: `CSP`, `nosniff`, `DENY`, `strict-origin-when-cross-origin`, `Permissions-Policy` ✅
- `SESSION_COOKIE_HTTPONLY=True`, `SAMESITE=Lax`, `SECURE` conditional ✅

---
## 4. Deployment Architecture

### Install (`install.sh`)
- Non-root by default (`ROOTMODE=0`); creates `.venv`, installs vendor wheels first (`--no-index --find-links`) ✅
- Admin hash generated with `python3 -c` + heredoc/stdin (not `$(echo "$PASS" | ...)` shell substitution) ✅
- `.env.local` set to `0600`; backup rotation (`.bak1`..`.bak5`) with `0600` chmod ✅
- Systemd service (`systemd` only when `ROOTMODE=1`) with hardening (`NoNewPrivileges`, `ProtectHome`) ✅

### Runtime
- `gunicorn --bind 0.0.0.0:8920 --workers 2 --timeout 30` ✅
- `restart.sh`, `start.sh`, `stop.sh`, `rebuild.sh` present; `clear_history.sh` for admin maintenance ✅

---
## 5. Summary & Recommendations

| Priority | Recommendation | Target File |
|---|---|---|
| **P0** | Split `routes.py` (984 lines) into `routes_public.py`, `routes_admin.py`, `routes_api.py` per `ROUTES_REFACTOR_APPROACH.md`. Reduces maintenance risk and test isolation. | `routes.py` |
| **P1** | `generate_static_html()` duplicates template logic; consider rendering `index.html` via Jinja (`render_template_string`) or extracting shared markup component. | `routes.py`, `templates/` |
| **P1** | Add rate-limit guard verification on mutation endpoints (`check_mutation_rate`) — already present via `@require_admin()`; confirm through test. | `tests/` |
| **P2** | `archive_db_snapshot()` writes JSON unconditionally on startup; consider hashing current snapshot to skip redundant archive when DB unchanged. | `db.py` |
| **P2** | `routes_admin` healthcheck routes should be split per refactor doc; no security gap, only structural debt. | `routes.py` |
| **P3** | `statuspage/_healthcheck_impl.py` uses `importlib.util` aliasing; document in `README` for future maintainers. | docs |

### Verdict
- **No critical security/blocking bugs** found in current commit (`357d741`).
- **Auth, CSRF, rate limits, session idle, input validation, SQLite WAL, atomic config, healthcheck thread isolation** all verified present and consistent.
- **Structural debt** is the dominant risk: monolithic routes file, duplicated static-render logic.
- Recommend proceeding with `routes.py` split (P0) before adding new admin endpoints (e.g. bulk operations).
