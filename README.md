# status-my-page

> 🌐 Personal service status dashboard with 3-state health indicators, persistent history, mobile-responsive dark theme, and deploy scripts. View-only by default — admin login required to manage.

## ✨ Features

- **3-State Status System**: Click services to cycle 🟢 Operational → 🟡 Degraded → 🔴 Outage
- **Smart Notes**: Auto-show hidden notes only for degraded/outage states, auto-hide on green  
- **Status History**: Every toggle and notes update is timestamped — open the 🕙 history panel per service to see the full change timeline
- **Dark Theme UI**: Responsive layout (≤640px & ≤425px breakpoints), mobile-first CSS with proper touch targets
- **Admin Controls**: Session-based auth, drag-and-drop reorder, inline rename, add/delete items, auto-saving notes
- **Config-Driven**: `config.yaml` controls everything — service names, credentials, server settings
- **Auto-Archival**: Pre-reset DB snapshots (JSON into `archives/`) saved on every restart so state survives seeding
- **YAML Backup Rotation**: Last 5 versions of `config.yaml` preserved automatically before each runtime save
- **Input Sanitization**: Every user-supplied field is validated through a dedicated filter layer — blocks XSS payloads, SQL injection patterns, path traversal, shell metacharacters, and fuzzing attacks

## 📸 Preview

![Application Status Dashboard](screenshots/dashboard.png)

A dark-themed, mobile-responsive dashboard showing monitored services with colored status indicators (🟢 Operational, 🟡 Degraded, 🔴 Outage), per-service notes, and a summary pill at the top.

---

## 📋 Prerequisites

| Requirement | Minimum Version | Notes |
|-------------|-----------------|-------|
| Python | 3.10+ | Tested on 3.12–3.14 with CPython |
| pip | Any recent version | Used only for `requirements.txt` (flask, pyyaml) |
| SQLite | Bundled with Python | Database lives in `instance/status.db` (WAL mode auto) |
| Optional: gunicorn | 20+ | Production WSGI server (used by `install.sh`) |

**OS support:** Any Linux distro (Ubuntu, Debian, Fedora, RHEL, Arch, etc.) and macOS. The install script detects `apt`, `dnf`, or `yum` package managers automatically.

---

## 🚀 Installation

### Option 1: Local development

The quickest path — ideal for testing on your own machine or a VPS before production deploy.

```bash
# 1️⃣  Clone the repository
git clone https://github.com/notsimar/status-my-page.git
cd status-my-page

# 2️⃣  Create a virtual environment and install deps
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt     # installs flask + pyyaml

# 3️⃣  Generate a password hash
#    (NEVER store plaintext passwords in config.yaml or commit them to git)
python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('my-secure-pw'))"

# 📌 Copy the output — it looks like: scrypt$72816...
```

**Edit `config.yaml`** to list your services:

```yaml
items:
  - Primary Internet
  - Secondary Internet
  - Router
  - Primary NAS
  - Secondary NAS
  - DNS
  - Core Switch
  - Google workspace
  - Slack
  # ... add all the services you want to monitor

admin:
  user: admin                        # ← or override via STATUS_ADMIN_USER env var

server:
  host: "0.0.0.0"
  port: 8920
```

> **Note:** Do not put a plaintext password in `config.yaml`. The `STATUS_ADMIN_PASS_HASH` environment variable is **required** (no fallback). Generate one with the command below.

**Start the server:**

```bash
# Set your credentials via env vars, then run:
export STATUS_ADMIN_PASS_HASH="scrypt$72816$..."   # from step 3 above

./start.sh
```

The server launches on `http://localhost:8920`. Admin user is the value from `config.yaml` under `admin.user` (default: `admin`).

### Option 2: One-command production install (`install.sh`)

For a fresh Linux server (Ubuntu, Debian, Fedora, RHEL), the wizard handles everything:

```bash
# Clone into any directory (e.g. your home)
git clone https://github.com/notsimar/status-my-page.git ~/status-my-page
cd ~/status-my-page

# Run the install script as root — it will:
#   • Install python3, venv, gunicorn system packages
#   • Create a dedicated 'statuspage' system user
#   • Deploy files to /opt/status-page (default)
#   • Create Python venv + install dependencies
#   • Seed the SQLite database from config.yaml
#   • Prompt for admin credentials (stored in /etc/status-page/env, mode 0640)
#   • Install & enable a systemd service (status-page.service)
#   • Start Gunicorn on 127.0.0.1:8920 behind systemd
sudo ./install.sh
```

