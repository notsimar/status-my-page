# status-my-page

> 🌐 Personal service status dashboard with 3-state health indicators, persistent history, mobile-responsive dark theme, and deploy scripts. View-only by default — admin login required to manage.

## ✨ Features

- **3-State Status System**: Click services to cycle 🟢 Operational → 🟡 Degraded → 🔴 Outage
- **Smart Notes**: Auto-show hidden notes only for degraded/outage states, auto-hide on green
- **Status History**: Every toggle and notes update is timestamped — enable the 🕙 history panel (admin → Page Settings) to see the full change timeline per service
- **Dark/Light Theme UI**: User-selectable theme (per-browser, no first-paint flash), responsive layout (≤640px & ≤425px breakpoints), mobile-first CSS with proper touch targets
- **Admin Controls**: Session-based auth, drag-and-drop reorder, inline rename, add/delete items, auto-saving notes, static page export
- **Healthchecks**: Background probing via HTTP/HTTPS (`curl`), TCP, ICMP (`ping`), SOAP, and vendor RSS/Atom status feeds with custom failure/degraded keywords and service linking
- **Static Page Export**: One-click standalone HTML export with inlined CSS for CDN/mass-delivery hosting
- **Config-Driven**: `config.yaml` provides read-only provisioning input for services, credentials, and initial healthcheck definitions
- **Database Persistence**: SQLite (`instance/status.db`) is the single source of truth for all service items, statuses, notes, positions, and mutation history
- **Auto-Archival & Backups**: Pre-reset JSON snapshots (`archives/`) on restart, and atomic backup rotation (`.bak1`–`.bak5`) for `config.yaml` healthcheck edits
- **Input Sanitization**: Every user-supplied field is validated through a dedicated filter layer — blocks XSS payloads, SQL injection patterns, path traversal, shell metacharacters, and fuzzing attacks
- **Slack Notifications**: Optional integration queues every status change (manual toggles + healthcheck flips) into a persistent outbox and posts one digest message to a Slack channel when the admin logs out — survives restarts, retries failed deliveries
- **Structured Request Logging**: `access.log` captures every request with client IP (X-Forwarded-For aware), browser/OS summary, method, path, status, duration; `app.log` records security events (login ok/failed/rate-limited with IP + User-Agent) — both rotate at 5 MB × 3 backups
- **Logo Installer**: `scripts/install_logo.sh` drops customer logos into `static/logos/` and wires `config.yaml` — single file or dark/light variants
- **Release Builder**: `scripts/build_release.sh` packages exactly the git-tracked files into a clean `dist/status-my-page-<version>.tar.gz` ready for `tar + ./install.sh` deployment
- **Browser Spellcheck**: Native spellcheck enabled on all text input fields (status notes, SOAP body, healthcheck keywords, expected strings, failure/degraded keywords, RSS keywords) — works across all modern browsers

## 📸 Preview

![Application Status Dashboard](screenshots/dashboard.png)

A dark-themed, mobile-responsive dashboard showing monitored services with colored status indicators (🟢 Operational, 🟡 Degraded, 🔴 Outage), per-service notes, and a summary pill at the top.

---

## 📋 Prerequisites

| Requirement        | Minimum Version     | Notes                                                  |
|--------------------|---------------------|--------------------------------------------------------|
| Python             | 3.9+                | Tested on 3.9–3.14 with CPython (incl. macOS default 3.9.6) |
| pip                | Any recent version  | Used only for `requirements.txt` (flask, gunicorn, pyyaml, python-dotenv) |
| SQLite             | Bundled with Python | Database lives in `instance/status.db` (WAL mode auto) |
| Optional: gunicorn | 20+                 | Production WSGI server (used by `install.sh`)          |

**OS support:** Any Linux distro (Ubuntu, Debian, Fedora, RHEL, Arch, etc.) and macOS. The install script detects `apt`, `dnf`, or `yum` package managers automatically.

---

## 🚀 Installation

### Option 1: Local development

