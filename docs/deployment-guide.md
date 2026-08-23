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
| Ubuntu/Debian | 20.04 LTS, 22.04 LTS | apt package manager (default for install.sh) |
| Fedora/RHEL/CentOS | 38+ | dnf/yum package manager support in install.sh |
| Arch Linux | Rolling | Manual installation only (install.sh does not auto-detect pacman) |
| macOS | 12+ (Monterey) | Development only; not recommended for production |

**Hardware minimum requirements:**
- CPU: 1 core (any architecture x86_64 or aarch64)
- RAM: 256 MB (app itself uses <100 MB at idle)
- Disk: 500 MB free space (for app files, SQLite DB, logs, archives)

---

## 2. One-Command Installation (Recommended)

The `install.sh` wizard handles everything automatically on fresh Linux servers.

### Prerequisites
- Root or sudo access
- Working internet connection for package installation
- Server reachable at the target network interface

### Installation Steps

```bash
# Clone the repository
git clone https://github.com/notsimar/status-my-page.git ~/status-my-page
cd ~/status-my-page

# Run the installer (as root or with sudo)
sudo ./install.sh
```

Choose a custom install path if desired:
```bash
sudo ./install.sh /srv/status-dashboard
```

### What the Wizard Does

1. Installs system packages (`python3`, `python3-venv`, `gunicorn`) via apt/dnf/yum auto-detect
2. Creates a dedicated `statuspage` system user (no shell, no home directory)
3. Deploys application files to `<install_path>/status-my-page` (default: `/opt/status-my-page`)
4. Creates Python virtual environment and installs dependencies (`flask`, `pyyaml`), plus `curl` and `iputils-ping` for the healthcheck worker
5. Seeds the SQLite database from `config.yaml` service names
6. Prompts for admin credentials interactively (username + password, hashed as scrypt)
7. Writes credentials to `/etc/status-page/env` with mode `0640` (owner read/write only)
8. Creates and installs systemd unit file at `/etc/systemd/system/status-page.service`
9. Starts Gunicorn on `0.0.0.0:8920` behind systemd service manager
10. Verifies health endpoint returns HTTP 200 before declaring success

### Interactive Prompts During Installation

```
=== Setting admin credentials ===
Admin username [admin]: <enter or type your desired username>
Admin password: <type silently — will be hashed as scrypt, not stored as plaintext>

Credentials set: user=<your_username>

=== Installing systemd service (status-page.service) ===
Service enabled. Starting…

=== Verification ===
✅ status-page.service is active and running
✅ Status page responding (HTTP 200)

Deployment complete!
URL: http://<server-ip>:8920/
```

### Post-Installation Commands

```bash
# Check service status
systemctl status status-page

# View live logs
sudo journalctl -u status-page -f

# Restart after config changes (e.g., editing config.yaml)
sudo systemctl restart status-page

# Run post-deploy health check
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
sudo mkdir -p /etc/status-page
sudo tee /etc/status-page/env > /dev/null <<EOF
STATUS_ADMIN_PASS_HASH=scrypt\$72816\$...   # Your generated hash here
STATUS_ADMIN_USER=admin
STATUS_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
PYTHONUNBUFFERED=1
EOF
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

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/status-my-page/.app/instance /opt/status-my-page/.app/logs /opt/status-my-page/.app/archives
PrivateTmp=true

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

Not yet officially supported, but the application is compatible with Docker containerization. Here is a basic `Dockerfile`:

```dockerfile
FROM python:3.12-slim AS runtime

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends nginx && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY . .

# Expose port for reverse proxy (HTTPS terminated externally)
EXPOSE 8920

USER nobody
CMD ["gunicorn", "--bind", "0.0.0.0:8920", "--workers", "2", "--timeout", "30", "app:app"]
```

Build and run:
```bash
docker build -t status-my-page .
docker run -d --name status-page \
  -p 127.0.0.1:8920:8920 \
  -v /var/status-data:/app/instance \
  -v ./logs:/app/logs \
  -e STATUS_ADMIN_PASS_HASH=scrypt\$72816\$... \
  -e STATUS_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))") \
  status-my-page
```

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

A basic scrape target exists at `/` — the full HTML response can be wrapped to extract metrics. For production monitoring:

```yaml
# prometheus.yml
scrape_configs:
  - job_name: 'status-page'
    static_configs:
      - targets: ['localhost:8920']
    metrics_path: /  # Health check endpoint (HTML status page, use only for HTTP response time)
    scrape_interval: 30s
```

### Log Rotation

Using systemd's built-in journal rotation:
```bash
# Check log size
journalctl --disk-usage -u status-page

# Rotate logs if growing too large (default is 10% of /dev/shm or 64MB)
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

*Document version: 1.2 | Last updated: 2026-08-18 | Author: Simar Sahni*