**Choose a custom install path:**

```bash
sudo ./install.sh /srv/status-dashboard
```

**What the wizard asks during installation:**

```
=== Setting admin credentials ===
Admin username [admin]: ?
Admin password: ?       ← typed silently, hashed as scrypt, stored in /etc/status-page/env

Credentials set: user=?

=== Installing systemd service (status-page.service) ===
Service enabled. Starting…

=== Verification ===
✅ status-page.service is running
✅ Status page responding (HTTP 200)

  Deployment complete!
  URL: http://<server-ip>:8920/
```

**After-install management:**

```bash
systemctl status status-page          # check if running
sudo systemctl restart status-page    # apply config changes
sudo journalctl -u status-page -f     # live logs
./cleanup.sh list                     # view archived snapshots
```

### Option 3: Manual systemd (no root / custom path)

If you can't or don't want to use `install.sh`, here's a manual example for **Ubuntu 22.04+**:

```bash
# Install system deps
sudo apt update && sudo apt install -y python3 python3-venv gunicorn

# Clone + setup the app
git clone https://github.com/notsimar/status-my-page.git /opt/status-page
cd /opt/status-page
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Create a service user (optional but recommended)
sudo useradd --system --no-create-home --shell /usr/sbin/nologin statuspage
sudo chown -R statuspage:statuspage /opt/status-page/{instance,logs,archives} 2>/dev/null || true

# Write credentials to env file
mkdir -p /etc/status-page
sudo tee /etc/status-page/env > /dev/null <<EOF
STATUS_ADMIN_PASS_HASH=scrypt$72816$...   # your hash here
STATUS_ADMIN_USER=admin
PYTHONUNBUFFERED=1
EOF
sudo chmod 0640 /etc/status-page/env

# Create systemd unit
sudo tee /etc/systemd/system/status-page.service > /dev/null <<'EOF'
[Unit]
Description=Status Page Web App
After=network.target

[Service]
Type=simple
User=statuspage
Group=statuspage
WorkingDirectory=/opt/status-page
ExecStart=/opt/status-page/.venv/bin/gunicorn \
    --bind 127.0.0.1:8920 \
    --workers 2 \
    --timeout 30 \
    app:app
Restart=on-failure
RestartSec=5
EnvironmentFile=/etc/status-page/env

[Install]
WantedBy=multi-user.target
EOF

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable --now status-page
sudo systemctl status status-page
```

### 🔁 Behind a reverse proxy (recommended for production)

The app binds to `127.0.0.1:8920` — put Nginx or Caddy in front to serve over HTTPS:

**Nginx example:**

```nginx
server {
    listen 443 ssl http2;
    server_name status.example.com;

    ssl_certificate     /etc/letsencrypt/live/status.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/status.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8920;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;  # so Flask sets secure cookies
    }
}
```

**Caddy example (shorter):**

```caddy
status.example.com {
    reverse_proxy 127.0.0.1:8920
}
```

> **Important:** Make sure `X-Forwarded-Proto` is set so Flask knows the request is HTTPS — session cookies are marked `Secure` in that case.

---

## 📱 Status states at a glance

| State | Emoji | Notes behavior |
|-------|-------|----------------|
| Operational | 🟢 Green | Hidden automatically |
| Degraded | 🟡 Yellow | Visible — admin can add context notes |
| Outage | 🔴 Red | Always visible with pulsing red glow |

*Click any service as admin to cycle through states ♪*

## 🕙 Status History

Every mutation (status toggle, notes update) is recorded in a `status_history` SQLite table.

- **View**: Click the 🕙 clock icon on any service row → modal shows newest-first timeline
- **API**: `GET /api/history/<item_id>` — public read, no auth required
- **Format**: Entries include `event_type`, `old_value`, `new_value`, `occurred` (ISO-8601 UTC)

## 🔧 Configuration

### `config.yaml` structure

```yaml
items:
  - Primary Internet      # ← add your services here
  - Secondary Internet
  - Router
  - Primary NAS
  - Secondary NAS
  - DNS
  - Core Switch
  - Google workspace
  - Slack
  # Every restart, the DB is synced to this list

admin:
  user: admin       # ← can be overridden by STATUS_ADMIN_USER env var
  # Do NOT put plaintext passwords here — use STATUS_ADMIN_PASS_HASH instead

server:
  host: "0.0.0.0"
  port: 8920
  secret_key_env: STATUS_SECRET_KEY   # Flask session signing key
```