The quickest path — ideal for testing on your own machine or a VPS before production deploy.

**Interactive setup (recommended):**

```bash
git clone https://github.com/notsimar/status-my-page.git
cd status-my-page
./dev-setup.sh
```

`dev-setup.sh` performs a safe `git pull` (skipped when there are uncommitted
local changes), creates the virtualenv, installs dependencies from the
bundled `vendor/` wheels (with PyPI fallback for network-restricted
environments), then **prompts you for each setup option** with sensible
defaults:

| Prompt            | Default      | Written to                        |
|-------------------|--------------|-----------------------------------|
| Admin username    | `admin`      | `DEV_ADMIN_USER` + synced into `config.yaml` |
| Admin password    | *(asked)*    | `STATUS_ADMIN_PASS_HASH` (scrypt) |
| Dev server port   | `8920`       | `DEV_PORT`                        |
| Disable healthchecks? | `Y`    | `STATUS_DISABLE_HEALTHCHECKS`     |
| Logo file         | *(blank)*    | `DEV_LOGO_PATH` + `config.yaml` logo section |

Re-runs offer your existing values as the defaults, so you just press Enter,
and an existing password is kept unless you explicitly choose to reset it.
The script writes `.env.local` (mode `600`, single-quoted values so
`source` never truncates `$` in scrypt hashes), seeds the database, and
prints the exact command to start the dev server.

After setup, start the dev server with:

```bash
source .venv/bin/activate
flask --app app run --host 127.0.0.1 --port 8920
```

> The dev server binds to `127.0.0.1` by default. Use `./start.sh` instead if
> you want the gunicorn-based launcher (gunicorn, PID tracking, logs →
> `logs/server.log`).

<details>
<summary>Manual equivalent (no prompts)</summary>

```bash
# 1️⃣  Clone the repository
git clone https://github.com/notsimar/status-my-page.git
cd status-my-page

# 2️⃣  Create a virtual environment and install deps
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install --find-links ./vendor -r requirements.txt

# 3️⃣  Generate a password hash
#    (NEVER store plaintext passwords in config.yaml or commit them to git)
python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('my-secure-pw'))"

# 📌 Copy the output — it looks like: scrypt:32768:8:1$<salt>$<hex-hash>
```

</details>

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

_base:
  admin:
    user: admin                        # ← or override via STATUS_ADMIN_USER env var
  server:
    host: "0.0.0.0"
    port: 8920
    secret_key_env: STATUS_SECRET_KEY   # Flask session signing key
```

> The top-level `admin:` / `server:` form (without `_base:`) also works — it is
> auto-migrated into `_base` on the first runtime write. After any admin API
> edit the file uses the `_base` form shown above.

> **Note:** Do not put a plaintext password in `config.yaml`. The `STATUS_ADMIN_PASS_HASH` environment variable is **required** (no fallback). Generate one with the command below.

**Start the server:**

```bash
# Set your credentials via env vars, then run:
export STATUS_ADMIN_PASS_HASH="scrypt:32768:8:1\$<salt>\$<hash>"   # from step 3 above

./start.sh
```

The server launches on `http://localhost:8920`. Admin user is `config.yaml` → `_base.admin.user` (default: `admin`), overridable with the `STATUS_ADMIN_USER` env var.

### Option 2: One-command production install (`install.sh`)

For a fresh Linux server (Ubuntu, Debian, Fedora, RHEL), the wizard handles everything:

```bash
# Clone into any directory (e.g. your home)
git clone https://github.com/notsimar/status-my-page.git ~/status-my-page
cd ~/status-my-page

# Run the install — root or normal user:
#   • Requires python3 + python3-venv to be present (fail-fast with hints otherwise)
#   • Runs as the invoking user (no separate system user is created)
#   • Deploys files to $HOME/.local/share/status-page (default) — or any absolute path
#   • Creates Python venv + installs dependencies (PyPI or bundled vendor/ wheels)
#   • Prompts for admin credentials (stored in <install_dir>/.env.local, mode 0600)
#   • Seeds the SQLite database from config.yaml
#   • Installs & enables a systemd service (status-page.service) — root mode only
#   • Root mode: Gunicorn bound to 0.0.0.0:8920 under systemd
#   • Non-root mode: tells you to start with ./start.sh
sudo ./install.sh
```

