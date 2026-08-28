# Installation Scripts & Maintenance Reference

## Table of Contents

- [1. Shell Script Inventory](#1-shell-script-inventory)
- [2. dev-setup.sh — Interactive Developer Setup](#2-dev-setupsh--interactive-developer-setup)
- [3. start.sh — Development Server Launcher](#3-startsh--development-server-launcher)
- [4. stop.sh / restart.sh — Process Management](#4-stopsh--restartsh--process-management)
- [5. rebuild.sh — Full Installation & Migration](#5-rebuildsh--full-installation--migration)
- [6. install.sh — Production Deploy Wizard](#6-installsh---production-deploy-wizard)
- [build_release.sh](#build_releasesh)
- [install_logo.sh](#install_logosh)

---

## 1. Shell Script Inventory

| Script | Purpose | User Privilege | Key Files Modified |
|--------|---------|----------------|--------------------|
| `start.sh` | Launch server (gunicorn) with PID tracking & log capture | None (user) | Creates `.server.pid`, `logs/server.log` |
| `stop.sh` | Graceful shutdown via PID file (kills worker children too) | None (user) | Reads `.server.pid` |
| `restart.sh` | stop.sh + 1 s grace period + start.sh | None (user) | Uses stop.sh + start.sh chain |
| `clear_history.sh` | History timeline CLI over the admin API: `list`, `show <id>`, `clear <id>`, `clear-all` (destructive commands confirm unless `--yes`) | None (user) | Deletes `status_history` rows via `POST /api/history/<id>/clear` (no local file changes) |
| `rebuild.sh` | Full dependency install + DB migration + server restart | sudo for system deps | Installs venv, runs migrations in app.py |
| `install.sh` | One-command production deploy wizard (root: systemd + 0.0.0.0; non-root: deploy only) | Root or user | Installs to `$HOME/.local/share/status-page` (or given abs path), `.env.local` (0600), systemd unit in root mode |
| `cleanup.sh` | Archive management (list/show/prune/report) | None (user) | Reads/writes `archives/` JSON snapshots |
| `change_password.sh` | Securely reset/update admin password hash in env | None (user) | Updates `.env.local` / `.env` (`STATUS_ADMIN_PASS_HASH`) |
| `scripts/build_release.sh` | Build clean deployable `dist/*.tar.gz` from git-tracked files | None (user) | Creates `dist/` tarball |
| `scripts/install_logo.sh` | Install customer logos into `static/logos/` + write config.yaml logo section | None (user) | `static/logos/`, `config.yaml` |
| `lib.sh` (sourced, not run) | Shared error-reporting helpers: die/warn/step/ok/run_step/require_cmd | — | — |
| `dev-setup.sh` | Interactive developer quick-start: git pull, venv, prompts, env | None (user) | Creates `.env.local`, venv, `instance/` DB |


---

## 2. dev-setup.sh — Interactive Developer Setup

### Purpose

One-command local development bootstrap. Run it after `git clone` and
again after any `git pull` to (re)configure the workspace. It is
interactive: it syncs the code, creates the virtualenv, installs
dependencies, and **prompts for each setup option** with sensible
defaults, then writes `.env.local`, seeds the DB, and prints the exact
dev-server start command.

### Prompts

| Prompt | Default | Writes |
|--------|---------|--------|
| Admin username | `admin` | `DEV_ADMIN_USER` |
| Admin password (asked only when `.env.local` is absent or reset chosen) | — | `STATUS_ADMIN_PASS_HASH` |
| Dev server port | `8920` | `DEV_PORT` |
| Disable background healthchecks? | `Y` | `STATUS_DISABLE_HEALTHCHECKS` |
| Logo file to install (optional) | blank | `DEV_LOGO_PATH` |

Existing values stored in `.env.local` are offered as defaults, so
re-runs are mostly Enter keys. A stored password is reused unless you
explicitly answer `y` to the reset question.

### Behavior details

- **Git sync:** `git pull --ff-only` runs first; the pull is skipped
  (with a warning) when the working tree has uncommitted changes or the
  checkout is in detached HEAD, so local work is never clobbered.
- **Password handling:** the entered password is piped into Python via
  stdin (never a shell argument or environment variable); the scrypt
  hash is written single-quoted so `source .env.local` cannot truncate
  the `$` characters inside the hash.
- **Validation:** the port must be numeric 1–65535; the password must be
  non-empty and must match its confirmation. Invalid input re-prompts.
- **DB seed:** `init_db()` runs with the freshly written env sourced,
  so `STATUS_ADMIN_PASS_HASH` is present at import time.
- **Logo:** if a path is given, `scripts/install_logo.sh` installs it.
- **Exit codes:** any failed step aborts with `die`, writing details to
  the shared install error log.

### Usage

```bash
./dev-setup.sh
```

### Test coverage

`tests/test_dev_setup.py` drives the script end-to-end with piped stdin:
defaults acceptance, custom values, password mismatch re-prompt, invalid
port re-prompt, declined password reset preserving the stored hash,
single-quoted env values, `600` permissions, and `$`-surviving sourcing.

---

## 3. start.sh — Server Launcher

### Purpose

Launches the status page with gunicorn (matching install.sh's systemd unit) with
PID tracking and log capture.

### Execution Flow

```
start.sh
    │
    ├── Already running? (kill -0 against PID in .server.pid)
    │   └── YES → "Already running (PID …)" → exit 0
    │
    ├── Create logs/ directory if missing
    │   └── mkdir -p logs
    │
    ├── Load environment from .env.local (set by install.sh/dev-setup.sh),
    │   falling back to .env — sourced with `set -a` so values with
    │   spaces/quotes survive export
    │
    └── Launch gunicorn in the background
        └── nohup .venv/bin/gunicorn --bind 0.0.0.0:8920 --workers 2 --timeout 30 app:app
            >> logs/server.log 2>&1 &
        ├── Write PID to .server.pid (for stop.sh)
        └── Print "Running on http://0.0.0.0:8920 (PID $!, gunicorn)"
```

### Notes

- Requires `.venv` — run `./dev-setup.sh` or `./install.sh` first. There is
  no system-python fallback and no automatic port-conflict detection (gunicorn
  will fail loudly in `logs/server.log` if 8920 is taken).
- Server output goes to `logs/server.log`, not the terminal.
- For a browser-facing dev server with the Flask reloader, use
  `flask --app app run --host 127.0.0.1 --port 8920` instead.

---

## 4. stop.sh / restart.sh — Process Management

### stop.sh

```bash
if [ ! -f .server.pid ]; then
    echo "No PID file found."
    exit 0
fi
PID=$(cat .server.pid)
pkill -P "$PID" 2>/dev/null || true   # gunicorn worker children
kill "$PID" 2>/dev/null || true
rm -f .server.pid
```

`stop.sh` is user-level only — it never talks to systemd. For a
systemd-managed production install use `sudo systemctl stop status-page`.

### restart.sh

Chains stop.sh then start.sh (dev mode) or use
`sudo systemctl restart status-page` under systemd:
```bash
./stop.sh 2>/dev/null || true
sleep 1          # Grace period for socket cleanup
./start.sh
```

---

### clear_history.sh

Manages the status-change history timeline **over the admin API** (requires a
running server — `./start.sh` or systemd). Reads `.env.local`/`.env` like
start.sh; authenticates as admin using `HEALTH_PASS` (and optional
`HEALTH_USER`, default `admin`) — same convention as `tests/test_health.sh`.

```
./clear_history.sh list                      # all services: id / entry count / last change
./clear_history.sh show <id>                 # one service's timeline (occurred / event / old -> new)
./clear_history.sh clear <id>                # delete one service's rows (confirmation prompt)
./clear_history.sh clear-all                 # delete every service's rows (confirmation prompt)
./clear_history.sh clear-all --yes           # skip the prompt (CI/scripts)
./clear_history.sh list https://host:8920    # optional server URL (default http://localhost:8920)
```

Notes:

- `list` degrades gracefully when the public history feature is disabled —
  per-service entry count shows `n/a` instead of failing.
- The CSRF token is single-use (rotated per accepted mutation), so it is
  re-fetched before **each** `POST /api/history/<id>/clear`.
- The clear endpoint is admin+CSRF only and is **not** gated by the history
  feature setting — an admin can wipe the timeline while the public view is
  off.
- Exit codes: `0` all clears succeeded, `1` one or more services failed (or
  bad usage / login failure).

---

## 5. rebuild.sh — Full Installation & Migration

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

## 6. install.sh — Production Deploy Wizard

### Purpose

Automated, interactive deployment wizard for turning a bare Linux server into a production-ready instance of the app. Runs as root via sudo.

### Interactive Prompts & Defaults



## build_release.sh

Packages exactly the git-tracked files into
`dist/status-my-page-<version>.tar.gz` (version from git tag, falling back
to short SHA). Extracts under a versioned top-level directory so
`tar -xzf` + `./install.sh` works directly. No venv/logs/env files leak in.

```bash
./scripts/build_release.sh
# -> dist/status-my-page-<version>.tar.gz
```

## install_logo.sh

Installs customer logos into `<install_dir>/static/logos/` and writes the
`logo:` section of config.yaml.

```bash
# Single logo
./scripts/install_logo.sh /path/to/logo.png ~/status

# Dark/light variants
LOGO_DARK=dark.png LOGO_LIGHT=light.png ./scripts/install_logo.sh ~/status

# Point config at the dark variant instead
LOGO_CONFIG_PATH=logos/dark-logo.png ./scripts/install_logo.sh ...
```