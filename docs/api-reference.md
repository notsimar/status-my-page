# API Reference — Status My Page

## Overview

The Status My Page application exposes a RESTful JSON API alongside its rendered HTML interface. All CRUD operations for service management go through authenticated POST endpoints, while status display and history are publicly readable.

**Base URL:** `http://localhost:8920` (or your deployment host)
**Content-Type:** `application/json` for all POST requests
**Authentication:** HTTP session cookie + CSRF token header

---

## Public Endpoints (No Auth Required)

### GET `/` — Render Status Dashboard

Renders the full status page with service list, current states, and embedded CSRF token.

**Response:** `text/html; charset=utf-8`
**Status Codes:** `200 OK`

```html
<!-- Response contains: -->
- Jinja2-rendered HTML template
- Embedded <meta name="csrf-token" content="...">
- Inline CSS/JS bundles from /static/
- Service list with status icons (green/yellow/red)
```

### GET `/api/status` — Lightweight Public Status List

Public read of current statuses for auto-refresh polling. Returns
`id`/`name`/`status`/`notes` only — no history, no admin detail. Notes are
included so open note panes stay in sync for visitors.

**Response Body:**
```json
[
  { "id": 1, "name": "Primary Internet", "status": "green", "notes": "" },
  { "id": 2, "name": "Primary NAS",      "status": "yellow", "notes": "Investigating latency" }
]
```

**Status Codes:** `200 OK`

### GET `/api/csrf-token` — Fetch CSRF Token

Returns the per-session CSRF token. Required for all mutation endpoints after login.

**Request:** None
**Response Body:**
```json
{
  "token": "a1b2c3d4e5f6..."
}
```

**Status Codes:** `200 OK` | `403 Forbidden` (`X-Auth-Error: not-logged-in` — must be logged in)

### GET `/api/history/<item_id>` — View Change Timeline

Returns the complete mutation history for a specific service, ordered newest-first.

**Note:** Requires the history feature to be enabled (admin UI → Page Settings → 🕙 History button, or `settings.history_enabled: true` in `config.yaml`). Returns `404` while disabled.

**Request Parameters:**
- `item_id` (path): Integer ID of the service item

**Response Body:**
```json
{
  "service": "Primary NAS",
  "entries": [
    {
      "event_type": "status",
      "old_value": "green",
      "new_value": "yellow",
      "occurred": "2026-08-13T14:30:22.123456Z"
    },
    {
      "event_type": "notes",
      "old_value": "",
      "new_value": "Investigating latency spikes on us-east",
      "occurred": "2026-08-13T14:25:10.987654Z"
    }
  ]
}
```

**event_type values:** `status` | `notes` — status changes (admin toggles *and*
healthcheck flips) and note updates only; renames/adds/deletes are not recorded.
`occurred` is ISO-8601 UTC with microseconds. Each item keeps at most 100 rows
(`MAX_HISTORY_PER_ITEM`).

