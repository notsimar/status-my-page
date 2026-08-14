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

### GET `/api/csrf-token` — Fetch CSRF Token

Returns a fresh per-request CSRF token. Required for all mutation endpoints after login.

**Request:** None
**Response Body:**
```json
{
  "csrf_token": "a1b2c3d4e5f6..."
}
```

**Status Codes:** `200 OK` | `401 Unauthorized` (must be logged in)

### GET `/api/history/<item_id>` — View Change Timeline

Returns the complete mutation history for a specific service, ordered newest-first.

**Request Parameters:**
- `item_id` (path): Integer ID of the service item

**Response Body:**
```json
{
  "history": [
    {
      "event_type": "status_toggle",
      "item_id": 3,
      "old_value": "green",
      "new_value": "yellow",
      "occurred": "2026-08-13T14:30:22Z"
    },
    {
      "event_type": "note_update",
      "item_id": 3,
      "old_value": "",
      "new_value": "Investigating latency spikes on us-east",
      "occurred": "2026-08-13T14:25:10Z"
    }
  ]
}
```

**event_type values:** `status_toggle` | `note_update` | `name_change` | `item_added` | `item_deleted`

**Status Codes:** `200 OK` | `404 Not Found` (item_id doesn't exist)

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

All mutation endpoints enforce a **three-layer security gate**:
1. _not_admin() — Must be authenticated as admin
2. _check_csrf() — Valid X-CSRF-Token header matching session
3. _check_mutation_rate(ip) — Max 5 mutations per IP in 60-second window

Failure at any layer returns `403 Forbidden`. Exceeding rate limits returns `429 Too Many Requests`.

### POST `/login` — Authenticate User

Establishes a signed Flask session cookie on successful authentication.

**Request Body:**
```json
{
  "user": "admin",
  "pass": "your-hashed-password-value"
}
```

> **Important:** The `pass` field must contain the werkzeug scrypt hash (not plaintext). Generate it with:
> ```bash
> python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('my-secure-pw'))"
> ```

**Response on Success:** Session cookie set in `Set-Cookie` header. No response body guaranteed.

**Status Codes:** `200 OK` (session created) | `401 Unauthorized` (bad credentials) | `403 Forbidden` (rate-limited)

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

**Status Codes:** `200 OK` | `401 Unauthorized` | `403 Forbidden` | `429 Too Many Requests`

### POST `/api/notes/<item_id>` — Update Notes Text

Saves or updates freeform notes for a specific service. Triggers real-time SSE broadcast to all connected browsers.

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

**Status Codes:** `200 OK` | `401 Unauthorized` | `403 Forbidden` | `429 Too Many Requests`

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

**Status Codes:** `200 OK` | `401 Unauthorized` | `403 Forbidden` | `429 Too Many Requests` | `400 Bad Request` (missing name)

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

**Status Codes:** `200 OK` | `401 Unauthorized` | `403 Forbidden` | `429 Too Many Requests` | `404 Not Found`

### POST `/api/rename/<item_id>` — Update Service Name

Renames an existing service item. Updates references in _runtime config and history.

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

**Status Codes:** `200 OK` | `401 Unauthorized` | `403 Forbidden` | `429 Too Many Requests` | `400 Bad Request` (missing name)

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

**Status Codes:** `200 OK` | `401 Unauthorized` | `403 Forbidden` | `429 Too Many Requests`

---

## SSE Endpoint (Real-Time Broadcast)

### GET `/events` — Server-Sent Events Stream

Establishes an EventSource connection for receiving real-time reload notifications. All mutation endpoints call `broadcast_reload()` which flushes a `{event: 'reload', data: ''}` message to every subscriber.

**Request:** None
**Response:** Text/event-stream with continuous SSE frames

```
event: reload

event: reload

... (sends event on admin mutation, client auto-refreshes UI)
```

**Client Integration (app.js):**
```javascript
const source = new EventSource('/events');
source.addEventListener('reload', () => {
  location.reload();  // or fetch fresh data and re-render
});
source.onerror = () => source.close();
```

**Status Codes:** `200 OK` with `text/event-stream` content type | Connection stays open indefinitely until client disconnects

---

## Error Responses

All error responses return JSON:

```json
{
  "error": "message describing the failure"
}
```

| Status Code | Meaning | Typical Cause |
|-------------|---------|---------------|
| `400 Bad Request` | Malformed incoming request | Missing required field, bad JSON |
| `401 Unauthorized` | Not authenticated | No valid session cookie |
| `403 Forbidden` | Authenticated but denied | Wrong CSRF token, insufficient permissions |
| `429 Too Many Requests` | Rate limit exceeded | More than 5 mutations per IP in 60 seconds |
| `404 Not Found` | Resource does not exist | Invalid item_id |

---

## Quick Start Examples

### View Status Page
```bash
curl http://localhost:8920/ | head -50
```

### Login and Toggle Service
```bash
# 1. Login to get session cookie
curl -c cookies.txt -b cookies.txt -s -X POST http://localhost:8920/login \
  -H "Content-Type: application/json" \
  -d '{"user":"admin","pass":"scrypt$72816$..."}'

# 2. Get CSRF token from the session cookie or /api/csrf-token
TOKEN=$(curl -c cookies.txt -b cookies.txt http://localhost:8920/api/csrf-token | python3 -c "import sys,json; print(json.load(sys.stdin)['csrf_token'])")

# 3. Toggle service #1 with CSRF header
curl -b cookies.txt -s -X POST http://localhost:8920/api/toggle/1 \
  -H "X-CSRF-Token: $TOKEN" \
  -H "Content-Type: application/json"
```

### Check History
```bash
curl http://localhost:8920/api/history/1 | python3 -m json.tool
```

---

*Document version: 1.0 | Last updated: 2026-08-13 | Author: Simar Sahni*