### Healthchecks (optional)

Automated per-service health checks update status indicators (🟢/🟡/🔴) in the background. Add a `healthchecks` section to `config.yaml`:

```yaml
healthchecks:
  Primary Internet:
    type: ping
    host: 1.1.1.1
    interval: 30
    timeout: 3
    retries: 2
  Database:
    type: tcp
    host: 192.168.10.50
    port: 5432
    interval: 60
    timeout: 5
    retries: 3
  API Gateway:
    type: curl
    url: https://api.example.com/health
    healthy_codes: [200, 204]
    interval: 30
    timeout: 10
    retries: 2
  Legacy SOAP Service:
    type: soap
    url: http://legacy:9000/soap
    soap_action: "HealthCheck"
    expected_string: "<Status>OK</Status>"
    interval: 60
    timeout: 15
```

**Healthcheck Types:**

| Type | Required Keys | Optional Keys | Use Case |
|------|---------------|---------------|----------|
| `ping` | `host` | `interval`, `timeout`, `retries` | ICMP reachability (routers, hosts) |
| `tcp` | `host`, `port` | `interval`, `timeout`, `retries` | TCP port connectivity (DB, Redis, SMTP) |
| `curl` | `url` | `healthy_codes`, `interval`, `timeout`, `retries` | HTTP/HTTPS REST APIs |
| `soap` | `url` | `soap_action`, `body`, `expected_string`, `healthy_codes`, `interval`, `timeout`, `retries` | SOAP/XML web services |

**Auto-detection:** If `type` is omitted, the parser infers it:
- `host` + `port` → `tcp`
- `host` only → `ping`
- `url` → `curl` (or `soap` if `soap_action`/`body` present)

**Key Options:**
- `interval` — seconds between checks (default: 60)
- `timeout` — seconds per attempt (default: 10)
- `retries` — consecutive failures before marking degraded/red (default: 2)
- `healthy_codes` — HTTP codes considered healthy (default: `[200]`)
- `expected_string` / `body_contains` — response must contain this substring

### Environment variables

| Variable | Purpose | Required | Example |
|----------|---------|----------|---------|
| `STATUS_ADMIN_USER` | Override admin username from config.yaml | No | `john` |
| `STATUS_ADMIN_PASS_HASH` | Password hash (**required** for production) | **Yes** | `scrypt$72816$...` |
| `STATUS_SECRET_KEY` | Flask session signing key (auto-generated if unset) | No | Any random string |
| `STATUS_NO_ARCHIVE=1` | Skip DB archival on restart (dev/testing only) | No | — |

**Generate a hash:**

```bash
python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('my-secure-pw'))"
```

### Runtime persistence & backups

Admin changes (status, notes, reorder) are persisted back to `config.yaml` under a `_runtime` section so they survive DB resets. On every save:

1. Current `config.yaml` → `.bak1`
2. Existing backups shift up (`.bak1` → `.bak2` … → `.bak5`)
3. New config written atomically (`tempfile` + `os.replace`)

Backup files are excluded from git via `.gitignore`. You can recover:

```bash
# Restore from the most recent backup
cp config.yaml.bak1 config.yaml
./restart.sh
```

---

## 🛠 Scripts reference

| Script | Purpose | Example usage |
|--------|---------|---------------|
| `start.sh` | Launch dev server (PID tracking, logs → `logs/server.log`) | `./start.sh` |
| `stop.sh` | Graceful shutdown via PID file | `./stop.sh` |
| `restart.sh` | Kill + start without reinstalling deps | `./restart.sh` |
| `rebuild.sh` | Full dep install + DB migrations + restart | `./rebuild.sh` |
| `install.sh` | Production deploy wizard (systemd, user, gunicorn) | `sudo ./install.sh[/path]` |
| `cleanup.sh` | Archive manager for `archives/` JSON snapshots | See commands below |

### `cleanup.sh` commands

```bash
./cleanup.sh list                 # List all archive snapshots (newest first)
./cleanup.sh show 20260811_091523   # Pretty-print a specific snapshot
./cleanup.sh report               # Historical outage summary across archives
./cleanup.sh prune                # Keep last 2, delete the rest
./cleanup.sh prune --keep 10      # Keep last 10 snapshots instead
```