**Choose a custom install path (must be absolute):**

```bash
./install.sh /srv/status-dashboard
```

**What the wizard asks during installation:**

```
=== status-my-page: installing ===
... (system packages, venv, DB seed) ...

=== Admin credentials ===
Admin username [admin]: ?
Admin password: ?       ← typed silently, hashed (scrypt), stored in .env.local

Credentials set: user=? (new password)

=== Installing systemd service (status-page.service) ===
Service enabled. Starting…
Service is up and serving on port 8920.
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
.venv/bin/pip install --find-links ./vendor -r requirements.txt

# Create a service user (optional but recommended)
sudo useradd --system --no-create-home --shell /usr/sbin/nologin statuspage
sudo chown -R statuspage:statuspage /opt/status-page/{instance,logs,archives} 2>/dev/null || true

# Write credentials to env file
mkdir -p /etc/status-page
sudo tee /etc/status-page/env > /dev/null <<EOF
STATUS_ADMIN_PASS_HASH=scrypt:32768:8:1$<salt>$<hash>   # your full hash here
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
    --bind 0.0.0.0:8920 \
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

The app binds to `0.0.0.0:8920` (all interfaces). For production, put Nginx or Caddy in front to serve over HTTPS and restrict direct access with a firewall if needed:

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

- **Off by default** — enable it from the admin UI (Page Settings → 🕙 History button) or via `settings: {history_enabled: true}` in `config.yaml`
- **View**: Click the 🕙 clock icon on any service row → modal shows newest-first timeline
- **API**: `GET /api/history/<item_id>` — public read, no auth required (returns 404 while disabled)
- **Format**: Entries include `event_type`, `old_value`, `new_value`, `occurred` (ISO-8601 UTC)

## 🔧 Configuration

### `config.yaml` structure

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

_base:
  admin:
    user: admin                        # ← or override via STATUS_ADMIN_USER env var
  server:
    host: "0.0.0.0"
    port: 8920
    secret_key_env: STATUS_SECRET_KEY   # Flask session signing key
```

> Legacy top-level `admin:` / `server:` keys are auto-migrated into `_base` on
> the first runtime write. See [docs/configuration.md](docs/configuration.md)
> for every section (`settings:`, `rss:`, `slack:`, `logo:`).

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
  Google Workspace:
    type: rss
    url: https://www.google.com/appsstatus/dashboard/en/feed.atom   # feed URL, not the dashboard page
    keywords:
      red: [outage, major issue]
      degraded: [degraded, minor, investigating]
    interval: 60
    timeout: 10
    retries: 2
