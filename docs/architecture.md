# Status My Page — Architecture Overview

## Table of Contents

- [1. System Design](#1-system-design)
- [2. Data Flow](#2-data-flow)
- [3. Technology Stack](#3-technology-stack)
- [4. Component Diagrams](#4-component-diagrams)
- [5. Security Architecture](#5-security-architecture)

---

## 1. System Design

### 1.1 Architecture Model

```
┌─────────────────────────────────────────────────────────┐
│                    Client Browser                       │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐   │
│  │   HTML UI    │  │ Vanilla JS   │  │ EventSource  │   │
│  │ (Jinja2      │  │ (app.js)     │  │ (SSE push)   │   │
│  │  templates)  │  │ DOM-manip    │  │ /events      │   │
│  └──────────────┘  └──────────────┘  └──────────────┘   │
└─────────────────────────┬───────────────────────────────┘
                          │ HTTP / WebSocket over TCP
                          ▼
┌───────────────────────────────────────────────────────────┐
│                  Flask Application Server                 │
│                                                           │
│  ┌────────────────────────────────────────────────────┐   │
│  │              URL Router (Flask)                    │   │
│  │  GET    /        → render index.html               │   │
│  │  GET    /events    → SSE broadcast stream (beta)   │   │
│  │  POST   /login     → session auth                  │   │
│  │  GET/POST /api/*   → CRUD operations               │   │
│  └──────────┬─────────────────────────────────────────┘   │
│             │                                             │
│  ┌──────────▼─────────────────────────────────────────┐   │
│  │           Request Validation Layer                 │   │
│  │  • CSRF token verification                         │   │
│  │  • Authentication check (_not_admin)               │   │
│  │  • Rate-limiting (_check_mutation_rate)            │   │
│  └──────────┬─────────────────────────────────────────┘   │
│             │                                             │
│  ┌──────────▼─────────────────────────────────────────┐   │
│  │           Business Logic Layer                     │   │
│  │  • toggle_item()          → cycles status          │   │
│  │  • rename_item()          → updates name           │   │
│  │  • delete_item()          → removes + compacts     │   │
│  │  • set_notes()            → writes notes + YAML    │   │
│  │  • reorder_items()        → applies position map   │   │
│  │  • _record_mutation()     → inserts history row    │   │
│  └──────────┬─────────────────────────────────────────┘   │
│             │                                             │
│  ┌──────────▼─────────────────────────────────────────┐   │
│  │           Persistence Layer                        │   │
│  │  • SQLite (WAL mode, status.db)                    │   │
│  │  • YAML config.yaml (_runtime section)             │   │
│  │  • archives/ JSON snapshots                        │   │
│  └──────────┬─────────────────────────────────────────┘   │
│             │                                             │
│  ┌──────────▼───────────────────────────────────────┐     │
│  │           Event Broadcasting Layer (experimental)│     │
│  │  • _sse_subscribers (thread-safe list)           │     │
│  │  • broadcast_reload() → flushes SSE events       │     │
│  └──────────────────────────────────────────────────┘     │
└───────────────────────────────────────────────────────────┘
```

### 1.2 Tier Breakdown

| Tier | Component | Responsibility |
|------|-----------|----------------|
| **Presentation** | `templates/index.html` + `static/css/style.css` + `static/js/app.js` | Client-side rendering, DOM manipulation, event binding, drag-drop reorder |
| **Web Server** | Flask app with gunicorn or werkzeug dev server | HTTP routing, session management, response generation |
| **Validation Gate** | `_not_admin()`, `_check_csrf()`, `_check_mutation_rate()` | Security guards applied before any mutation |
| **Domain Logic** | `toggle_item()`, `set_notes()`, `reorder_items()`, etc. | Core business operations, state transitions |
| **Persistence** | SQLite (WAL), YAML config, JSON archive snapshots | Data storage and backup |

### 1.3 Design Decisions

1. **No ORM (raw SQL)** — SQLite is simple enough that SQLAlchemy adds complexity without benefit. Raw SQL via `sqlite3.Row` gives explicit control over every query.
2. **WAL-mode SQLite** — Enables concurrent reads during writes, critical for the real-time UI polling pattern.
3. **Session-based auth (no JWT)** — Flask signed sessions store state server-side; no token expiration complexity needed for a personal dashboard.
4. **Config-driven seed data** — Service names come from `config.yaml` so deployment is just editing a YAML file and restarting.
5. **YAML `_runtime` section** — Runtime changes (status, notes) are serialized back to `config.yaml` so state persists across DB resets and server restarts.

---

## 2. Data Flow

### 2.1 Application Bootstrap (`_load_runtime()` → L~208)

```
1. Parse config.yaml via yaml.safe_load()
2. Filter keys: skip '_base', '_runtime' sections (config metadata)
3. Validate `items` array — must be a list of strings
4. Store in `_runtime.config.data.items`
5. Seed SQLite DB from runtime items if needed (first-run or on startup)
6. Load any previously persisted YAML runtime state
```

### 2.2 Request Lifecycle

#### Read Requests (Status Page Render)

```
GET /
  → _load_runtime() to ensure live config is loaded
  → db.execute('SELECT * FROM status_items ORDER BY position')
  → Merge SQL rows with _runtime.config.data.items + _runtime.notes
  → Jinja2 render templates/index.html with merged data
  → Inject CSRF token into <meta> tag (CSP-safe)
```

#### Mutation Requests

```
POST /api/toggle/<id> / POST /api/rename/<id> etc.
  │
  ├── _not_admin() — is user authenticated? → 401 if not
  │
  ├── _check_csrf() — does X-CSRF-Token match session token? → 403 if mismatch (3 strikes = session wipe)
  │
  ├── _check_mutation_rate(ip) — has this IP exceeded 5 mutates in 60s? → 429 if rate-limited
  │
  └── Domain function (toggle_item/set_notes/rename_item/delete_item/reorder_items)
       │
       ├── Validate input parameter (not empty, present, correct type)
       │
       ├── Execute single SQL statement within a transaction
       │
       ├── Record history entry via _record_mutation() → status_history table
       │
       ├── If mutation succeeded → broadcast_reload() → flush SSE events to all subscribers
       │
       └── Return JSON response {ok: True} or {status: ...}
```

### 2.3 State Restoration Flow (YAML Backing)

On server restart, `config.yaml` under `_runtime` section contains the last known state. This merges over the DB seed data so that runtime changes survive a database reset:

```python
# In _load_runtime():
if '_runtime' in cfg_data:
    rt = cfg_data['_runtime']
    
    # Status values (merge with items)
    if 'status' in rt and 'data' in rt['status']:
        self._runtime.config.data.status = rt['status']['data']
    
    # Custom notes
    if 'notes' in rt:
        self._runtime.notes = rt['notes']
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
| Frontend | Vanilla JS | ES6+ | Zero dependencies, fast load times |
| Styling | CSS3 | Modern browsers | CSS custom properties, flexbox, media queries |

### Runtime Dependency Graph

```
app.py
├── flask                    # HTTP routing, sessions, Jinja2 templates
├── pyyaml (PyYAML)          # YAML config parsing + runtime persistence  
└── werkzeug.security        # scrypt password hashing, secure cookie handling
    └── flask                 # indirect, via werkzeug
```

---

## 4. Component Diagrams

### 4.1 Session Authentication Flow

```
Client                          Server
  │                               │
  │ POST /login {user, pass}      │
  │───────────────────────────────>│
  │                               │ _check_mutation_rate(ip) ✓
  │                               │ db.execute(SELECT... WHERE user=?)
  │                               │ werkzeug.security check_password_hash()
  │                               │ time.perf_counter_ns timing-safe compare
  │                               │ Failed? → increment _failed_logins[ip]
  │                               │     Check: len(_failed_logins[ip]) >= 5?
  │                               │           → _lockout_ips[ip] = now
  │                               │     Success? → _failed_logins.pop(ip, None)
  │                               │ Set Flask session cookie (signed)
  │ <──────────────────────────────│ Session: {admin_user: True, last_csrf_token: ...}
  │ Set-Cookie                    │
  │                               │
```

### 4.2 CSRF Token Lifecycle

```
Login Success ─────────────────► First Page Load ──────────────────► Mutation
       │                            │                                    │
       ├─ Generate new token         ├─ Inject into <meta> tag             ├─ Extract from header X-CSRF-Token
       ├─ Store in session           └─ Client reads:                      ├─ Compare against session token
       │                            document.querySelector('input[name="csrf_token"]').value  ├─ hmac.compare_digest() (timing-safe)
    Session: {                  }│                                    └─ On mismatch → strike counter++ → 3 strikes = full session wipe
         last_csrf_token: "...",
         csrf_strikes: 0
```

### 4.3 Mutation Endpoint Guard Chain

Each mutation endpoint (`/api/toggle/*`, `/api/rename/*`, `/api/delete/*`, `/api/add`, `/api/notes/*`, `/api/reorder`) applies a three-layer guard before executing the domain function:

```
Incoming POST /api/mutation/*
        │
  ┌─────▼──────────┐   False  ┌──────────────────┐
  │ _not_admin()   ├─────────>│ 401 Unauthorized │  (Response)
  │  Auth?         │          └──────────────────┘
  └──────┬─────────┘
         │ True
  ┌──────▼──────────┐   Mismatch ┌───────────────┐
  │ _check_csrf()   ├───────────>│ 403 Forbidden │  (+ strike++ )
  │ Token match?    │            └───────────────┘
  └──────┬──────────┘
         │ Match
  ┌──────▼──────────────────────┐ True (rate exceeded)  ┌─────────────┐
  │ _check_mutation_rate(ip)?   ├─────────────────────> │ 429 Too Many│
  │ Within rate limit?          │                       │  Requests   │
  └──────┬──────────────────────┘                       └─────────────┘
         │ Pass
  ┌──────▼──────────────────────┐
  │ Execute Domain Function     │
  │ toggle_item / set_notes etc │
  └──────┬──────────────────────┘
         │ Success
  ┌──────▼──────────────────────┐
  │ _record_mutation()          │  INSERT INTO status_history 
  │ Capture pre/post values     │  (event_type, item_id, old_value,
  └──────┬──────────────────────┘   new_value, occurred UTC)
         │ DB Committed
  ┌──────▼──────────────────────┐
  │ broadcast_reload()          │  Flush "reload" event to all
  │ SSE subscriber queue        │  EventSource clients (app.js)
  └──────┬──────────────────────┘
         │ Complete
  ┌──────▼──────────────────────┐
  │ Return JSON {ok: True}      │
  └─────────────────────────────┘
```

---

## 5. Security Architecture

### 5.1 Threat Model

| Attack Vector | Mitigation | Location in Code |
|--------------|-----------|------------------|
| Brute-force credential guessing | `LOCKOUT_SECONDS * 2` lockout (60s) after 5 failed attempts per IP; random wait added for timing | `_check_mutation_rate()` + auth handler L~418-L456 |
| Session hijacking | Signed session cookies with `secure`, `httponly`, `samesite` flags; auto-rotated CSRF token | Flask session config, `make_session_cookie()` override |
| Cross-Site Request Forgery (CSRF) | Per-request secret token in hidden form field + header; 3-strike policy wipes entire session on mismatch | `_check_csrf()` L~596-L617, token injected via `<meta>` tag for CSP compliance |
| Query-string CSRF bypass | Token read from `form` or `request.body`, NOT from `request.args`; fallback checks `X-CSRF-Token` header only | `api_reorder()` at L580 uses `data.pop('csrf_token', '')` not `request.args.get(...)` |
| Cross-Site Scripting (XSS) | Zero `innerHTML` or template literal injection; all user/sensitive data set via `textContent`; DOM nodes created with `createElement()` | `app.js` throughout; confirmed in security commit `f67a5e1` |
| SQL Injection | Parameterized queries (`?` placeholders); no string interpolation into SQL strings | Throughout `app.py` SQLite operations |
| Plaintext password disclosure | Only scrypt hashes in config/env; never logged or returned in API responses | `_load_runtime()` + auth handlers |
| Configuration exposure | Critical sections (`_base.admin`) filtered from `_runtime.config.data`; `.gitignore` blocks `config.yaml` on install | `_parse_config()` L~130-L145, `_load_runtime()` merge logic |

### 5.2 Defense-in-Depth Layers

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
   └── Mutation rate limiting (_check_mutation_rate per IP)

Layer 4: Data Level (SQL Parameterization)
   └── All queries use ? placeholders; no string formatting

Layer 5: Persistence Level (Encrypted Hashes)
   └── werkzeug scrypt hashing with salt; raw password never stored or logged
```

### 5.3 Lockout Mechanism Details (Security-Critical — Covered by MC/DC D6 at line L~228)

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

*Document version: 2.0 | Last updated: 2026-08-13 | Author: Simar Sahni*
