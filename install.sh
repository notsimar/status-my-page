#!/usr/bin/env bash
# install.sh — Deploy status page as an unprivileged user (non-root by default)
# Run as the target user (no sudo required):
#   cd status-page && ./install.sh [INSTALL_DIR]
# Default INSTALL_DIR = $HOME/.local/share/status-page
set -euo pipefail

# Determine the project root directory (where this script lives)
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Allow override via environment variable for testing/flexibility
ROOT_DIR="${STATUS_MY_PAGE_ROOT:-$ROOT_DIR}"

INSTALL_DIR="${1:-$HOME/.local/share/status-page}"
SERVICE_USER="$(whoami)"
SYSTEMD_NAME="status-page.service"

echo ""
echo "=== Validating installation path ==="
case "$INSTALL_DIR" in
    /*) : ;;
    *) echo "ERROR: Install path must be absolute: $INSTALL_DIR"; exit 1 ;;
esac
if echo "$INSTALL_DIR" | grep -q '\.\./'; then
    echo "ERROR: Invalid install path (traversal not allowed): $INSTALL_DIR"
    exit 1
fi
INSTALL_DIR=$(realpath -m "$INSTALL_DIR")
export INSTALL_DIR
echo "Install directory: $INSTALL_DIR"
echo "Service user: $SERVICE_USER"
echo ""

# Preflight
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found. Install python3 + python3-venv first."
    exit 1
fi
PYTHON_VER=$(python3 --version | awk '{print $2}' | cut -d. -f1,2)
echo "Python version: $PYTHON_VER"

# Non-root: skip system package installs
if [ "$(id -u)" -eq 0 ]; then
    echo "⚠️ Running as root. Will attempt system installs and ownership changes."
    ROOT=1
else
    echo "Running as non-root user $SERVICE_USER. Skipping system packages, user creation, systemd."
    ROOT=0
fi
echo ""

echo "=== Creating directories ==="
mkdir -p "$INSTALL_DIR"/{instance,logs,archives}
chmod 0750 "$INSTALL_DIR/archives" 2>/dev/null || true
echo ""

echo "=== Deploying files ==="
cp -r app.py healthcheck.py input_filter.py constants.py config.yaml requirements.txt \
      statuspage/ templates/ static/ tests/ docs/ start.sh stop.sh restart.sh rebuild.sh cleanup.sh install.sh README.md \
      "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR"/*.sh 2>/dev/null || true
echo ""

if [ "$ROOT" -eq 1 ]; then
    chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR"/{instance,logs,archives,config.yaml} 2>/dev/null || true
else
    chmod -R u+rwX "$INSTALL_DIR"
fi
echo ""

echo "=== Python virtual environment ==="
VENV_DIR="$INSTALL_DIR/.venv"
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install -r "$INSTALL_DIR/requirements.txt" --quiet
if [ "$ROOT" -eq 0 ]; then
    chmod -R u+rwX "$VENV_DIR" 2>/dev/null || true
fi
echo ""

# ── Admin credentials ────────────────────────────────────────────
# Prompted FIRST: init_db() seeds the DB via the app, which requires
# STATUS_ADMIN_PASS_HASH to be set — seeding before the env file exists
# would abort the install (set -e) before the user is ever asked.
echo "=== Admin credentials ==="
read -rp "Admin username [admin]: " ADMIN_USER_INPUT
ADMIN_USER="${ADMIN_USER_INPUT:-admin}"
read -rsp "Admin password: " ADMIN_PASS_INPUT
echo ""
if [ -z "$ADMIN_PASS_INPUT" ]; then
    echo "ERROR: Admin password must not be empty."
    exit 1
fi
if [ -t 1 ] && printf '%s' "$ADMIN_PASS_INPUT" | grep -q $'\t'; then
    echo "ERROR: Password contains a tab character (terminal echo artefact). Re-run install."
    exit 1
fi
PASS_HASH="$("$VENV_DIR/bin/python3" -c "from werkzeug.security import generate_password_hash; import sys; pwd=sys.stdin.read(); print(generate_password_hash(pwd.rstrip('\n')))" <<< "$ADMIN_PASS_INPUT")"

SECRET_KEY="$("$VENV_DIR/bin/python3" -c "import secrets; print(secrets.token_hex(32))")"
ENV_FILE="$INSTALL_DIR/.env.local"
mkdir -p "$(dirname "$ENV_FILE")"

if [ -f "$ENV_FILE" ]; then
    # Existing credentials in the install dir: keep them, but tell the user —
    # the prompted values above are NOT applied unless override is explicit.
    echo "⚠️  $ENV_FILE already exists — keeping existing credentials."
    echo "    The prompted password was ignored. Remove $ENV_FILE to re-create it,"
    echo "    or re-run with SP_INSTALL_OVERRIDE_ENV=1 to force the new credentials."
    if [ "${SP_INSTALL_OVERRIDE_ENV:-0}" = "1" ]; then
        echo "    Override active — replacing $ENV_FILE with prompted credentials."
        {
            printf '%s=%s\n' "STATUS_ADMIN_PASS_HASH" "$PASS_HASH"
            printf '%s=%s\n' "STATUS_SECRET_KEY" "$SECRET_KEY"
            printf 'PYTHONUNBUFFERED=1\n'
        } > "$ENV_FILE"
        export _SP_INSTALL_USER="$ADMIN_USER"
        "$VENV_DIR/bin/python3" -c "
import yaml, os
p = os.environ['INSTALL_DIR'] + '/config.yaml'
cfg = yaml.safe_load(open(p))
cfg['admin'] = cfg.get('admin', {})
cfg['admin']['user'] = os.environ['_SP_INSTALL_USER']
open(p, 'w').write(yaml.dump(cfg, default_flow_style=False, sort_keys=False))
"
        echo "    Credentials set: user=$ADMIN_USER (new password)"
    else
        echo "    Keeping existing $ENV_FILE untouched."
    fi
elif [ -f "$ROOT_DIR/.env.local" ]; then
    # Project env (e.g. a dev-only password / healthchecks-disabled flag) would
    # silently override the credentials just prompted — only reuse when the
    # source env actually exports a real admin hash.
    if grep -q '^STATUS_ADMIN_PASS_HASH=..*' "$ROOT_DIR/.env.local" 2>/dev/null; then
        echo "⚠️  Reusing STATUS_ADMIN_PASS_HASH from $ROOT_DIR/.env.local —"
        echo "    the prompted password is NOT used. The new secret key is applied."
        { grep -v '^STATUS_SECRET_KEY=' "$ROOT_DIR/.env.local" || true; } > "$ENV_FILE.tmp"
        printf '%s=%s\n' "STATUS_SECRET_KEY" "$SECRET_KEY" >> "$ENV_FILE.tmp"
        if ! grep -q '^PYTHONUNBUFFERED=' "$ENV_FILE.tmp"; then
            printf 'PYTHONUNBUFFERED=1\n' >> "$ENV_FILE.tmp"
        fi
        mv "$ENV_FILE.tmp" "$ENV_FILE"
        # Keep the admin username in sync with the prompt
        export _SP_INSTALL_USER="$ADMIN_USER"
        "$VENV_DIR/bin/python3" -c "
import yaml, os
p = os.environ['INSTALL_DIR'] + '/config.yaml'
cfg = yaml.safe_load(open(p))
cfg['admin'] = cfg.get('admin', {})
cfg['admin']['user'] = os.environ['_SP_INSTALL_USER']
open(p, 'w').write(yaml.dump(cfg, default_flow_style=False, sort_keys=False))
"
        echo "Credentials set: user=$ADMIN_USER (password from $ROOT_DIR/.env.local)"
    else
        # Source env has no usable hash — fall through to fresh creation.
        echo "Source $ROOT_DIR/.env.local has no STATUS_ADMIN_PASS_HASH — creating a fresh env file."
        {
            printf '%s=%s\n' "STATUS_ADMIN_PASS_HASH" "$PASS_HASH"
            printf '%s=%s\n' "STATUS_SECRET_KEY" "$SECRET_KEY"
            printf 'PYTHONUNBUFFERED=1\n'
        } > "$ENV_FILE"
        if ! grep -q '^STATUS_DISABLE_HEALTHCHECKS=' "$ROOT_DIR/.env.local" 2>/dev/null; then
            : # no dev-only flags inherited; nothing to merge
        fi
        echo "Credentials set: user=$ADMIN_USER (new password)"
    fi
else
    # Fresh install: write prompted credentials
    export _SP_INSTALL_USER="$ADMIN_USER"
    "$VENV_DIR/bin/python3" -c "
import yaml, os
p = os.environ['INSTALL_DIR'] + '/config.yaml'
cfg = yaml.safe_load(open(p))
cfg['admin'] = cfg.get('admin', {})
cfg['admin']['user'] = os.environ['_SP_INSTALL_USER']
open(p, 'w').write(yaml.dump(cfg, default_flow_style=False, sort_keys=False))
"
    {
        printf '%s=%s\n' "STATUS_ADMIN_PASS_HASH" "$PASS_HASH"
        printf '%s=%s\n' "STATUS_SECRET_KEY" "$SECRET_KEY"
        printf 'PYTHONUNBUFFERED=1\n'
    } > "$ENV_FILE"
    echo "Credentials set: user=$ADMIN_USER (new password)"
fi
chmod 0600 "$ENV_FILE"
echo "Env file created at $ENV_FILE"
echo ""

# ── Seed the database ────────────────────────────────────────────
# Runs AFTER the env file exists so init_admin_auth() can resolve the hash.
echo "=== Initialize database ==="
"$VENV_DIR/bin/python3" -c "import sys; sys.path.insert(0, '$INSTALL_DIR'); from app import init_db; init_db()"
echo "Database seeded."
echo ""

if [ "$ROOT" -eq 1 ]; then
    echo "=== Installing systemd service ==="
    cat > "/etc/systemd/system/$SYSTEMD_NAME" << SVCEOF
[Unit]
Description=Status Page Web App
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
Environment="PATH=$VENV_DIR/bin:/usr/local/bin:/usr/bin"
ExecStart=$VENV_DIR/bin/gunicorn --bind 127.0.0.1:8920 --workers 2 --timeout 30 app:app
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
SVCEOF
    systemctl daemon-reload
    systemctl enable "$SYSTEMD_NAME"
    systemctl start "$SYSTEMD_NAME"
    echo "Service running."
else
    echo ""
    echo "Non-root install complete. Start manually:"
    echo "  cd $INSTALL_DIR && ./start.sh"
fi
echo ""

echo "========================================"
echo "  Deployment complete!"
echo "  Install dir: $INSTALL_DIR"
echo "  Admin user: $ADMIN_USER"
echo "========================================"