```

**Healthcheck Types:**

| Type   | Required Keys | Optional Keys                                                                               | Use Case                                |
|--------|---------------|---------------------------------------------------------------------------------------------|-----------------------------------------|
| `ping` | `host`        | `service`, `interval`, `timeout`, `retries`                                                 | ICMP reachability (routers, hosts)      |
| `tcp`  | `host`, `port`| `service`, `interval`, `timeout`, `retries`                                                 | TCP port connectivity (DB, Redis, SMTP) |
| `curl` | `url`         | `service`, `healthy_codes`, `failure_keyword`, `degraded_keyword`, `interval`, `timeout`, `retries` | HTTP/HTTPS REST APIs            |
| `soap` | `url`         | `service`, `soap_action`, `body`, `expected_string`, `failure_keyword`, `degraded_keyword`, `healthy_codes`, `interval`, `timeout`, `retries` | SOAP/XML web services |
| `rss`  | `url`         | `service`, `keywords.red`, `keywords.degraded`, `interval`, `timeout`, `retries`            | Vendor status pages (RSS/Atom feeds)    |

**Auto-detection:** If `type` is omitted, the parser infers it:
- `host` + `port` → `tcp`
- `host` only → `ping`
- `url` → `curl` (or `soap` if `soap_action`/`body` present)
- `rss` is never auto-detected — a bare `url` always means `curl`; set `type: rss` explicitly.

**RSS feed checks:** The worker fetches the feed (curl, stdlib XML parse,
first 20 entries, 512 KB cap) and scans entry titles/descriptions for
keywords. A matching `red` keyword flips the item red *immediately* (next
check, no retry ladder); a matching `degraded` keyword flips it degraded;
a clean feed flips it back to green. If the feed itself can't be fetched,
the normal retry ladder applies (degraded → red). `keywords` is optional:
with no keywords, only fetch failures change the status. Feeds whose XML
preamble declares a DOCTYPE with **internal DTD entities** (the "billion
laughs" entity-expansion vector) are rejected as unreachable before
parsing — a vendor feed can never drive the dashboard red via a crafted
DTD (`healthcheck.feed_treats_as_unfetchable`).

**Key Options:**
- `service` — target dashboard service name to update (defaults to healthcheck name if omitted)
- `interval` — seconds between checks (default: 60)
- `timeout` — seconds per attempt (default: 10)
- `retries` — severity ladder threshold (default: 2). Single retrieval
  failure = no state change. If the number of *consecutive* failures
  reaches `retries` the service flips **degraded**, and at
  `retries × 3` it flips **red**:
  | Consecutive failures | healthcheck state     |
  |--------------------:|----------------------|
  | `< retries`          | no change            |
  | `retries … 3×retries−1` | **degraded**     |
  | `≥ 3×retries`        | **red**              |
  RSS "cannot retrieve feed" failures go through the same ladder as
  other check types; keyword-driven red/degraded flips do not.
  (implemented in `healthcheck.severity_from_failures`)
- `healthy_codes` — HTTP codes considered healthy (default: `[200]`)
- `expected_string` — response must contain this substring (SOAP)
- `failure_keyword` — response body match that flags RED/outage (HTTP/SOAP)
- `degraded_keyword` — response body match that flags DEGRADED/warning (HTTP/SOAP)
- `keywords.red` / `keywords.degraded` — rss type: marker words per severity

### Environment variables

| Variable                 | Purpose                                             | Required | Example            |
|--------------------------|-----------------------------------------------------|----------|--------------------|
| `STATUS_ADMIN_USER`      | Override admin username from config.yaml (`_base.admin.user`) | No | `john`             |
| `STATUS_ADMIN_PASS_HASH` | Password hash (**required** for production)         | **Yes**  | `scrypt:32768:8:1$...$...` |
| `STATUS_SECRET_KEY`      | Flask session signing key. If unset, one is auto-generated **persisted to `instance/.secret_key`** (mode 600) so multi-worker gunicorn deployments share one key and sessions survive restarts | No | Any random string |
| `STATUS_NO_ARCHIVE=1`    | Skip DB archival on restart (dev/testing only)      | No       | —                  |
| `STATUS_TRUST_PROXY=1`   | Trust `X-Forwarded-For` for client IP (enable ONLY behind a reverse proxy that overwrites the header) | No | — |
| `STATUS_SECURE_COOKIES=1`| Set the `Secure` flag on session cookies (HTTPS deployments) | No | — |
| `STATUS_DISABLE_HEALTHCHECKS=1` | Don't start the healthcheck worker (dev/testing) | No | — |
| `STATUS_SLACK_WEBHOOK_URL` | Slack webhook fallback if unset in config.yaml   | No       | `https://hooks.slack.com/...` |

> Note the difference between `STATUS_DISABLE_HEALTHCHECKS` (kills the worker
> process entirely — dev/testing) and `settings.healthchecks_enabled` in
> config.yaml (opt out at runtime, toggleable from the admin UI). The repo's
> bundled `config.yaml` ships with `healthchecks_enabled: false`.

### 📚 Documentation

