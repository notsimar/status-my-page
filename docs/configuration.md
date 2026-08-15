# Configuration Reference

## Table of Contents

- [1. config.yaml Structure](#1-configyaml-structure)
- [2. Environment Variables](#2-environment-variables)
- [3. Security Credentials](#3-security-credentials)
- [4. Runtime State (Auto-Populated)](#4-runtime-state-auto-populated)
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

# ── Feature toggles ────────────────────────────────────────────
features:
  drag_drop_reorder: true    # Enable drag-and-drop item reordering UI
  notes_enabled: true        # Enable per-service notes functionality
  history_enabled: true      # Enable status history timeline feature
```

### Field Details

#### `items` (required)
- **Type:** Array of strings
- **Purpose:** Defines the list of services to monitor
- **Constraints:** Each name must be unique; empty array results in empty dashboard
- **Lifecycle:** Changes to this list on server restart will:
  - Add any new names as new DB rows (auto-seeded, position = current_count)
  - Remove any deleted names from the display (items stay in DB but are filtered out by `_load_runtime`)
  - Preserve existing status/notes for items that remain

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

#### `features.*` (optional, all default: `true`)
- **Type:** Boolean flags
- **Purpose:** Feature toggles for optional functionality
- Currently exposed flags: `drag_drop_reorder`, `notes_enabled`, `history_enabled`. Future versions may add more.

#### `healthchecks` (optional)
- **Type:** Dictionary keyed by service name
- **Purpose:** Automated background health checking via HTTP (`curl`), ICMP (`ping`), or SOAP endpoints.
- **Supported types:**
  - `curl` (default): Performs an HTTP/HTTPS GET request using `curl`.
  - `ping` / `icmp`: Performs ICMP ping check using `ping -c 1`.
  - `soap`: POSTs a SOAP XML payload via `curl` and optionally validates the response body against an expected string.

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

**SOAP options:**
| Key | Description | Default |
|---|---|---|
| `url` | REQUIRED; the SOAP endpoint URL (http/https only) | — |
| `soap_action` | The `SOAPAction` header value | *(optional)* |
| `body` | Custom XML payload to POST | Minimal SOAP envelope with empty `<Body/>` |
| `expected_string` | String that must appear in the response body for healthy status | *(none — 200 is enough)* |

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

## 4. Runtime State (Auto-Populated)

On every admin mutation (toggle, rename, notes, add, delete, reorder), the application automatically writes the current runtime state back to `config.yaml` under a `_runtime` section:

```yaml
_runtime:
  status:
    SvcA: degraded        # Current status per item name
    SvcB: green
  notes:                  # Per-item freeform notes (keys = item names)
    SvcA: "Investigating latency spikes"
  config:
    data:
      items:              # Synced list of current service names
        - Slack
        - Azure
```

### How Runtime State Works

1. **Persistence:** On mutation, app serializes `_runtime.config.data`, `_runtime.status`, and `_runtime.notes` to YAML
2. **Restoration:** On server restart, `config.yaml` `_runtime` entries are loaded into memory and merged over DB seed data (runtime values take precedence)
3. **Isolation:** Keys starting with `_` (`_base`, `_runtime`) are filtered from the public config dict so they never appear in API responses or templates

This design ensures that runtime changes (status updates, notes, reorder) survive:
- Application restarts
- Database drops and re-seeding
- Backup/restore operations

---

## 5. Backup Files

The application maintains a rotation of previous `config.yaml` versions to protect against accidental data loss:

| File | Purpose | Retention |
|------|---------|-----------|
| `config.yaml` | Current configuration | Always newest |
| `config.yaml.bak1` | Most recent backup | Auto-created before each mutation save |
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

*Document version: 1.0 | Last updated: 2026-08-13 | Author: Simar Sahni*
