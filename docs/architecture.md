# Status My Page — Architecture Overview

## Table of Contents

- [1. System Design](#1-system-design)
- [2. Data Flow](#2-data-flow)
- [3. Technology Stack](#3-technology-stack)
- [4. Component Diagrams](#4-component-diagrams)
- [5. Healthcheck Worker](#5-healthcheck-worker)
- [6. Security Architecture](#6-security-architecture)

---

## 1. System Design

### 1.1 Architecture Model

```
┌─────────────────────────────────────────────────────────┐
│                    Client Browser                       │
│  ┌──────────────┐  ┌──────────────────────────────────┐ │
│  │   HTML UI    │  │ Vanilla JS (app.js,              │ │
│  │ (Jinja2      │  │  healthchecks.js, rss.js)        │ │
│  │  templates)  │  │ DOM-manip, drag-drop, admin UI   │ │
│  └──────────────┘  └──────────────────────────────────┘ │
└─────────────────────────┬───────────────────────────────┘
                          │ HTTP over TCP
                          ▼
┌─────────────────────────────────────────────────────────┐
│                  Flask Application Server               │
│                                                         │
│  ┌─────────────────────────────────────────────────┐    │
│  │              URL Router (Flask, app.py)         │    │
│  │  GET    /               → render index.html     │    │
│  │  GET    /feed.xml, rss → public RSS 2.0 feed    │    │
│  │  GET    /api/rss        → feed availability     │    │
│  │  GET/POST /api/*   → status CRUD, healthchecks  │    │
│  └────────────────────────┬────────────────────────┘    │
│                           │                             │
│  ┌────────────────────────▼────────────────────────┐    │
│  │      Request Validation Layer (statuspage/auth) │    │
│  │  • CSRF token verification (timing-safe)        │    │
│  │  • Authentication check (session admin flag)    │    │
│  │  • Rate-limiting (check_mutation_rate)          │    │
│  │  • Login lockout (failed-attempt window)        │    │
│  └────────────────────┬────────────────────────────┘    │
│                           │                             │
│  ┌────────────────────────▼────────────────────────┐    │
│  │     Business Logic Layer (statuspage/services)  │    │
│  │  • toggle_item()       → cycles status          │    │
│  │  • rename_item()       → updates name           │    │
│  │  • delete_item()       → removes + compacts     │    │
│  │  • set_notes()         → writes notes to DB     │    │
│  │  • reorder_items()     → applies position map   │    │
│  │  • record_mutation()   → inserts history row    │    │
│  └────────────────────────┬────────────────────────┘    │
│                           │                             │
│  ┌────────────────────────▼────────────────────────┐    │
│  │        Persistence Layer (statuspage/db)        │    │
│  │  • SQLite (WAL mode, instance/status.db)        │    │
│  │  • Read-only YAML config.yaml (seeding input)   │    │
│  │  • archives/ JSON snapshots                     │    │
│  └─────────────────────────────────────────────────┘    │
│                                                         │
│  ┌─────────────────────────────────────────────────┐    │
│  │  Healthcheck worker (statuspage/_healthcheck_   │    │
│  │  impl.py; facade in statuspage/healthcheck.py)  │    │
│  │   daemon, fcntl single-instance lock            │    │
│  │   curl / ping / tcp / soap / rss dispatch       │    │
│  │   due probes run in a bounded thread pool       │    │
│  │   → writes status + status_history via db.py    │    │
│  └─────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────┘
```

### 1.2 Tier Breakdown

| Tier                   | Component                                                                                         | Responsibility                                                                                  |
|------------------------|---------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------|
| **Presentation**       | `templates/index.html` + `static/css/style.css` + `static/js/app.js`, `healthchecks.js`, `rss.js` | Client-side rendering, DOM manipulation, event binding, drag-drop reorder, healthcheck admin UI |
| **Web Server**         | Flask app with gunicorn or werkzeug dev server                                                    | HTTP routing, session management, response generation                                           |
| **Validation Gate**    | `require_admin()` guard chain (in `statuspage/auth.py`: session check, `check_csrf`, `check_mutation_rate`) | Security guards applied before any mutation; uniform 403 + `X-Auth-Error` header |
| **Domain Logic**       | `toggle_item()`, `set_notes()`, `reorder_items()` etc. (in `statuspage/services.py`)              | Core business operations, state transitions             |
| **Background Workers** | healthcheck worker thread (`statuspage/_healthcheck_impl.py`, facade in `statuspage/healthcheck.py`) | Polling of healthcheck endpoints (curl/ping/tcp/soap/rss) and automatic status transitions      |
| **Public Feed**        | `statuspage/rss.py` (`build_feed_xml`)                                                            | RSS 2.0 feed of status-change history for external consumption                                  |
| **Persistence**        | SQLite (WAL), YAML config, JSON archive snapshots                                                 | Data storage and backup                                                                         |

### 1.3 Design Decisions

1. **No ORM (raw SQL)** — SQLite is simple enough that SQLAlchemy adds complexity without benefit. Raw SQL via `sqlite3.Row` gives explicit control over every query.
2. **WAL-mode SQLite** — Enables concurrent reads during writes, so the healthcheck worker thread (which writes status on each poll) never blocks UI reads.
3. **Session-based auth (no JWT)** — Flask signed sessions store state server-side; no token expiration complexity needed for a personal dashboard. A 5-minute sliding idle timeout logs out inactive admin sessions.
4. **Config-driven seed data** — Service names in `config.yaml` act as read-only provisioning input; new items are inserted into SQLite on startup.
5. **Database as Single Source of Truth** — Runtime mutations (status, notes, reorder, items, history) are maintained directly in SQLite.
6. **Polling-free UI, RSS feed for consumers** — A prior SSE broadcast layer was removed; the client updates the DOM in response to each successful mutation (no long-lived connections holding gunicorn prefork slots). External consumers (uptime monitors, IFTTT, other dashboards) subscribe to the public `/feed.xml` RSS 2.0 status-change feed instead.
7. **Package layout (`app.py` + `statuspage/`)** — `app.py` is a thin composition root (Flask app factory, route registration, security headers, bootstrap); each concern lives in `statuspage/`: `config.py` (parsing + migration + runtime state), `db.py` (schema + history), `services.py` (domain ops), `routes.py` (HTTP handlers), `auth.py` (session/CSRF/rate-limit/lockout), `healthcheck.py` (integration facade; implementation in `statuspage/_healthcheck_impl.py`), `slack.py` (notification outbox), `rss.py` (public feed builder), `logging_setup.py` (structured logs). `constants.py` holds shared tunables. The repo-root `healthcheck.py` is a compatibility alias so `import healthcheck` keeps working for tests and tooling.
   - *WIP:* `routes_public.py` (public route handlers) and `_healthcheck_parsing/probing/worker.py` are unfinished modular splits that nothing imports yet (`ROUTES_REFACTOR_APPROACH.md` tracks this); they must be finished and re-exported — or deleted — before any doc treats them as live components.
8. **RSS healthchecks are explicit-only** — `type: rss` must be stated; a bare `url` never auto-detects as rss (it stays `curl`). Feeds are fetched via a curl subprocess (redirect policy, max-filesize, max-redirs capped) and parsed with stdlib ElementTree — no third-party RSS library.

---

## 2. Data Flow

### 2.1 Application Bootstrap

```
1. Parse config.yaml via yaml.safe_load()
2. Validate `items` array — list of configured strings
3. init_db() — create schema if needed, sync new items from config into DB
4. Healthcheck worker: _parse_healthchecks() → start_healthchecks()
   spawns the daemon worker thread if any healthcheck is configured
```

### 2.2 Request Lifecycle

#### Read Requests (Status Page Render)

```
GET /
  → ensure live config is loaded
  → db.execute('SELECT * FROM status_items ORDER BY position')
  → Merge SQL rows with runtime notes/state
  → Jinja2 render templates/index.html with merged data
  → Inject CSRF token into <meta> tag (CSP-safe)
```

#### Public Status-Change Feed

```
GET /feed.xml   (alias: GET /rss)
  → rss.py build_feed_xml(db, base_url)
  → SELECT history rows (event_type "status"/"notes" events, most recent first)
  → Emit RSS 2.0 <rss> document:
      <channel>  — title from config, link = host_url + "/"
      <item>     — per change: title "Name: Old → New", description
                  with the note text, pubDate = occurrence (RFC-822),
                  guid  = host_url + /#<item_id>-<occurred>
  → 404 if the feed feature is disabled in config
```

#### Mutation Requests

```
POST /api/toggle/<id> / POST /api/rename/<id> / etc.
  │
  ├── require_admin() guard chain (statuspage/auth.py), all failures → 403:
  │     • not admin → 403 + X-Auth-Error: not-logged-in
  │     • X-CSRF-Token mismatch → 403 + X-Auth-Error: csrf
  │     •   (3 failed CSRFs = full session wipe)
  │     • mutation rate limit (60 mutations/IP/60s) → 403 + X-Auth-Error: rate-limited
  │
  └── Domain function (toggle_item / set_notes / rename_item / delete_item / reorder_items)
       │
       ├── Validate input parameter (not empty, present, correct type)
       │
       ├── Execute single SQL statement within a transaction
       │
       └── Record history entry → status_history table
             (event_type "status" or "notes", old_value, new_value,
              occurred UTC, per-item prune to last 100 rows)
```

#### Healthcheck Admin Requests

```
POST/GET /api/healthchecks, PUT/DELETE /api/healthchecks/<name>
  → same auth+CSRF+rate guard chain
  → validate type (curl|ping|tcp|soap|rss), _safe_url/_safe_host,
    numeric bounds, rss keyword lists (list-of-strings, ≤ 32 words)
  → rewrite config.yaml healthchecks section (config.py)
  → re-parse + hot-restart worker (healthcheck.py configure + restart)
```

### 2.3 State Management

All state transitions and service entries are committed directly to SQLite (`instance/status.db`). `config.yaml` is used strictly on startup to seed any new services defined by the user without overwriting existing database state. Snapshots before DB initialization are automatically saved under `archives/`.

---

## 3. Technology Stack

| Layer            | Technology                       | Version Constraint           | Justification                                                           |
|------------------|----------------------------------|------------------------------|-------------------------------------------------------------------------|
| Runtime          | Python                           | 3.9+ (tested 3.9–3.14)       | stdlib sqlite3, yaml via pyyaml                                         |
| Web Framework    | Flask                            | Latest from requirements.txt | Lightweight WSGI, built-in sessions                                     |
| Database         | SQLite                           | Bundled with Python 3        | Zero-config file-based DB, WAL mode                                     |
| Config Format    | YAML (PyYAML)                    | —                            | Human-editable, standard for config files                               |
| Password Hashing | Werkzeug scrypt                  | Bundled with Flask           | FIPS-compliant cryptographic hash                                       |
| WSGI Server      | Gunicorn                         | 20+ (production only)        | Pre-fork worker model, signal handling                                  |
| External Checks  | curl (CLI) / ping (CLI)          | System packages              | `--proto`, `--max-filesize`, `--max-redirs` caps; zero Python HTTP deps |
| Feed Parsing     | stdlib `xml.etree.ElementTree`   | Python stdlib                | Namespace-agnostic local-name scan; tolerant of RSS 2.0 + Atom          |
| Feed Output      | stdlib `xml.sax.saxutils` escape | Python stdlib                | Well-formed RSS 2.0 without a template                                  |
| Frontend         | Vanilla JS                       | ES6+                         | Zero dependencies, fast load times                                      |
| Styling          | CSS3                             | Modern browsers              | CSS custom properties, flexbox, media queries                           |

### Runtime Dependency Graph

```
app.py                      # composition root: app factory, route table, headers, bootstrap
├── flask                    # HTTP routing, sessions, Jinja2 templates
├── pyyaml (PyYAML)          # YAML config parsing + runtime persistence
├── python-dotenv            # .env.local / .env auto-loading at startup
├── werkzeug.security        # scrypt password hashing, secure cookie handling
│   └── flask                # indirect, via werkzeug
└── statuspage/              # application package
    ├── config.py            # config loading/migration, path getters, settings/slack/rss loaders
    ├── db.py                # schema, seeding, history, archive prune
    ├── services.py          # domain operations (toggle/notes/rename/add/delete/reorder)
    ├── routes.py            # HTTP handlers (status CRUD, healthcheck CRUD, feed, export)
    ├── auth.py              # session, CSRF, rate limit, login lockout, idle expiry
    ├── healthcheck.py       # integration facade; loads implementation from _healthcheck_impl.py
    ├── _healthcheck_impl.py # worker thread, bounded probe pool, curl/ping/tcp/soap/rss dispatch
    ├── slack.py             # persistent Slack outbox + digest flush on logout
    ├── rss.py               # public RSS 2.0 feed builder
    └── logging_setup.py     # structured access.log / app.log (5 MB x 3 rotation)
constants.py                 # shared tunables (timeouts, caps, ports)
(healthcheck.py at repo root = top-level compatibility alias for statuspage.healthcheck)
```

---

## 4. Component Diagrams

### 4.1 Session Authentication Flow

```
Client                          Server
  │                               │
  │ POST /login {user, pass}      │
  │──────────────────────────────>│
  │                               │ is_locked(ip)? (5 fail within 30s window)
  │                               │   yes → 429 + retry_after
  │                               │ werkzeug.security check_password_hash()
  │                               │   (timed against stored scrypt hash;
  │                               │    username compared timing-safely —
  │                               │    no user enumeration)
  │                               │ Failed? → record_attempt(ip) + 401
  │                               │ Success? → regenerate session (fixation
  │                               │   defense), start 5-min sliding idle
  │                               │   expiry clock, clear IP's fail list
  │ <─────────────────────────────│ {ok:true} + Set-Cookie
```

### 4.2 CSRF Token Lifecycle

```
Login Success ─────────────────► First Page Load ──────────────────► Mutation
       │                            │                                    │
       ├─ Generate new token         ├─ Inject into <meta> tag            ├─ Extract from header X-CSRF-Token
       ├─ Store in session           └─ Client reads:                     ├─ hmac.compare_digest() (timing-safe)
    Session: {                      document.querySelector(...)           ├─ On mismatch → per-IP failure counter++
         _csrf: "..."}                    .value                            3 failures = full session wipe
                                                          └─ On success → token rotated, failure counter cleared
```

### 4.3 Mutation Endpoint Guard Chain

Every mutation endpoint (`/api/toggle/*`, `/api/rename/*`, `/api/delete/*`, `/api/add`, `/api/notes/*`, `/api/reorder`, healthcheck CRUD, feed toggle) applies the `require_admin()` guard chain before executing the domain function. All three failure modes return **403** with an `X-Auth-Error` header so the client can tell them apart without the server leaking which guard tripped:

```
Incoming POST/PUT/DELETE /api/*
        │
  ┌─────▼──────────────┐  not logged in  ┌────────────────────────────┐
  │ session[admin]?    ├────────────────>│ 403 X-Auth-Error:          │
  └──────┬─────────────┘                 │      not-logged-in         │
         │ True
  ┌──────▼──────────┐  mismatch ┌────────────────────────────┐
  │ check_csrf()    ├──────────>│ 403 X-Auth-Error: csrf     │
  └──────┬──────────┘           │ (3 fails = session wipe)   │
         │ Match                └────────────────────────────┘
  ┌──────▼───────────────────────────┐ rate exceeded (60/60s)  ┌─────────────┐
  │ check_mutation_rate(ip)          ├────────────────────────>│ 403         │
  │ Within rate limit?               │                         │ X-Auth-Error:│
  └──────┬───────────────────────────┘                         │ rate-limited│
         │ Pass                                                └─────────────┘
  ┌──────▼──────────────────────┐
  │ Execute Domain Function     │
  │ toggle_item / set_notes etc │
  └──────┬──────────────────────┘
         │ Success
  ┌──────▼──────────────────────┐
  │ record_history()            │  INSERT INTO status_history
  │ (event_type "status"/       │  + per-item prune (outer item_id filter,
  │  "notes", old_value,        │      keeps last 100 rows per item)
  │  new_value, occurred UTC)   │  + Slack outbox enqueue (best-effort)
  └──────┬──────────────────────┘
         │ DB Committed
  ┌──────▼──────────────────────┐
  │ Return JSON {ok: True}      │
  └─────────────────────────────┘
```

---

## 5. Healthcheck Worker

A daemon thread (`statuspage/_healthcheck_impl.py`) polls each configured check at its own `interval` and drives the item's DB status automatically. Due probes run concurrently in a bounded thread pool (8 workers) so one slow endpoint cannot delay other services' intervals; result handling (fail counters, status flips, scheduling) is applied serially afterwards. It is the only writer of healthcheck-driven status rows, and it shares the same history table the admin mutations write. The top-level `healthcheck.py` is a compatibility alias that loads the implementation under the historical module name — both import paths resolve to the same module object.

```
worker loop (one thread, fcntl-locked to a single instance)
  │
  for each healthcheck entry (name, type, ...):
  │
  ├── type == curl   → curl -s -o - -w "\n%{http_code}" → code in healthy_codes?
  │                    (+ optional expected_string substring match)
  ├── type == ping   → ping -c1 -W timeout host → exit code 0?
  ├── type == tcp    → socket.create_connection((host, port), timeout) → connected?
  ├── type == soap   → curl POST soap body → code in healthy_codes AND
  │                    (expected_string absent OR in body)
  └── type == rss    → curl feed (max-filesize RSS_MAX_BYTES, max-redirs cap)
                       → 200 + parseable XML, else fetch-failure ladder
                       → case-insensitive scan of first RSS_MAX_ITEMS items
                         (title/description/summary, namespace-agnostic):
                           red word       → red (immediate, no retry ladder)
                           degraded word  → degraded
                           clean          → green
  │
  └── on change → db.set_status + history row (per-item prune)
     healthy→unhealthy and unhealthy→healthy both recorded (event_type="status")
```

**Safety caps** (constants.py / implementation): `HEALTHCHECK_INTERVAL_DEFAULT` 60s, `HEALTHCHECK_TIMEOUT_DEFAULT` 10s, `HEALTHCHECK_RETRIES_DEFAULT` 2 (retries are clamped to a minimum of 1 at parse time — see the retry ladder above). Feed body 512 KB, 20 entries scanned, redirect depth 5, curl max-time = timeout + 5.

> **WIP note:** `statuspage/_healthcheck_parsing.py`, `_healthcheck_probing.py`,
> and `_healthcheck_worker.py` are an unfinished modular split — import
> nowhere in the app or test suite (test docstrings reference the historical
> function names only). The running app uses `statuspage/_healthcheck_impl.py`
> (loaded as top-level `healthcheck`). Treat `_impl.py` as the source of truth
> until the split lands or the stubs are deleted.

**Restart semantics:** `PUT /api/healthchecks/<name>` (or delete/create) re-parses config and hot-restarts the worker thread in place — no process restart needed. The per-item history prune carries an outer `item_id = ?` filter so one item flipping can never wipe another item's history (regression-tested).

---

## 6. Security Architecture

### 6.1 Threat Model

| Attack Vector                     | Mitigation                                                                                                                          | Location in Codea                                   |
|-----------------------------------|-------------------------------------------------------------------------------------------------------------------------------------|-----------------------------------------------------|
| Brute-force credential guessing   | 5 failed attempts per IP within a 30s sliding window → IP locked for 30s (`LOCKOUT_SECONDS`); lockout state persisted to DB so worker restarts can't reset it; timing-safe username check prevents account enumeration                        | `auth.py` (lockout helpers) + login route           |
| Session hijacking                 | Signed session cookies with `secure`, `httponly`, `samesite` flags; auto-rotated CSRF token; 5-min idle expiry                      | `auth.py`, `app.py` session-cookie config           |
| Session fixation                  | Session regenerated on login                                                                                                        | `auth.py` `login` route                             |
| Cross-Site Request Forgery (CSRF) | Per-session secret token accepted ONLY via the `X-CSRF-Token` header (never query/form — query strings are logged); token rotated after every successful mutation; 3 failures (per IP) wipe the entire session | `auth.py` `check_csrf()`;                             |
|                                   |                                                                                                                                         |  token injected into the page via `<meta>` tag for CSP compliance |
| Query-string CSRF bypass          | Token read ONLY from the request header, never from `request.args` (query strings are logged)                                             | `auth.py` `check_csrf()`                             |
| XSS                               | Zero `innerHTML` of user data; all sensitive content via `textContent`; DOM built with `createElement()`                            | `static/js/*.js`                                    |
| SQL Injection                     | Parameterized queries (`?` placeholders); no string interpolation into SQL                                                          | `statuspage/db.py`, `services.py`                   |
| Plaintext password disclosure     | Only scrypt hashes in `.env` / config; never logged or returned                                                                     | `auth.py`, `config.py` `_base` filtering            |
| Configuration exposure            | `_base.admin` (hash + secret) filtered from exposed config; `.env` is 0600                                                          | `config.py` `load_config()`                          |
| SSRF via healthcheck URLs         | `_safe_url` (http/https only, no userinfo, host allowlist of TLDs) + curl `--proto` pinned to http/https; `_safe_host` for ping/tcp | `config.py` validation, `healthcheck.py` dispatch   |
| Feed injection                    | Feed output escaped via `xml.sax.saxutils.escape`; feed is read-only (no admin token in it)                                         | `statuspage/rss.py`                                 |
| Healthcheck DoS                   | per-check timeout + worker-thread cap + curl `--max-time`; feed body cap 512 KB                                                     | `healthcheck.py`, `constants.py`                    |

### 6.2 Defense-in-Depth Layers

```
Layer 1: Network Level (Reverse Proxy)
   ├── HTTPS termination (Nginx/Caddy)
   └── X-Forwarded-Proto → Flask secure cookie handling

Layer 2: Transport Level (Flask Headers)
   ├── Content-Security-Policy: default-src 'self'
   ├── X-Content-Type-Options: nosniff
   ├── X-Frame-Options: DENY
   ├── Referrer-Policy: strict-origin-when-cross-origin
   └── Permissions-Policy

Layer 3: Application Level (Auth + CSRF + Rate Limit)
   ├── Request authentication (require_admin guard)
   ├── CSRF validation (check_csrf) with timing-safe comparison
   ├── Mutation rate limiting (check_mutation_rate: 60 muts/IP/60s)
   └── Login lockout (failed-attempt sliding window per IP)

Layer 4: Data Level (SQL Parameterization)
   └── All queries use ? placeholders; no string formatting

Layer 5: Persistence Level (Cryptographic Hashes)
   └── werkzeug scrypt hashing with salt; raw password never stored or logged
```

### 6.3 Lockout Mechanism Details (MC/DC D6)

The lockout uses a **timestamp sliding window** per IP: each failed login
appends a timestamp; on the next login attempt, entries older than
`LOCKOUT_SECONDS` (30s) are pruned, and the IP is locked while ≥
`MAX_LOGIN_ATTEMPTS` (5) fresh failures remain. The lockout expiry is tracked
in `_lockout_until` so the API can return a real `retry_after` count.

```python
def is_locked(ip: str) -> bool:
    now = time.time()
    ts = _failed_logins.get(ip, [])
    if not ts:
        return False
    ts = [t for t in ts if now - t < LOCKOUT_SECONDS]   # prune stale entries
    if ts:
        _failed_logins[ip] = ts
    else:
        _failed_logins.pop(ip, None)
    if len(ts) >= MAX_LOGIN_ATTEMPTS:                    # 5 fresh failures
        _lockout_until[ip] = max(_lockout_until.get(ip, 0), ts[-1] + LOCKOUT_SECONDS)
        return True
    return False
```

**Key insight**: the pruning step is what makes the lockout always expire —
including lockouts rehydrated from the persisted rate-limits table after a gunicorn
worker restart (stale persisted entries must never re-lock an IP permanently).
The `not ts` guard handles the edge case where an IP has a (now-empty) list
record without raising.

---

*Document version: 2.3 | Last updated: 2026-08-27 | Author: Simar Sahni*