| Document | Contents |
|---|---|
| [docs/architecture.md](docs/architecture.md) | System design, data flow, component diagrams, security architecture |
| [docs/api-reference.md](docs/api-reference.md) | Every HTTP endpoint with request/response examples |
| [docs/configuration.md](docs/configuration.md) | Full config.yaml reference, env vars, security credentials, migrations |
| [docs/deployment-guide.md](docs/deployment-guide.md) | install.sh, manual systemd, Docker, reverse proxies, SSL, monitoring |
| [docs/scripts-and-maintenance.md](docs/scripts-and-maintenance.md) | Every shell script: purpose, execution flow, safety features |
| [docs/testing.md](docs/testing.md) | Test suite layout, running tests, MC/DC decision coverage |
| [README_MCDC.md](README_MCDC.md) | Structural-testing strategy (MC/DC proof tables) |

**Generate a hash:**

```bash
python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('my-secure-pw'))"
# Output looks like: scrypt:32768:8:1$<salt>$<hex-hash> — copy the whole string
```

### Database persistence & backups

The SQLite database (`instance/status.db`) is the single source of truth for all services, statuses, notes, positions, and history. `config.yaml` provides provisioning input for new items and initial configuration; the `healthchecks:`, `rss:`, and `settings:` sections are additionally maintained at runtime by the admin API (atomic writes with backup rotation) and are read from disk on every use.

When healthchecks are updated via the admin API:
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
|---|---|---|
| `install.sh` | Production deploy wizard (venv, DB seed, credentials, systemd in root mode) | `sudo ./install.sh[/abs/path]` |
| `dev-setup.sh` | Interactive dev bootstrap: git pull, venv, prompts, `.env.local`, DB seed | `./dev-setup.sh` |
| `start.sh` / `stop.sh` / `restart.sh` | Server lifecycle (gunicorn, PID tracking, logs → `logs/server.log`) | `./start.sh` |
| `rebuild.sh` | Full dep install + DB migrations + restart | `./rebuild.sh` |
| `cleanup.sh` | Archive manager for `archives/` JSON snapshots: list/show/prune/report | `./cleanup.sh list` |
| `change_password.sh` | Rotate the admin password hash in `.env.local` | `./change_password.sh` |
| `lib.sh` | Shared error-reporting helpers (sourced by other scripts, not a CLI) | — |
| `scripts/backup.sh` | Database backup / restore / list / prune (30-day default retention) | `./scripts/backup.sh --list` |
| `scripts/backup_db.py` | Consistent SQLite backup/restore (the engine behind `backup.sh`) | `python3 scripts/backup_db.py --list` |
| `scripts/export_db.py` | Database export utility | `python3 scripts/export_db.py` |
| `scripts/build_release.sh` | Build a clean deployable `dist/*.tar.gz` from git-tracked files | `./scripts/build_release.sh` |
| `scripts/install_logo.sh` | Install customer logos into `static/logos/` + wire config.yaml | `./scripts/install_logo.sh logo.png` |
| `scripts/fake_slack.py` | Local mock Slack webhook for testing notifications | `python3 scripts/fake_slack.py` |

### `cleanup.sh` commands

```bash
./cleanup.sh list                 # List all archive snapshots (newest first)
./cleanup.sh show 20260811_091523   # Pretty-print a specific snapshot
./cleanup.sh report               # Historical outage summary across archives
./cleanup.sh prune                # Keep last 2, delete the rest
```

---

## 📜 License

MIT License — see [License.md](License.md) for details.

---

## 🙏 Acknowledgments

- [Flask](https://flask.palletsprojects.com/) — lightweight WSGI framework
- [Werkzeug](https://werkzeug.palletsprojects.com/) — WSGI utilities & password hashing
- [PyYAML](https://pyyaml.org/) — YAML parsing
- [gunicorn](https://gunicorn.org/) — production WSGI server
- Browser native `spellcheck` attribute — zero-dependency spellcheck

---

*Built with 💚 for self-hosted observability*