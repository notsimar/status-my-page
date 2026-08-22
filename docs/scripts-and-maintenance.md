# Installation Scripts & Maintenance Reference

## Table of Contents

- [1. Shell Script Inventory](#1-shell-script-inventory)
- [2. start.sh — Development Server Launcher](#2-startsh--development-server-launcher)
- [3. stop.sh / restart.sh — Process Management](#3-stopsh--restartsh--process-management)
- [4. rebuild.sh — Full Installation & Migration](#4-rebuildsh--full-installation--migration)
- [5. install.sh — Production Deploy Wizard](#5-installsh---production-deploy-wizard)
- [6. cleanup.sh — Archive Manager](#6-cleanupsh--archive-manager)
- [7. Maintenance Procedures](#7-maintenance-procedures)

---

## 1. Shell Script Inventory

| Script | Purpose | User Privilege | Key Files Modified |
|--------|---------|----------------|--------------------|
| `start.sh` | Launch dev server with PID tracking & log capture | None (user) | Creates `.server-pid`, `logs/server.log` |
| `stop.sh` | Graceful shutdown via PID file | sudo for systemd, none for dev | Reads `.server-pid` or uses `kill $(pgrep -f gunicorn)` |
| `restart.sh` | Kill existing process then start fresh | None (user) | Uses stop.sh + start.sh chain |
| `rebuild.sh` | Full dependency install + DB migration + server restart | sudo for system deps | Installs venv, runs migrations in app.py |
| `install.sh` | One-command production deploy wizard | Root/sudo | System user, systemd unit, /etc/status-page/env |
| `cleanup.sh` | Archive management (list/show/prune/report) | None (user) | Reads/writes `archives/` JSON snapshots |
| `change_password.sh` | Securely reset/update admin password hash in env | None (user) | Updates `.env.local` / `.env` (`STATUS_ADMIN_PASS_HASH`) |


---

## 2. start.sh — Development Server Launcher

### Purpose

Starts the Flask application for local development with process tracking and structured logging.

### Execution Flow

```
start.sh
    │
    ├── Check if server already running (read .server-pid)
    │   └── YES → display "Server is already running" → exit 1
    │
    ├── Create logs/ directory if missing
    │   └── mkdir -p logs
    │
    ├── Determine Python venv location
    │   ├── Prefer .venv/bin/python (local)
    │   └── Fall back to system python3 if .venv not found
    │
    ├── Apply environment variables from .env file or system
    │   └── Source: .env file (if present, dotenv-style parsing)
    │
    ├── Launch Gunicorn or Flask dev server
    │   ├── Production mode: gunicorn app:app --bind 0.0.0.0:8920
    │   └── Dev mode: python -m flask run --port 8920
    │
    ├── Write PID to .server-pid file for stop.sh
    │   └── echo $! > .server-pid
    │
    └── Log startup message
        └── echo "[start] Server started on PID $!" >> logs/server.log
```

### Configuration Overrides

Environment variables are loaded from:
1. `.env` file (dotenv format if present) — NOT tracked in version control via .gitignore
2. System environment variables
3. Defaults defined in `app.config`

### Safety Features

- **PID tracking**: Written to `.server-pid` so stop.sh can gracefully terminate
- **Port conflict detection**: Tries default port 8920 first, reports if occupied
- **Logging**: All server output captured to `logs/server.log`, preventing terminal noise

---

## 3. stop.sh / restart.sh — Process Management

### stop.sh

#### For Development (PID file mode)
```bash
if [ -f .server-pid ]; then
    PID=$(cat .server-pid)
    kill $PID 2>/dev/null || echo "No process found"
    rm -f .server-pid
fi
```

#### For Production (systemd mode)
```bash
sudo systemctl stop status-page
```

The script auto-detects whether a PID file exists (development) or systemd service is present (production).

### restart.sh

Chains stop.sh then start.sh:
```bash
./stop.sh
sleep 1          # Grace period for socket cleanup
./start.sh
```

For systemd-managed production:
```bash
sudo systemctl restart status-page
```

---

## 4. rebuild.sh — Full Installation & Migration

### Purpose

Performs a complete dependency reinstall, database migration, and server restart. Used when updating the application code or fixing corrupted dependencies.

### Execution Flow

```
rebuild.sh
    │
    ├── Install system dependencies (apt/dnf/yum auto-detect)
    │   └── python3, python3-venv, gunicorn
    │
    ├── Create Python virtual environment
    │   └── python3 -m venv .venv
    │
    ├── Activate venv & install pip dependencies
    │   └── pip install -r requirements.txt  (flask + pyyaml)
    │
    ├── Run any pending database migrations
    │   └── Calls init_db() which CREATEs tables IF NOT EXISTS
    │       (idempotent — safe to run repeatedly)
    │
    ├── Verify config.yaml is valid YAML
    │   └── python -c "import yaml; yaml.safe_load(open('config.yaml'))"
    │
    └── Restart server
        └── ./restart.sh
```

### Use Cases

- After pulling new app.py code with dependency changes
- When SQLite database schema needs migration (new tables added via CREATE TABLE IF NOT EXISTS)
- Before fresh deployments to verify all components work end-to-end
- Post-hardware upgrades to rebuild venv on new architecture

---

## 5. install.sh — Production Deploy Wizard

### Purpose

Automated, interactive deployment wizard for turning a bare Linux server into a production-ready instance of the app. Runs as root via sudo.

### Interactive Prompts & Defaults


