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
│  ┌─────────────────────────────────────────────────┐   │
│  │              URL Router (Flask, app.py)         │   │
│  │  GET    /               → render index.html     │   │
│  │  GET    /feed.xml, rss → public RSS 2.0 feed    │   │
│  │  GET    /api/rss        → feed availability     │   │
│  │  GET/POST /api/*   → status CRUD, healthchecks  │   │
│  └────────────────────────┬────────────────────────┘   │
│                           │                             │
│  ┌────────────────────────▼────────────────────────┐   │
│  │      Request Validation Layer (statuspage/auth) │   │
│  │  • CSRF token verification (timing-safe)        │   │
│  │  • Authentication check (_not_admin)            │   │
│  │  • Rate-limiting (_check_mutation_rate)         │   │
│  │  • Login lockout (failed-attempt window)        │   │
│  └────────────────────────┬────────────────────────┘   │
│                           │                             │
│  ┌────────────────────────▼────────────────────────┐   │
│  │     Business Logic Layer (statuspage/services)  │   │
│  │  • toggle_item()       → cycles status          │   │
│  │  • rename_item()       → updates name           │   │
│  │  • delete_item()       → removes + compacts     │   │
│  │  • set_notes()         → writes notes + YAML    │   │
│  │  • reorder_items()     → applies position map   │   │
│  │  • record_mutation()   → inserts history row    │   │
│  └────────────────────────┬────────────────────────┘   │
│                           │                             │
│  ┌────────────────────────▼────────────────────────┐   │
│  │        Persistence Layer (statuspage/db)        │   │
│  │  • SQLite (WAL mode, instance/status.db)        │   │
│  │  • YAML config.yaml (_runtime section)          │   │
│  │  • archives/ JSON snapshots                     │   │
│  └─────────────────────────────────────────────────┘   │
│                                                         │
│  ┌─────────────────────────────────────────────────┐   │
│  │   Healthcheck worker thread (healthcheck.py)    │   │
│  │   daemon, fcntl single-instance lock            │   │
│  │   curl / ping / tcp / soap / rss dispatch       │   │
│  │   → writes status + status_history via db.py    │   │
│  └─────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

### 1.2 Tier Breakdown

| Tier | Component | Responsibility |
|------|-----------|----------------|
| **Presentation** | `templates/index.html` + `static/css/style.css` + `static/js/app.js`, `healthchecks.js`, `rss.js` | Client-side rendering, DOM manipulation, event binding, drag-drop reorder, healthcheck admin UI |
| **Web Server** | Flask app with gunicorn or werkzeug dev server | HTTP routing, session management, response generation |
| **Validation Gate** | `_not_admin()`, `_check_csrf()`, `_check_mutation_rate()` (in `statuspage/auth.py`) | Security guards applied before any mutation |
| **Domain Logic** | `toggle_item()`, `set_notes()`, `reorder_items()` etc. (in `statuspage/services.py`) | Core business operations, state transitions |
| **Background Workers** | `healthcheck.py` worker thread | Polling of healthcheck endpoints (curl/ping/tcp/soap/rss) and automatic status transitions |
| **Public Feed** | `statuspage/rss.py` (`build_feed_xml`) | RSS 2.0 feed of status-change history for external consumption |
| **Persistence** | SQLite (WAL), YAML config, JSON archive snapshots | Data storage and backup |

### 1.3 Design Decisions

1. **No ORM (raw SQL)** — SQLite is simple enough that SQLAlchemy adds complexity without benefit. Raw SQL via `sqlite3.Row` gives explicit control over every query.
2. **WAL-mode SQLite** — Enables concurrent reads during writes, so the healthcheck worker thread (which writes status on each poll) never blocks UI reads.
3. **Session-based auth (no JWT)** — Flask signed sessions store state server-side; no token expiration complexity needed for a personal dashboard. A 5-minute sliding idle timeout logs out inactive admin sessions.
4. **Config-driven seed data** — Service names come from `config.yaml` so deployment is just editing a YAML file and restarting.
5. **YAML `_runtime` section** — Runtime changes (status, notes) are serialized back to `config.yaml` so state persists across DB resets and server restarts.
6. **Polling-free UI, RSS feed for consumers** — A prior SSE broadcast layer was removed; the client updates the DOM in response to each successful mutation (no long-lived connections holding gunicorn prefork slots). External consumers (uptime monitors, IFTTT, other dashboards) subscribe to the public `/feed.xml` RSS 2.0 status-change feed instead.
7. **Package layout (`app.py` + `statuspage/`)** — `app.py` is a thin composition root (Flask app factory, route registration, security headers, bootstrap); each concern lives in `statuspage/`: `config.py` (parsing + runtime state), `db.py` (schema + history), `services.py` (domain ops), `routes.py` (HTTP handlers), `auth.py` (session/CSRF/rate-limit/lockout), `healthcheck.py` (worker + check dispatch), `rss.py` (public feed builder). `constants.py` holds shared tunables.
8. **RSS healthchecks are explicit-only** — `type: rss` must be stated; a bare `url` never auto-detects as rss (it stays `curl`). Feeds are fetched via a curl subprocess (redirect policy, max-filesize, max-redirs capped) and parsed with stdlib ElementTree — no third-party RSS library.

---

## 2. Data Flow

### 2.1 Application Bootstrap

```
1. Parse config.yaml via yaml.safe_load()            (statuspage/config.py _load_runtime)
2. Filter keys: skip '_base' (admin secrets), keep '_runtime' for state restore
3. Validate `items` array — must be a list of strings
4. Store parsed config in module-level runtime state
5. Restore `_runtime` state (status/notes) over the DB seed (idempotent)
6. init_db() — schema, seed rows from items, backfill (statuspage/db.py)
7. Healthcheck worker: _parse_healthchecks() → start_healthchecks()
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
  → SELECT history rows (status_toggle/note events, most recent first)
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
  ├── _not_admin() — is user authenticated? → 401 if not
  │
  ├── _check_csrf() — does X-CSRF-Token match session token? → 403 if mismatch (3 strikes = session wipe)
  │
  ├── _check_mutation_rate(ip) — has this IP exceeded 5 mutates in 60s? → 429 if rate-limited
  │
  └── Domain function (toggle_item / set_notes / rename_item / delete_item / reorder_items)
       │
       ├── Validate input parameter (not empty, present, correct type)
       │
       ├── Execute single SQL statement within a transaction
       │
       └── Record history entry → status_history table
             (event_type, item_id, old_value, new_value, occurred UTC)
           + per-item history prune (keeps last N rows, outer item_id filter)
           + _save_runtime() → write YAML _runtime section
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

### 2.3 State Restoration Flow (YAML Backing)

On server restart, `config.yaml` under `_runtime` section contains the last known state. This merges over the DB seed data so that runtime changes survive a database reset:

```python
# In _load_runtime() (statuspage/config.py):
if '_runtime' in cfg_data:
    rt = cfg_data['_runtime']
    # Status values (merge with items) — only restore rows that still exist
    # and are not already green (green == default, no churn)
    ...
    # Custom notes — restore non-blank notes for surviving items
```

---

## 3. Technology Stack

| Layer | Technology | Version Constraint | Justification |
|-------|-----------|-------------------|---------------|
| Runtime | Python | 3.10+ (tested 3.12–3.14) | stdlib sqlite3, yaml via pyyaml |
| Web Framework | Flask | Latest from requirements.txt | Lightweight WSGI, built-in sessions |
| Database | SQLite | Bundled with Python 3 | Zero-config file-based DB, WAL mode |
| Config Format | YAML (PyYAML) | — | Human-editable, standard for config files |
| Password Hashing | Werkzeug scrypt | Bundled with Flask | FIPS-compliant cryptographic hash |
| WSGI Server | Gunicorn | 20+ (production only) | Pre-fork worker model, signal handling |
| External Checks | curl (CLI) / ping (CLI) | System packages | `--proto`, `--max-filesize`, `--max-redirs` caps; zero Python HTTP deps |
| Feed Parsing | stdlib `xml.etree.ElementTree` | Python stdlib | Namespace-agnostic local-name scan; tolerant of RSS 2.0 + Atom |
| Feed Output | stdlib `xml.sax.saxutils` escape | Python stdlib | Well-formed RSS 2.0 without a template |
| Frontend | Vanilla JS | ES6+ | Zero dependencies, fast load times |
| Styling | CSS3 | Modern browsers | CSS custom properties, flexbox, media queries |

### Runtime Dependency Graph

```
app.py                      # composition root: app factory, route table, headers, bootstrap
├── flask                    # HTTP routing, sessions, Jinja2 templates
├── pyyaml (PyYAML)          # YAML config parsing + runtime persistence
├── werkzeug.security        # scrypt password hashing, secure cookie handling
│   └── flask                # indirect, via werkzeug
└── statuspage/              # application package
    ├── config.py            # config + _runtime state, healthcheck entry parsing
    ├── db.py                # schema, seeding, history, pruning
    ├── services.py          # domain operations
    ├── routes.py            # HTTP handlers (status CRUD, healthcheck CRUD, feed)
    ├── auth.py              # session, CSRF, rate limit, lockout, idle expiry
    ├── healthcheck.py       # worker thread + curl/ping/tcp/soap/rss dispatch
    └── rss.py               # public RSS 2.0 feed builder
constants.py                 # shared tunables (timeouts, caps, ports)
```

---

## 4. Component Diagrams

### 4.1 Session Authentication Flow

```
Client                          Server
  │                               │
  │ POST /login {user, pass}      │
  │──────────────────────────────>│
  │                               │ _check_mutation_rate(ip) ✓
  │                               │ werkzeug.security check_password_hash()
  │                               │ Failed? → increment _failed_logins[ip]
  │                               │     Check: len(_failed_logins[ip]) >= 5?
  │                               │           → _lockout_ips[ip] = now
  │                               │     Success? → _failed_logins.pop(ip, None)
  │                               │ Set Flask session cookie (signed, HttpOnly, SameSite)
  │ <─────────────────────────────│ Session: {admin_user: True, last_csrf_token: ...}
  │ Set-Cookie                    │
```

### 4.2 CSRF Token Lifecycle

```
Login Success ─────────────────► First Page Load ──────────────────► Mutation
       │                            │                                    │
       ├─ Generate new token         ├─ Inject into <meta> tag            ├─ Extract from header X-CSRF-Token
       ├─ Store in session           └─ Client reads:                     ├─ hmac.compare_digest() (timing-safe)
    Session: {                      document.querySelector(...)           └─ On mismatch → strike counter++ → 3 strikes = full session wipe
         last_csrf_token: "...",    .value
         csrf_strikes: 0
```

### 4.3 Mutation Endpoint Guard Chain

Every mutation endpoint (`/api/toggle/*`, `/api/rename/*`, `/api/delete/*`, `/api/add`, `/api/notes/*`, `/api/reorder`, healthcheck CRUD, feed toggle) applies a three-layer guard before executing the domain function:

```
Incoming POST/PUT/DELETE /api/*
        │
  ┌─────▼──────────┐   False  ┌──────────────────┐
  │ _not_admin()   ├─────────>│ 401 Unauthorized │
  └──────┬─────────┘          └──────────────────┘
         │ True
  ┌──────▼──────────┐   Mismatch ┌───────────────┐
  │ _check_csrf()   ├───────────>│ 403 Forbidden │ (+ strike++)
  └──────┬─────────┘            └───────────────┘
         │ Match
  ┌──────▼──────────────────────┐ True (rate exceeded)  ┌─────────────┐
  │ _check_mutation_rate(ip)    ├──────────────────────>│ 429 Too Many│
  │ Within rate limit?          │                       │  Requests   │
  └──────┬──────────────────────┘                       └─────────────┘
         │ Pass
  ┌──────▼──────────────────────┐
  │ Execute Domain Function     │
  │ toggle_item / set_notes etc │
  └──────┬──────────────────────┘
         │ Success
  ┌──────▼──────────────────────┐
  │ record_mutation()           │  INSERT INTO status_history
  │ (event_type, item_id,       │  + per-item prune (outer item_id filter)
  │  old_value, new_value,      │  + _save_runtime() YAML write
  │  occurred UTC)              │
  └──────┬──────────────────────┘
         │ DB Committed
  ┌──────▼──────────────────────┐
  │ Return JSON {ok: True}      │
  └─────────────────────────────┘
```

---

## 5. Healthcheck Worker

A daemon thread (`healthcheck.py`) polls each configured check at its own `interval` and drives the item's DB status automatically. It is the only writer of healthcheck-driven status rows, and it shares the same history table the admin mutations write.

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
     healthy→unhealthy and unhealthy→healthy both recorded (event_type=status_toggle)
```

**Safety caps** (constants.py): `HEALTHCHECK_INTERVAL_DEFAULT` 60s, timeout default 10s, 3 retries max before degraded, feed body 512 KB, 20 entries scanned, redirect depth 5, curl max-time = timeout + 5.

**Restart semantics:** `PUT /api/healthchecks/<name>` (or delete/create) re-parses config and hot-restarts the worker thread in place — no process restart needed. The per-item history prune carries an outer `item_id = ?` filter so one item flipping can never wipe another item's history (regression-tested).

---

## 6. Security Architecture

### 6.1 Threat Model

| Attack Vector | Mitigation | Location in Code |
|--------------|-----------|------------------|
| Brute-force credential guessing | `LOCKOUT_SECONDS × 2` lockout (60s) after 5 failed attempts per IP; random wait added for timing | `auth.py` (lockout helpers) + login route |
| Session hijacking | Signed session cookies with `secure`, `httponly`, `samesite` flags; auto-rotated CSRF token; 5-min idle expiry | `auth.py`, `app.py` session-cookie config |
| Session fixation | Session regenerated on login | `auth.py` `login` route |
| Cross-Site Request Forgery (CSRF) | Per-request secret token in hidden form field + header; 3-strike policy wipes entire session on mismatch | `auth.py` `_check_csrf()`; token injected via `<meta>` tag for CSP compliance |
| Query-string CSRF bypass | Token read from `form` or `request.body`, NOT from `request.args` | all mutation routes |
| XSS | Zero `innerHTML` of user data; all sensitive content via `textContent`; DOM built with `createElement()` | `static/js/*.js` |
| SQL Injection | Parameterized queries (`?` placeholders); no string interpolation into SQL | `statuspage/db.py`, `services.py` |
| Plaintext password disclosure | Only scrypt hashes in `.env` / config; never logged or returned | `auth.py`, `config.py` `_base` filtering |
| Configuration exposure | `_base.admin` (hash + secret) filtered from `_runtime.config.data`; `.env` is 0600 | `config.py` `_load_runtime()` |
| SSRF via healthcheck URLs | `_safe_url` (http/https only, no userinfo, host allowlist of TLDs) + curl `--proto` pinned to http/https; `_safe_host` for ping/tcp | `config.py` validation, `healthcheck.py` dispatch |
| Feed injection | Feed output escaped via `xml.sax.saxutils.escape`; feed is read-only (no admin token in it) | `statuspage/rss.py` |
| Healthcheck DoS | per-check timeout + worker-thread cap + curl `--max-time`; feed body cap 512 KB | `healthcheck.py`, `constants.py` |

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
   ├── Request authentication (_not_admin)
   ├── CSRF validation (_check_csrf) with timing-safe comparison
   ├── Mutation rate limiting (_check_mutation_rate per IP)
   └── Login lockout (failed-attempt sliding window per IP)

Layer 4: Data Level (SQL Parameterization)
   └── All queries use ? placeholders; no string formatting

Layer 5: Persistence Level (Cryptographic Hashes)
   └── werkzeug scrypt hashing with salt; raw password never stored or logged
```

### 6.3 Lockout Mechanism Details (MC/DC D6)

The lockout uses a **timestamp-based sliding window** per IP:

```python
if not ts or time.time() - max(ts) >= LOCKOUT_SECONDS * 2:
    # Clear the list — all timestamps are expired → remove stale entries
    _failed_logins.pop(ip, None)
else:
    # At least one entry is within the lockout window → reject
    return True
```

**Key insight**: The condition `not ts` handles the case where `_failed_logins[ip]` exists but is an empty list (all entries were manually cleared). This prevents a KeyError while correctly treating an empty list as "no active lockouts."

---

*Document version: 2.2 | Last updated: 2026-08-18 | Author: Simar Sahni*

