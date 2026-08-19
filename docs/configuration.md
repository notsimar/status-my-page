# Configuration Reference

## Table of Contents

- [1. config.yaml Structure](#1-configyaml-structure)
- [2. Environment Variables](#2-environment-variables)
- [3. Security Credentials](#3-security-credentials)
- [4. State Management & Persistence](#4-state-management--persistence)
- [5. Backup Files](#5-backup-files)
- [6. Config Migrations](#6-config-migrations)

---

## 1. config.yaml Structure

Full structure with defaults and descriptions:

```yaml
# ── Service definitions ───────────────────────────────────────
# Each entry becomes a monitored service card on the dashboard.
items:
  - Slack                    # Display name shown to users
  - Glean                    # Supports any string value
  - Azure
  - ServiceNow

# ── Admin credentials ─────────────────────────────────────────
admin:
  user: admin                # Admin username (overridable via STATUS_ADMIN_USER env)
  # NOTE: Never store plaintext passwords here. 
  # The STATUS_ADMIN_PASS_HASH environment variable is REQUIRED (no fallback).

# ── Server settings ────────────────────────────────────────────
server:
  host: "0.0.0.0"            # Bind address (use "127.0.0.1" for localhost-only)
  port: 8920                 # Listening port
  secret_key_env: STATUS_SECRET_KEY  # Env var name containing Flask session signing key

# ── Page settings ─────────────────────────────────────────────
# Optional. History is OFF by default — opt in per-deployment.
settings:
  history_enabled: false   # Show per-service 🕙 history button + /api/history
```

### Field Details

#### `items` (required)
- **Type:** Array of strings
- **Purpose:** Read-only user input defining services to monitor and add to the configuration.
- **Constraints:** Each name must be unique.
- **Lifecycle:** On startup / `init_db()`, newly added items from `config.yaml` are seeded into SQLite without deleting or overriding existing items in the database. The database serves as the single source of truth for items and their state.

#### `admin.user` (required)
- **Type:** String
- **Purpose:** Admin username for authentication
- **Override:** Can be overridden at runtime via `STATUS_ADMIN_USER` environment variable
- **Default:** `"admin"`

#### `server.host` (optional, default: `"0.0.0.0"`)
- **Type:** String (IP address)
- **Purpose:** Network interface to bind Flask server to
- **Recommendations:**
  - `0.0.0.0` — Accept connections from all interfaces (for production behind reverse proxy)
  - `127.0.0.1` — Localhost only (for development or direct access)
  - Specific IP — Restrict to a particular network interface

#### `server.port` (optional, default: `8920`)
- **Type:** Integer
- **Purpose:** Port number for the Flask server
- **Constraints:** Must be an available port ≥1024 if privileged; typically 1024–65535
- **Override:** Can be overridden by any server configuration passed to `install.sh`

#### `server.secret_key_env` (optional, default: `"STATUS_SECRET_KEY"`)
- **Type:** String
- **Purpose:** Environment variable name whose value is used as the Flask session signing key (via `app.config['SECRET_KEY']`). If unset, Flask generates a random key on startup (keys don't survive restarts — not recommended).
- **Recommendation:** Always set `STATUS_SECRET_KEY` to a cryptographically random string of ≥32 characters.

#### `settings.*` (optional)
- **Type:** Boolean flags
- **Purpose:** Page-level feature toggles. Edited at runtime via the admin UI
  (Page Settings → 🕙 History button), which persists back to this section
  atomically (backup rotation).
- Currently exposed flags:

| Key | Purpose | Default |
|---|---|---|
| `history_enabled` | Show the per-service history button + make `GET /api/history/<id>` available | `false` |

#### `rss` (optional)
- **Type:** Dictionary
- **Purpose:** Controls the public status RSS feed at `GET /feed.xml`.
- **Keys:**

| Key | Description | Default |
|---|---|---|
| `enabled` | Toggle the feed endpoint | `true` |
| `title` | Feed `<title>` (max 64 chars) | `Application Status` |
| `max_items` | Max history entries in the feed | `50` |

The feed is generated on demand from `status_history` status-change rows
(admin toggles and healthcheck flips both qualify), so `lastBuildDate` and the
newest `<item>` always advance the instant a status changes.

#### `healthchecks` (optional)
- **Type:** Dictionary keyed by service name
- **Purpose:** Automated background health checking via HTTP (`curl`), ICMP (`ping`), TCP, SOAP endpoints, or vendor RSS/Atom status feeds.
- **Supported types:**
  - `curl` (default): Performs an HTTP/HTTPS GET request using `curl`.
  - `ping` / `icmp`: Performs ICMP ping check using `ping -c 1`.
  - `tcp`: TCP port connectivity check (`host` + `port`).
  - `soap`: POSTs a SOAP XML payload via `curl` and optionally validates the response body against an expected string.
  - `rss`: Fetches an RSS/Atom status feed and maps entry keywords to status (see below). **Never auto-detected** — a bare `url` always means `curl`; set `type: rss` explicitly.

**Example:**
```yaml
healthchecks:
  Web Server:
    type: curl
    url: http://localhost:8920/
    interval: 30
    timeout: 5
    healthy_codes: [200, 204]
    retries: 2
  Gateway Router:
    type: ping
    host: 192.168.10.1
    interval: 15
    timeout: 2
    retries: 2
  Legacy API:
    type: soap
    url: https://api.example.com/webservice.asmx
    soap_action: "http://tempuri.org/GetStatus"
    body: "<ns:GetStatus xmlns:ns='http://tempuri.org/'/>"
    expected_string: "<return>OK</return>"
    healthy_codes: [200]
    interval: 60
    timeout: 15
    retries: 3
```

**HTTP (curl) options:**
| Key | Description | Default |
|---|---|---|
| `url` | REQUIRED; the HTTP/HTTPS endpoint URL (http/https only) | — |
| `healthy_codes` | List of HTTP status codes considered healthy | `[200]` |
| `failure_keyword` | String that flags OUTAGE/RED if present in the response body (case-insensitive) | *(none)* |
| `degraded_keyword` | String that flags DEGRADED/YELLOW if present in the response body (case-insensitive) | *(none)* |
| `service` | Optional target dashboard service to update (defaults to check name if omitted) | *(check name)* |

**SOAP options:**
| Key | Description | Default |
|---|---|---|
| `url` | REQUIRED; the SOAP endpoint URL (http/https only) | — |
| `soap_action` | The `SOAPAction` header value | *(optional)* |
| `body` | Custom XML payload to POST | Minimal SOAP envelope with empty `<Body/>` |
| `expected_string` | String that must appear in the response body for healthy status | *(none)* |
| `failure_keyword` | String that flags OUTAGE/RED if present in the response body (case-insensitive) | *(none)* |
| `degraded_keyword` | String that flags DEGRADED/YELLOW if present in the response body (case-insensitive) | *(none)* |
| `service` | Optional target dashboard service to update (defaults to check name if omitted) | *(check name)* |

**RSS feed options:**

| Key | Description | Default |
|---|---|---|
| `url` | REQUIRED; the RSS/Atom feed URL (http/https only) | — |
| `keywords.red` | Marker phrases that flip the item **red immediately** (no retry ladder) | *(empty in YAML; admin panel create applies `outage`, `down`, `major issue`, `critical` when omitted)* |
| `keywords.degraded` | Marker phrases that flip the item **degraded** | *(empty in YAML; admin panel create applies `degraded`, `partial`, `minor`, `investigating` when omitted)* |
| `interval` | Seconds between feed polls | `60` |
| `timeout` | Fetch timeout in seconds | `10` |
| `retries` | Consecutive **fetch-failure** retries before degraded/red | `2` |

The worker fetches the feed (`curl`), parses it with stdlib ElementTree
(first 20 entries, 512 KB body cap, namespace-agnostic `<item>`/`<entry>`),
and scans entry titles + descriptions/summaries case-insensitively for the
keywords. Precedence: any `red` keyword → red; else any `degraded` keyword →
degraded; clean feed → green. If the feed itself can't be fetched, the normal
retry ladder applies (degraded → red). With no keywords configured, only fetch
failures change status.

**Example:**
```yaml
healthchecks:
  Google Workspace:
    type: rss
    url: https://www.google.com/appsstatus/dashboard/en/feed.atom
    keywords:
      red: [outage, major issue]
      degraded: [degraded, minor, investigating]
    interval: 60
    timeout: 30
    retries: 2
```

> Use the vendor's **feed URL**, not the dashboard page URL. A plain HTML page parses as "not well-formed XML" and reads as a fetch failure every poll, not as "all clear".

---

## 2. Environment Variables

| Variable | Purpose | Required | Example | Notes |
|----------|---------|----------|---------|-------|
| `STATUS_ADMIN_USER` | Override admin username from config.yaml | No | `john` | Takes precedence over `admin.user` in config |
| `STATUS_ADMIN_PASS_HASH` | Password hash for Flask auth (REQUIRED for production) | Yes | `scrypt$72816$salt...` | **Never** store plaintext passwords |
| `STATUS_SECRET_KEY` | Flask session signing key | Recommended | `a1b2c3d4e5f6...` | Auto-generated if unset but doesn't survive restarts |
| `STATUS_NO_ARCHIVE=1` | Disable DB archival on server restart | No | (any value) | Use only for development/testing |

### Generating Values

**Password hash:**
```bash
python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('your-secure-password'))"
# Output: scrypt$72816$... (copy this entire string)
```

**Secret key:**
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
# Output: a1b2c3d4e5f6... (64 hex characters)
```

### Setting Environment Variables

For production deployments via `install.sh`, credentials are stored in `/etc/status-page/env`:
```bash
sudo tee /etc/status-page/env > /dev/null <<EOF
STATUS_ADMIN_PASS_HASH=scrypt$72816$salt...
STATUS_ADMIN_USER=admin
STATUS_SECRET_KEY=a1b2c3d4e5f6...
PYTHONUNBUFFERED=1
EOF
sudo chmod 0640 /etc/status-page/env
```

For manual systemd setups, reference this file in the service definition via `EnvironmentFile=`. For development, export directly:
```bash
export STATUS_ADMIN_PASS_HASH="scrypt$72816$salt..."
export STATUS_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
./start.sh
```

---

## 3. Security Credentials

### Password Storage Format

Passwords are stored exclusively as werkzeug scrypt hashes:
```
scrypt:32768:8:<salt>:<hash>
```

- **Algorithm:** scrypt (FIPS 140-2 compliant)
- **Parameters:** Default CPU/work factor is set by werkzeug; adjust via `werkzeug.security.generate_password_hash(password)` parameters if needed
- **Comparison:** Timing-safe comparison via `werkzeug.security.check_password_hash()` prevents side-channel attacks

### Secret Key Requirements

The Flask `SECRET_KEY` signs session cookies cryptographically. Requirements:
- Minimum 32 bytes (256 bits) of entropy
- Must be a hex string or base64-encoded raw bytes
- Should **never** be hardcoded in config.yaml or committed to git
- Use `python3 -c "import secrets; print(secrets.token_hex(32))"` to generate

If the key is regenerated (e.g., after server restart and no persistent value), all existing sessions become invalid immediately.

---

## 4. State Management & Persistence

State is maintained exclusively within SQLite (`instance/status.db`). `config.yaml` is strictly a read-only input file for provisioning service items, server settings, and initial healthcheck definitions.

### How State Works

1. **Database as Single Source of Truth:** All service items, current statuses (`green`, `degraded`, `red`), notes, positions, and mutation history live directly in SQLite.
2. **Seeding:** On startup, `init_db()` reads `items` from `config.yaml` and adds any services that do not yet exist in the database. Existing items in SQLite and their state are preserved.
3. **Admin Mutations:** All UI mutations (status toggles, notes, reordering, adding, deleting items) perform direct ACID operations against SQLite.

---

## 5. Backup Files

The application maintains a rotation of previous `config.yaml` versions when healthcheck definitions are modified through the admin API:

| File | Purpose | Retention |
|------|---------|-----------|
| `config.yaml` | Current configuration | Always newest |
| `config.yaml.bak1` | Most recent backup | Auto-created before each healthcheck save |
| `config.yaml.bak2` | Second most recent | Shifted up from `.bak1` on next save |
| `config.yaml.bak3` | Third-most recent | Shifted up from `.bak2` |
| `config.yaml.bak4` | Fourth-most recent | — |
| `config.yaml.bak5` | Oldest preserved backup | Deleted when `.bak6` would be created |

### Rotation Logic

```
Before each mutation:
1. config.yaml → config.yaml.bak1 (overwrites old .bak1)
2. Old .bak1 → .bak2 (if exists)
3. ... up to .bak5
4. If .bak6 would exist → delete it (keep max 5 backups)
```

### Recovery Example

```bash
# Restore from most recent backup
cp config.yaml.bak1 config.yaml
./restart.sh

# View available archives and restore specific one
python3 -m json.tool archives/20260813_091523.json | head -40
```

Backup files are excluded from git via `.gitignore` to prevent credential/history leakage.

---

## 6. Config Migrations

### Adding a New Feature Flag

When extending `config.yaml` with new top-level sections or feature toggles, update the `_parse_config()` function in `app.py` to:
1. Validate the new section exists and is a dict/array (not None)
2. Default to sensible fallbacks if missing
3. Filter from runtime config data (if metadata-only)

### Changing DB Schema

SQLite migrations are handled automatically via `init_db()`:
```python
def init_db():
    with _db_lock:
        db = get_db()
        db.execute('''CREATE TABLE IF NOT EXISTS status_items (...)''')
        db.execute('''CREATE TABLE IF NOT EXISTS status_history (...)''')
        db.commit()
```

No explicit migration scripts needed — `CREATE TABLE IF NOT EXISTS` is idempotent. For actual schema changes (column additions), use `ALTER TABLE column_name ADD COLUMN new_col TYPE` within the same function before first query execution.

### Breaking Changes Checklist

Before modifying config.yaml structure:
1. Run `pytest tests/ -v` to verify existing tests still pass
2. Update `_parse_config()` validation logic if adding/changing top-level sections
3. Document migration path in release notes
4. Consider versioning the schema if backward compatibility is needed

---

*Document version: 1.2 | Last updated: 2026-08-18 | Author: Simar Sahni*