---


## 🧪 Testing & Quality Assurance

### Test Coverage Summary

| Test Suite | Coverage | Description |
|------------|----------|-------------|
| `test_input_filter.py` | **100%** | 80+ assertions: XSS payloads, SQLi patterns, path traversal, shell injection, fuzzing, and safe-string passthrough |
| `test_healthcheck.py` | 76% | Healthcheck parsing, endpoints, worker lock, and 20 exception-path tests (timeout, missing binaries, bad curl output) — includes TCP, ping, curl, SOAP |
| `test_mc_dc.py` | — | **MC/DC formal verification** of 7 compound decisions (admin+CSRF+rate-limit gates, YAML restore filters, CSRF internals, delete cleanup) |
| `test_structural.py` | — | MC/DC for reorder override (D4) and set_notes YAML persist gate (D5) |
| `test_history.py` | — | 13 end-to-end scenarios: history recording, public access, cascade delete, pruning |
| `test_routes_and_features.py` | — | Auth, mutations, security headers, backups, admin credential validation |
| `test_restart_persistence.py` | — | 2 critical restart-simulation tests (add/delete survival) |
| `test_healthcheck_mc_dc.py` | — | MC/DC for healthcheck result gate (D_hc1) and URL sanitisation (D_hc2) + worker lock tests |

**Overall coverage: 88%** (app.py 92%, healthcheck.py 76%, input_filter.py 100%)

### Running Tests

```bash
# All tests (no server needed)
.venv/bin/pytest tests/ -v

# Specific test categories
.venv/bin/pytest tests/test_input_filter.py -v              # Input sanitization (100%)
.venv/bin/pytest tests/test_mc_dc.py -v                      # MC/DC structural proofs
.venv/bin/pytest tests/test_structural.py -v                 # Additional MC/DC (D4, D5)
.venv/bin/pytest tests/test_healthcheck.py -v                # Healthcheck + exception paths
.venv/bin/pytest tests/test_healthcheck_mc_dc.py -v          # Healthcheck MC/DC + worker lock
.venv/bin/pytest tests/test_history.py -v                    # Status history feature
.venv/bin/pytest tests/test_routes_and_features.py -v        # Auth, mutations, headers
.venv/bin/pytest tests/test_restart_persistence.py -v        # Restart simulation

# With coverage report
.venv/bin/pytest tests/ --cov=app --cov=healthcheck --cov=input_filter --cov-report=term-missing
```

### Structural Verification (MC/DC)

This project employs **Modified Condition/Decision Coverage** to prove that critical security guards and restoration filters are logically sound.

- **Coverage Target**: Every condition in compound boolean expressions independently affects the outcome.
- **Verified Guards**: Admin authentication, CSRF validation, Rate limiting, YAML runtime restoration logic, reorder override, set_notes persistence gate, CSRF internal guard, delete cleanup cascade.
- **Documentation**: See [README_MCDC.md](./README_MCDC.md) for the full mapping table and proof matrices.

```bash
# Run MC/DC structural coverage
.venv/bin/pytest tests/test_mc_dc.py tests/test_structural.py tests/test_healthcheck_mc_dc.py -v
```

The structural test suite uses fixture-based HTTP clients to simulate authenticated requests, then probes compound boolean expressions (auth guards, CSRF validation, rate-limiting thresholds, YAML restoration filters, `_runtime` key filtering, healthcheck result evaluation, URL sanitisation, worker locking). Each assertion maps to a condition in the MC/DC proof matrix documented in [README_MCDC.md](./README_MCDC.md).

### Quick Health Check (Shell)

For CI pipelines or post-deploy validation:

```bash
# Requires running server on localhost:8920 (or set BASE_URL)
./tests/test_health.sh                    # Default: http://localhost:8920
./tests/test_health.sh http://myserver:9920  # Custom URL
```

This shell script validates: page load, static assets, auth-check, login, mutation auth requirement, 3-state toggle cycle, and notes API — all in ~2 seconds.

## 🗂 File structure

