# Deployment Guide

## Table of Contents

- [1. Supported Operating Systems](#1-supported-operating-systems)
- [2. One-Command Installation (Recommended)](#2-one-command-installation-recommended)
- [3. Manual systemd Setup](#3-manual-systemd-setup)
- [4. Reverse Proxy Configuration](#4-reverse-proxy-configuration)
- [5. Docker Deployment](#5-docker-deployment)
- [6. SSL/TLS Certificate Management](#6ssltls-certificate-management)
- [7. Monitoring & Logging](#7-monitoring--logging)
- [8. Backup & Recovery](#8-backup--recovery)
- [9. Troubleshooting](#9-troubleshooting)

---

## 1. Supported Operating Systems

| OS | Version(s) Tested | Notes |
|----|-------------------|-------|
| Ubuntu/Debian | 20.04 LTS, 22.04 LTS | Works; systemd service installed in root mode |
| Fedora/RHEL/CentOS | 38+ | dnf/yum |
| Arch Linux | Rolling | Manual installation only (install.sh does not auto-detect pacman) |
| macOS | 12+ (Monterey) | Development only (dev-setup.sh); not recommended for production |

**Hardware minimum requirements:**
- CPU: 1 core (any architecture x86_64 or aarch64)
- RAM: 256 MB (app itself uses <100 MB at idle)
- Disk: 500 MB free space (for app files, SQLite DB, logs, archives)

---

## 2. One-Command Installation (Recommended)

The `install.sh` wizard handles everything. **It can run as root or as a
normal user** — behavior adapts to how you invoke it, and the service always
runs as the **invoking user**:

- **Root (`sudo ./install.sh`)** — deploys to the install path owned by root
  and installs a hardened systemd `status-page.service` (NoNewPrivileges /
  ProtectSystem / PrivateTmp) running as that user.
- **Non-root (`./install.sh`)** — installs into the invoking user's space
  and finishes with "start with `./start.sh`" (no systemd).

> **Prerequisites are installed by you, not by the wizard:** `python3` (3.9+)
> and `python3-venv` must already exist (the script fails fast with install
> hints otherwise). The wizard manages the venv, app files, DB, credentials,
> and (root mode) the systemd unit — it does not install system packages,
> create a `statuspage` user, or write to `/etc/status-page`.

### Prerequisites
- `python3` (3.9+) and `python3-venv` installed
- `curl` + `ping` (IP utils) if you plan to use healthchecks — the probe
  worker shells out to the CLI binaries
- `pip` reachable (PyPI or the bundled `vendor/` wheels for air-gapped)
- Server reachable at the target network interface

### Installation Steps

```bash
# Clone the repository
git clone https://github.com/notsimar/status-my-page.git ~/status-my-page
cd ~/status-my-page

# Root (systemd-managed, runs as root):
sudo ./install.sh

# OR non-root (user-managed, start with ./start.sh):
./install.sh
```

**Choose a custom install path (must be absolute):**
```bash
sudo ./install.sh /srv/status-dashboard
#   or
./install.sh ~/my-status
```

**Non-interactive / CI** — pass both credentials and it prompts nothing:
```bash
./install.sh --admin-user ops --admin-pass "s3cret" --port 8920
#   (env equivalents: SP_ADMIN_USER, SP_ADMIN_PASS, STATUS_PORT, STATUS_BIND, SP_INSTALL_OVERRIDE_ENV)
```

**Upgrade in place** — re-run; it keeps the existing `.env.local` and DB by
default, and just refreshes the app files. Pass `--force-env` to replace
credentials.

### Options

| Flag | Env | Default | Purpose |
|------|-----|---------|---------|
| `--admin-user USER` | `SP_ADMIN_USER` | `admin` | Admin username |
| `--admin-pass PASS` | `SP_ADMIN_PASS` | prompted | Admin password (min 8 chars recommended) |
| `--port N` | `STATUS_PORT` | `8920` | Listen port (1–65535) |
| `--host ADDR` | `STATUS_BIND` | `0.0.0.0` | Bind address |
| `--workers N` | — | `2` | Gunicorn worker count |
| `--force-env` | `SP_INSTALL_OVERRIDE_ENV=1` | off | Overwrite existing `.env.local` even on upgrade |

### What the Wizard Does

1. **Preflight** — confirms `python3` (3.9+) is present and normalizes the
   install path. Non-root: explicitly skips system packages / systemd.
2. **Deploy files** — copies the app to `<install_path>/` (default
   `~/.local/share/status-page`; any absolute path may be passed; creates
   `instance/`, `logs/`, `archives/`, `static/logos/`).
3. **Python venv** — creates `.venv` and installs `requirements.txt`
   (`flask`, `gunicorn`, `pyyaml`, `python-dotenv`) from PyPI or the bundled
   `vendor/` wheels.
4. **Admin credentials** — prompts for username + password. The password is
   hashed as scrypt (via `generate_password_hash`, with a PBKDF2-SHA256
   fallback for builds without OpenSSL scrypt). The chosen username is synced
   into `config.yaml` so `_base.admin.user` matches. Credentials are written
   to `<install_path>/.env.local` (mode `0600`) — never `/etc/status-page`.
5. **DB seed** — runs `init_db()`; takes an archive snapshot of any existing
   DB first so previous admin changes survive.
6. **Systemd (root only)** — writes a hardened `status-page.service`
   (gunicorn, `EnvironmentFile=<install_path>/.env.local`, `User=<invoking
   user>`) and enables + starts it. Non-root: skips systemd.
7. **Verification** — in root mode, waits ~2s and curls
   `http://127.0.0.1:<port>/` and reports. In non-root mode, prints the
   `./start.sh` command.

### Interactive Prompts During Installation

```
=== Admin credentials ===
Admin username [admin]: <enter or type your desired username>
Admin password: <type silently — will be hashed as scrypt, not stored as plaintext>

Credentials set: user=<your_username> (new password)

=== Installing systemd service (status-page.service) ===   [root mode only]
Service enabled. Starting…
Service is up and serving on port 8920.

========================================
  Deployment complete!
  Install dir: /opt/status-page
  Admin user:  <your_username>
  Address:     http://0.0.0.0:8920
========================================
```

> **Non-root output** ends with `Non-root install complete. Start manually:
> cd <install_dir> && ./start.sh  — Serving on http://0.0.0.0:8920` instead of
> the systemd block.

### Post-Installation Commands

```bash
# Root / systemd:
systemctl status status-page
sudo journalctl -u status-page -f
sudo systemctl restart status-page

# Non-root (or manual):
cd <install_dir> && ./start.sh          # start
./stop.sh                                # stop
./restart.sh                             # restart

# Verify it serves:
curl -s http://localhost:8920/ | head -20
```


---

## 3. Manual systemd Setup

If you cannot use `install.sh`, here is a complete manual configuration for Ubuntu 22.04+:

### Step 1 — Install system dependencies
```bash
sudo apt update && sudo apt install -y python3 python3-venv gunicorn curl iputils-ping
```

> `curl` and `ping` are required at runtime for the healthcheck worker (curl/ping/tcp/rss check types use the CLI binaries directly). Without them those healthchecks fail with "command not found" and the item stays degraded/red.

### Step 2 — Clone and setup the application
```bash
sudo mkdir -p /opt/status-my-page
git clone https://github.com/notsimar/status-my-page.git /opt/status-my-page/.app
cd /opt/status-my-page/.app
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### Step 3 — Create a service user
```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin statuspage
sudo chown -R statuspage:statuspage /opt/status-my-page/.app/{instance,logs} 2>/dev/null || true
```

### Step 4 — Write credentials to a secure env file
```bash
# Generate the hash first:
#   python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('your-password'))"
sudo mkdir -p /etc/status-page
sudo tee /etc/status-page/env > /dev/null <<'EOF'
STATUS_ADMIN_PASS_HASH=scrypt:32768:8:1$<salt>$<hash>
STATUS_ADMIN_USER=admin
STATUS_SECRET_KEY=__SECRETSUBSTITUTED__
PYTHONUNBUFFERED=1
EOF
# (replace __SECRETSUBSTITUTED__ with: python3 -c "import secrets; print(secrets.token_hex(32))")
sudo chmod 0640 /etc/status-page/env
```

### Step 5 — Create the systemd unit file
```bash
sudo tee /etc/systemd/system/status-page.service > /dev/null <<'EOF'
[Unit]
Description=Status My Page Web Application
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=statuspage
Group=statuspage
WorkingDirectory=/opt/status-my-page/.app
ExecStart=/opt/status-my-page/.app/.venv/bin/gunicorn \
    --bind 0.0.0.0:8920 \
    --workers 2 \
    --timeout 30 \
    --access-logfile /opt/status-my-page/.app/logs/access.log \
    --error-logfile /opt/status-my-page/.app/logs/error.log \
    app:app
Restart=on-failure
RestartSec=5
EnvironmentFile=/etc/status-page/env

# Security hardening (full, not strict: the admin API writes config.yaml backups)
NoNewPrivileges=true
ProtectSystem=full
PrivateTmp=true
ReadWritePaths=/opt/status-my-page/.app/instance /opt/status-my-page/.app/logs /opt/status-my-page/.app/archives

[Install]
WantedBy=multi-user.target
EOF
```

### Step 6 — Enable and start the service
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now status-page
sudo systemctl status status-page
```

---

## 4. Reverse Proxy Configuration

**Important:** The app binds to `0.0.0.0:8920` (all interfaces). Place Nginx or Caddy in front for HTTPS termination and TLS management, and use a firewall to restrict direct access if needed.

### Nginx Configuration

```nginx
server {
    listen 443 ssl http2;
    server_name status.yourdomain.com;

    # SSL certificates (use letsencrypt)
    ssl_certificate     /etc/letsencrypt/live/status.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/status.yourdomain.com/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    # Security headers (extra, in addition to what Flask sets)
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;

    location / {
        proxy_pass http://127.0.0.1:8920;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;  # Critical for secure cookies!
    }
}
```

> **Important — proxy trust:** the app ignores `X-Forwarded-For` unless
> `STATUS_TRUST_PROXY=1` is set in `/etc/status-page/env`. With this nginx
> config in front, add it so access logs and login rate-limiting see real
> client IPs instead of `127.0.0.1`:
>
> ```bash
> echo 'STATUS_TRUST_PROXY=1' | sudo tee -a /etc/status-page/env
> sudo systemctl restart status-page
> ```
>
> Only enable it when a trusted proxy always sets the header; on a directly
> exposed server, leaving it off prevents clients from spoofing IPs.

# Redirect HTTP → HTTPS automatically
server {
    listen 80;
    server_name status.yourdomain.com;
    return 301 https://$host$request_uri;
}
```

### Caddy Configuration (Simpler — Auto-HTTPS)

```caddyfile
status.yourdomain.com {
    reverse_proxy 127.0.0.1:8920

    header {
        X-Content-Type-Options "nosniff"
        X-Frame-Options "DENY"
    }
}
```

Caddy will automatically obtain and renew Let's Encrypt SSL certificates for `status.yourdomain.com`.

### Proxy Configuration Notes

- **X-Forwarded-Proto** must be set so Flask knows the request is over HTTPS — this triggers secure cookie behavior
- The WSGI app runs behind Gunicorn, not direct Flask development server

---

## 5. Docker Deployment

A production-ready `Dockerfile` is included in the repo root (Python 3.14-slim,
`curl` + `iputils-ping` for healthcheck support, dedicated non-root
`statuspage` user, `.dockerignore` excludes instance data and logs). If you
maintain your own, mirror these requirements — in particular the image needs
`curl` and the `ping` binary or the healthcheck worker will fail.

```dockerfile
FROM python:3.14-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        iputils-ping \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 statuspage
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt python-dotenv

COPY --chown=statuspage:statuspage . .

RUN mkdir -p instance logs archives && \
    chown -R statuspage:statuspage /app

USER statuspage
EXPOSE 8920

ENV PYTHONUNBUFFERED=1 \
    STATUS_PORT=8920

CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:8920", "--workers", "2", "--threads", "4"]
```

Build and run:
```bash
docker build -t status-my-page .
docker run -d --name status-page \
  -p 127.0.0.1:8920:8920 \
  -v /var/status-data:/app/instance \
  -v ./logs:/app/logs \
  -e STATUS_ADMIN_PASS_HASH='scrypt:32768:8:1$<salt>$<hash>' \
  status-my-page
```

Notes:

- Don't bother passing `STATUS_SECRET_KEY`: the app now persists its own
  key in `instance/.secret_key` (mode 0600, created once and shared by all
  gunicorn workers), so sessions survive restarts as long as the
  `instance` volume is persistent — which it is in the example above.
- Healthchecks probe real network targets; a container without network
  access to your targets will flap red.

---

## 6. SSL/TLS Certificate Management

Using Let's Encrypt with Certbot (for Nginx):

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d status.yourdomain.com
```

Automate renewal (certbot does this automatically for systemd-managed nginx, but verify):
```bash
# Test renewal dry-run
sudo certbot renew --dry-run

# Check current certificate expiry
echo | openssl s_client -connect status.yourdomain.com:443 -servername status.yourdomain.com 2>/dev/null | openssl x509 -noout -dates
```

---

## 7. Monitoring & Logging

### Application Logs

All logs go to the `logs/` directory:
- `logs/server.log` — Flask/Werkzeug server stdout (development)
- `logs/access.log` — Gunicorn access log (production, via systemd redirect)
- `logs/app.log` — Application events: login ok/failed/rate-limited (with client IP + User-Agent), Slack flush results
- `logs/access.log` (application) — Structured per-request lines with client IP (X-Forwarded-For aware), browser/OS summary, method, path, status, duration; rotates at 5 MB × 3 backups

**Slack integration** (optional): set `slack.enabled: true` and
`slack.webhook_url` in config.yaml (or `STATUS_SLACK_WEBHOOK_URL` env var).
Status changes queue to a persistent outbox; one digest message posts on
admin logout. See [configuration.md](./configuration.md#slack-notifications-optional).
- `logs/error.log` — Gunicorn error log (production)

### Monitoring with Prometheus/Grafana

For scrape-based monitoring, point at the JSON endpoint `GET /api/status`
(lightweight, no auth, returns each service's `id`/`name`/`status`/`notes`)
rather than the HTML `/` page. There is no `/metrics` exporter.

```yaml
# prometheus.yml
scrape_configs:
  - job_name: 'status-page'
    static_configs:
      - targets: ['localhost:8920']
    # Up/down + response-time probing; derive status metrics from
    # GET /api/status responses in a recording rule or textfile collector.
    metrics_path: /api/status
    scrape_interval: 30s
```

### Log Rotation

The app writes its own rotating file logs via `logging_setup.py`:
`logs/server.log` and `logs/error.log`, **5 MB per file × 3 backups**
(`RotatingFileHandler(maxBytes=5*1024*1024, backupCount=3)`). No restart or
manual rotation is needed — the handler rotates automatically when a file
exceeds 5 MB.

Under systemd the journal covers gunicorn's own stderr; check sizing with:
```bash
journalctl --disk-usage -u status-page
# Rotate the app's file logs manually if you need to
sudo systemctl restart status-page
```

---

## 8. Backup & Recovery

### Automatic Archival

On every server restart, a JSON snapshot of the database state is saved to `archives/`. Snapshots are pruned automatically — only the most recent **50** are kept (oldest deleted first), so frequent restarts cannot fill the disk. `./cleanup.sh prune --keep N` still works for manual control:

```bash
# List all archived snapshots
./cleanup.sh list

# View historical details of a specific snapshot
./cleanup.sh show 20260813_091523

# Generate outage summary report
./cleanup.sh report

# Prune old archives (keep last 2 by default)
./cleanup.sh prune

# Keep last N snapshots instead
./cleanup.sh prune --keep 10
```

### Manual Backup Commands

```bash
# Backup SQLite database
cp instance/status.db ~/status-backup/$(date +%Y%m%d_%H%M%S).db

# Backup config file and runtime state
cp config.yaml ~/status-backup/$(date +%Y%m%d_%H%M%S)-config.yaml.bak

# Full application backup (everything)
tar czf status-page-full-backup.tar.gz \
  --exclude='.venv' \
  --exclude='logs/' \
  .
```

### Disaster Recovery

If the database becomes corrupted:
1. Restore from latest archive: `./cleanup.sh show <timestamp> | python3 -c "import sys,json,re; data=json.loads(sys.stdin.read()); print(json.dumps(data,indent=2))"`
2. Or restore from backup: `cp ~/status-backup/*.db instance/status.db`
3. Restart the service: `sudo systemctl restart status-page`

---

## 9. Troubleshooting

### Service won't start

```bash
# Check journal logs for errors
sudo journalctl -u status-page --no-pager -n 50

# Verify credentials file exists and has correct permissions
ls -la /etc/status-page/env
stat /etc/status-page/env

# Check that the port is available
ss -tlnp | grep 8920

# Manually test Flask app (as statuspage user)
sudo -u statuspage -g statuspage python3 -c "import app; print('App loads OK')"
```

### Authentication failures

```bash
# Verify the hash stored in config/env matches what your client sends
python3 -c "
from werkzeug.security import check_password_hash
expected_hash = open('/etc/status-page/env').read().split()[0].split('=')[1]
print(check_password_hash(expected_hash, 'your-plaintext-password'))  # Should print True/False
"

# Reset admin password (generate new hash first)
python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('new-password'))"
```

### HTTP 502 Bad Gateway (Nginx → Flask)

This means Nginx can't connect to Gunicorn. Common causes:
- Gunicorn crashed or didn't start — check `journalctl -u status-page`
- Wrong bind address in systemd unit — must be `0.0.0.0:8920` (or `127.0.0.1:8920` if running behind a local reverse proxy)
- SELinux/AppArmor blocking — temporarily disable to test: `sudo setenforce 0`

### High memory usage on server restart

On first boot after seeding a large number of services, `_load_runtime()` loads everything into memory. If you have >500 service items, monitor peak RSS with `ps aux --sort=-%mem | head`:
```bash
# Kill and restart if stuck
sudo systemctl kill status-page
sudo systemctl start status-page
```

---

*Document version: 1.3 | Last updated: 2026-08-27 | Author: Simar Sahni*