**Status Codes:** `200 OK` | `404 Not Found` (history disabled, or item_id doesn't exist)

### POST `/api/history/<item_id>/clear` — Clear a Service's History Timeline

🔒 Admin + CSRF. Deletes all `status_history` rows for one service and returns the count.

**Note:** Not gated by the history feature setting — an admin can wipe the timeline even while the public view is disabled (the 🧹 button only renders when it's enabled).

**Request:** No body.

**Response Body:**
```json
{ "ok": true, "removed": 42 }
```

**Status Codes:** `200 OK` | `404 Not Found` (item_id doesn't exist)

### GET `/feed.xml` (alias: `/rss`) — Status RSS Feed

Public read. Returns an RSS 2.0 feed of status changes, generated **on demand** from `status_history` so `lastBuildDate` and the newest `<item>` advance the instant a status changes (admin toggle **or** an automatic healthcheck flip). Only status-change events are surfaced — notes/renames are filtered out. Configurable via the `rss:` config section (`enabled`, `title`, `max_items`).

**Request:** None
**Response:** `application/rss+xml` document
```xml
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Application Status</title>
    <link>http://localhost:8920/</link>
    <description>Status change timeline</description>
    <lastBuildDate>Mon, 17 Aug 2026 12:00:00 -0000</lastBuildDate>
    <item>
      <title>Slack: Operational → Degraded</title>
      <description>Slack status changed from Operational to Degraded</description>
      <pubDate>Mon, 17 Aug 2026 11:59:40 -0000</pubDate>
    </item>
  </channel>
</rss>
```
**Headers:** `Cache-Control: no-cache, no-store, must-revalidate`

**Status Codes:** `200 OK` | `404 Not Found` (feed disabled via `rss: {enabled: false}`)

### GET `/api/healthchecks` — List Configured Healthchecks

Public read, but **redacted** for non-admins: probe targets (url, host, port,
soap_action) and keyword lists reveal internal network topology, so visitors
see only `name`, `type`, `interval`, `timeout`, `retries`, and optional
`service`/`healthy_codes`. Logged-in admins get the full configuration.

**Request:** None
**Response Body (non-admin):**
```json
{
  "Google workspace": {
    "type": "rss",
    "interval": 60,
    "timeout": 30,
    "retries": 2
  }
}
```

**Status Codes:** `200 OK` (empty object `{}` when healthchecks are disabled via `STATUS_DISABLE_HEALTHCHECKS`)

### GET `/api/rss` — Feed Metadata

Public read. Returns feed availability + metadata so the UI can render the feed link and the admin panel can show current state without a second round trip.

**Request:** None
**Response Body:**
```json
{
  "enabled": true,
  "title": "Application Status",
  "max_items": 50,
  "url": "http://localhost:8920/feed.xml"
}
```

**Status Codes:** `200 OK`

### GET `/auth-check` — Session Validation

Checks if the current session is authenticated as admin. Useful for UI to show/hide admin controls.

**Request:** None
**Response Body:**
```json
{
  "admin": true,
  "user": "admin"
}
```

or:

```json
{
  "reason": "not_logged_in"
}
```

**Status Codes:** `200 OK` (always succeeds, value depends on auth state)

---

## Authenticated Endpoints (Admin + CSRF Required)

All mutation endpoints enforce a **three-layer security gate** (applied by the
`require_admin()` decorator in `statuspage/auth.py`):
1. `session.get("admin")` — Must be authenticated as admin
2. `check_csrf()` — Valid X-CSRF-Token header matching session token
3. `check_mutation_rate(ip)` — Max **60** mutations per IP in a 60-second window (`MUTATION_MAX`/`MUTATION_WINDOW`)

All three failure modes return **`403 Forbidden`** — deliberately uniform so the
server never reveals which guard tripped. The failed guard is distinguished for
the frontend via the **`X-Auth-Error` response header**:

| `X-Auth-Error` | Meaning | Recommended client behavior |
|---|---|---|
| `not-logged-in` | Session expired/missing | Log out + reload the page |
| `csrf` | Token mismatch or malformed | Show inline error; refetch the page for a fresh token |
| `rate-limited` | Mutation rate limit exceeded | Back off and retry |

(3 failed CSRF attempts also wipe the entire session.)

### POST `/login` — Authenticate User

Establishes a signed Flask session cookie on successful authentication.

**Request Body:**
```json
{
  "user": "admin",
  "pass": "your-plaintext-password"
}
```

> **Note:** Send the **plaintext password** — the server verifies it against the
> stored scrypt hash with `werkzeug.security.check_password_hash()`. The hash
> itself lives only in `.env.local` / the env file (`STATUS_ADMIN_PASS_HASH`);
> it is never sent by the client. Username check is timing-safe (no user
> enumeration).

**Response on Success:** `{ "ok": true }` + session cookie in `Set-Cookie` header
(session is regenerated clean; a 5-minute sliding idle clock starts).

**Status Codes:** `200 OK` (session created) | `401 Unauthorized` (bad credentials) | `429 Too Many Requests` (login rate-limited, includes `retry_after` seconds)

### POST `/logout` — Clear Session

Invalidates the current session cookie.

**Request:** None
**Response:** Empty body with `Set-Cookie` clearing the session

**Status Codes:** `200 OK`

### POST `/api/toggle/<item_id>` — Cycle Status Status

Cycles a service's status: green → yellow → red → green on each call.

**Request Parameters:**
- `item_id` (path): Integer ID of the service

**Request Body:** Empty or `{}`

**Response Body:**
```json
{
  "status": "yellow"
}
```

Where status values: `green` | `yellow` | `red`

**Status Codes:** `200 OK` | `403 Forbidden` (uniform for all guard failures — see [Error Responses](#error-responses)) | `429 Too Many Requests`

### POST `/api/notes/<item_id>` — Update Notes Text

Saves or updates freeform note text for a specific service. The change is recorded in `status_history` and appears in the public `/feed.xml` on the next feed fetch.

**Request Parameters:**
- `item_id` (path): Integer ID of the service

**Request Body:**
```json
{
  "notes": "Investigating latency spikes on us-east region"
}
```

**Response Body:**
```json
{
  "ok": true
}
```

**Status Codes:** `200 OK` | `403 Forbidden` (uniform for all guard failures — see [Error Responses](#error-responses)) | `429 Too Many Requests`

### POST `/api/add` — Create New Service Item

Adds a new service to the dashboard.

**Request Body:**
```json
{
  "name": "NewService"
}
```

**Response Body:**
```json
{
  "item": {
    "id": 7,
    "name": "NewService",
    "status": "green",
    "notes": "",
    "position": 6
  }
}
```

The new item is inserted at the next available position and history entry recorded.

**Status Codes:** `200 OK` | `403 Forbidden` (uniform for all guard failures — see [Error Responses](#error-responses)) | `429 Too Many Requests` | `400 Bad Request` (missing name)

### POST `/api/delete/<item_id>` — Remove Service Item

Permanently deletes a service item and its associated history entries. Compacts positions of remaining items.

**Request Parameters:**
- `item_id` (path): Integer ID of the service to delete

**Request Body:** Empty or `{}`

**Response Body:**
```json
{
  "ok": true
}
```

**Status Codes:** `200 OK` | `403 Forbidden` (uniform for all guard failures — see [Error Responses](#error-responses)) | `429 Too Many Requests` | `404 Not Found`

### POST `/api/rename/<item_id>` — Update Service Name

Renames an existing service item in the database and records the event in status history.

**Request Parameters:**
- `item_id` (path): Integer ID of the service to rename

**Request Body:**
```json
{
  "name": "Slack"
}
```

**Response Body:**
```json
{
  "ok": true
}
```

**Status Codes:** `200 OK` | `403 Forbidden` (uniform for all guard failures — see [Error Responses](#error-responses)) | `429 Too Many Requests` | `400 Bad Request` (missing name)

### POST `/api/reorder` — Apply Drag-Drop Position Map

Reorders service items based on the frontend drag-and-drop state. Accepts a JSON map of item_id to position.

**Request Body:**
```json
{
  "reorder": {
    "1": 0,
    "2": 1,
    "3": 2
  }
}
```

Where keys are item IDs (strings) and values are zero-based integer positions.

**Response Body:**
```json
{
  "ok": true
}
```

**Status Codes:** `200 OK` | `403 Forbidden` (uniform for all guard failures — see [Error Responses](#error-responses)) | `429 Too Many Requests`

### POST `/api/healthchecks` — Create Healthcheck

Registers a background healthcheck for a service. `type` is one of `curl` (default), `ping`, `tcp`, `soap`, `rss`; when omitted it is auto-detected from the payload (`host`+`port`→`tcp`, `host`→`ping`, `url`→`curl`, `soap_action`/`body`→`soap`) — a bare `url` is never treated as `rss`. Numeric fields are bound-checked (`interval` 1–3600, `timeout` 1–300, `retries` 1–10).

**Request Body (rss example):**
```json
{
  "name": "Google workspace",
  "type": "rss",
  "url": "https://www.google.com/appsstatus/dashboard/en/feed.atom",
  "keywords": {
    "red": ["major issue", "major outage", "ongoing"],
    "degraded": ["partial", "minor", "investigating", "experiencing issues"]
  },
  "interval": 60,
  "timeout": 10,
  "retries": 2
}
```
Other types: `curl`/`soap` take `url` (+ `soap_action`/`body`/`expected_string`/`healthy_codes` for soap); `ping`/`tcp` take `host` (+ `port` for tcp).

**Response Body:**
```json
{ "ok": true, "name": "Google workspace", "config": { "...": "full stored config" } }
```
**Status Codes:** `200 OK` | `400 Bad Request` (invalid type/url/host/keywords/empty target) | `409 Conflict` (name already exists) | `403`/`429`

### PUT `/api/healthchecks/<name>` — Update Healthcheck

Partial-field merge by service name. **A `type` change is a full-replace** — old-type fields are dropped and only `interval`/`timeout`/`retries` survive the swap. Absent fields keep their current value.

**Request Body:** any subset of create fields (e.g. `{"keywords": {...}}`, `{"timeout": 20}`).
**Response Body:** `{ "ok": true, "name": "...", "config": { "...": "merged config" } }`
**Status Codes:** `200 OK` | `400 Bad Request` | `404 Not Found` (name unknown) | `403`/`429`

### DELETE `/api/healthchecks/<name>` — Remove Healthcheck
**Response Body:** `{ "ok": true }`
**Status Codes:** `200 OK` | `404 Not Found` | `403`/`429`

### POST `/api/healthcheck/run` — One-Shot Healthcheck Run

Runs every configured check **once** immediately (no worker, no persistence side-effects) and returns the live result. Useful for verifying a new config without waiting a full interval.

**Request:** None
**Response Body:**
```json
{
  "Google workspace": {
    "type": "rss",
    "url": "...",
    "status_code": 200,
    "result": "green",
    "healthy": true
  }
}
```
**Status Codes:** `200 OK` | `403`/`429`

### POST `/api/rss` — Toggle Status Feed

Enables/disables the public `/feed.xml` status feed. Persists to `config.yaml` `rss: {enabled: ...}` while preserving other rss keys.

**Request Body:** `{ "enabled": true }`
**Response Body:** `{ "ok": true, "enabled": true }`
**Status Codes:** `200 OK` | `400 Bad Request` (missing/non-bool `enabled`) | `403`/`429`

### GET `/api/export/static` — Export Standalone Static Page

Generates and downloads a self-contained static HTML file with inlined styles and the current service status state, designed for static hosting (S3/CDN/Nginx) for mass delivery.

**Query Parameters:**
- `download` (optional, default `true`): If `true`, adds `Content-Disposition: attachment; filename="status.html"`.

**Response Body:** Full standalone HTML document (`text/html; charset=utf-8`).
**Status Codes:** `200 OK` | `403 Forbidden` (admin gate; `X-Auth-Error` distinguishes `not-logged-in` from CSRF)

---

## Error Responses

All error responses return JSON:

```json
{
  "error": "message describing the failure"
}
```

| Status Code             | Meaning                    | Typical Cause                              |
|-------------------------|----------------------------|--------------------------------------------|
| `400 Bad Request`       | Malformed incoming request | Missing required field, bad JSON, rejected by input validation (`input_filter`) |
| `401 Unauthorized`      | Not authenticated          | Bad credentials on `POST /login` |
| `403 Forbidden`         | Authenticated but denied / admin gate failed | Admin + CSRF mutations: `X-Auth-Error` header says which guard (`not-logged-in` / `csrf` / `rate-limited`) |
| `404 Not Found`         | Resource does not exist    | Invalid item_id, history disabled, endpoint disabled |
| `409 Conflict`          | Duplicate resource         | Healthcheck name already exists |
| `429 Too Many Requests` | Rate limit exceeded        | Login lockout (5 failed logins in 30s window) |

---

## Quick Start Examples

### View Status Page
```bash
curl http://localhost:8920/ | head -50
```

### Login and Toggle Service
```bash
# 1. Login to get session cookie (send the plain password — the server checks it against the stored hash)
curl -c cookies.txt -b cookies.txt -s -X POST http://localhost:8920/login \
  -H "Content-Type: application/json" \
  -d '{"user":"admin","pass":"your-plaintext-password"}'

# 2. Get CSRF token from /api/csrf-token (note the field is "token")
TOKEN=$(curl -c cookies.txt -b cookies.txt -s http://localhost:8920/api/csrf-token | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")

# 3. Toggle service #1 with CSRF header
curl -b cookies.txt -s -X POST http://localhost:8920/api/toggle/1 \
  -H "X-CSRF-Token: $TOKEN" \
  -H "Content-Type: application/json"
```

### Check History
```bash
# Returns 404 while history is disabled (default)
curl http://localhost:8920/api/history/1 | python3 -m json.tool
```

---

## Page Settings

### GET `/api/settings` — Read Page Settings

Public read of the current page-level settings.

**Response Body:**
```json
{
  "history_enabled": false,
  "healthchecks_enabled": true
}
```

### POST `/api/settings` — Update Page Settings

🔒 Admin + CSRF. Update a page-level setting. Persists to `config.yaml` `settings:`.

**Request Body:**
```json
{
  "history_enabled": true,
  "healthchecks_enabled": false
}
```
*(Accepts either or both fields in a single payload)*

**Response Body:**
```json
{
  "ok": true,
  "history_enabled": true,
  "healthchecks_enabled": false
}
```

**Status Codes:** `200 OK` | `400 Bad Request` (neither field present or value not a boolean)


---

## Slack Notifications

### `GET /api/slack` (admin only)

Returns the current Slack integration state.

```json
{
  "enabled": true,
  "configured": true,
  "webhook_masked": "https://hooks.slack.com/services/…",
  "queued": 3,
  "channel": "#ops"
}
```

The webhook token is never returned — only a masked form ending in `…`.

### `POST /api/slack` (admin + CSRF)

Body fields (all optional):

| Field | Type | Effect |
|-------|------|--------|
| `enabled` | bool | Toggle the integration |
| `webhook_url` | string | Set the incoming-webhook URL (`https://` required) |
| `channel` | string | Optional `#channel`/`@user` override |
| `clear_queue` | true | Drop all queued changes without sending |

Response: same shape as GET plus `"ok": true`. Validation errors return
400 with a message; the full webhook token is never echoed back.

**Delivery model:** status changes queue to a persistent outbox as they
happen. When the admin logs out, ONE digest message posts via the
incoming webhook. Failed deliveries keep the queue for the next logout.

---

*Document version: 1.4 | Last updated: 2026-08-27 | Author: Simar Sahni*