```
status-my-page/
├── config.yaml              # Service names, admin creds, server cfg, runtime overrides
├── input_filter.py          # Centralized input validation (XSS, SQLi, fuzzing sanitization)
├── app.py                   # Flask routes + SQLite DB logic (history, archival, auth)
├── cleanup.sh               # Archive manager: list / show / prune / report
├── requirements.txt         # flask, pyyaml only!
├── static/
│   ├── css/style.css        # Dark theme, 3 breakpoints (≤640px, ≤425px)
│   └── js/app.js            # Vanilla JS: toggle, drag-drop, notes, history modal
├── templates/index.html     # Jinja2-rendered UI with login & history modals
├── start.sh / stop.sh / restart.sh / rebuild.sh / install.sh
├── tests/
│   ├── conftest.py              # Shared fixtures (temp DB, admin auth, CSRF)
│   ├── test_input_filter.py     # 80+ assertions: XSS, SQLi, fuzzing, sanitization
│   ├── test_structural.py       # Per-route mutation logic & YAML persistence (MC/DC D4, D5)
│   ├── test_history.py          # Automated API/DB history test suite (13 scenarios)
│   ├── test_mc_dc.py            # MC/DC structural coverage for 7 critical guards
│   ├── test_healthcheck.py      # Healthcheck functional + 17 exception-path tests
│   ├── test_healthcheck_mc_dc.py # Healthcheck MC/DC (D_hc1, D_hc2) + worker lock
│   ├── test_routes_and_features.py # Auth, mutations, headers, backups
│   ├── test_restart_persistence.py # Restart simulation tests
│   └── test_health.sh           # Quick health-check script for CI/CD
├── License.md               # MIT © 2026 Simar Sahni
└── .venv/                   # (excluded from git/deploy)
```

## 🔒 Security checklist

| Feature | Implementation |
|---------|---------------|
| **Authentication** | Flask signed sessions only — no plaintext cookie fallback; `STATUS_ADMIN_PASS_HASH` env var **required** |
| **CSRF protection** | Per-request secret token (header-only, no query param leakage), rotated on every successful mutation; 3-strike session wipe |
| **Login rate-limit** | 5 failed attempts per IP → 30-second lockout |
| **Mutation rate-limit** | 60 mutations per IP per 60-second window |
| **Password storage** | werkzeug `scrypt` hashing with timing-safe HMAC comparison |
| **Input sanitization** | Centralized `input_filter.py` layer — blocks XSS, SQLi, path traversal, shell injection, null bytes, and oversized payloads on every mutation route |
| **Content Security Policy** | `default-src 'self'`; inline CSS via `'unsafe-inline'` on style-src only |
| **Additional headers** | X-Content-Type-Options, X-Frame-Options=DENY, Referrer-Policy, Permissions-Policy |

## 💻 API endpoints

| Route | Method | Auth | Action |
|-------|--------|------|--------|
| `/` | `GET` | Public | Render full status page |
| `/api/history/<id>` | `GET` | Public | Return change timeline for a service |
| `/api/toggle/<id>` | `POST` | 🔒 Admin+CSRF | Cycle: green → degraded → red |
| `/api/notes/<id>` | `POST` | 🔒 Admin+CSRF | Save/update freeform note text |
| `/api/add` | `POST` | 🔒 Admin+CSRF | Create new service item |
| `/api/delete/<id>` | `POST` | 🔒 Admin+CSRF | Remove service + compact positions + prune history |
| `/api/rename/<id>` | `POST` | 🔒 Admin+CSRF | Update service display name |
| `/api/reorder` | `POST` | 🔒 Admin+CSRF | Apply drag-drop position map |
| `/api/csrf-token` | `GET` | 🔒 Admin | Fetch fresh CSRF token |
| `/login` / `/logout` / `/auth-check` | — | Public | Session management |

### Quick curl examples

```bash
# View history for service id=1 (no auth needed)
curl http://localhost:8920/api/history/1 | jq

# Login to get a session cookie, then toggle status
TOKEN=$(curl -s http://localhost:8920 | grep -o 'csrf_token[^"]*"[^"]*"' | head -1 | awk -F'"' '{print $4}')
curl -b cookies.json -s -X POST http://localhost:8920/login \
  -d '{"user":"admin","pass":"your-password"}'

# Toggle service #1
export CSRF_TOKEN=...   # from page or /api/csrf-token
curl -X POST http://localhost:8920/api/toggle/1 \
  -H "X-CSRF-Token: $CSRF_TOKEN"
```

## 📜 License

MIT License. Copyright © 2026 Simar Sahni ([@notsimar](https://github.com/notsimar)). See [License.md](./License.md).
