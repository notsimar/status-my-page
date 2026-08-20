#!/usr/bin/env bash
# install.sh — Deploy status page as an unprivileged user (non-root by default)
# Run as the target user (no sudo required):
#   cd status-page && ./install.sh [INSTALL_DIR]
# Default INSTALL_DIR = $HOME/.local/share/status-page

set -euo pipefail

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
echo "=== Initialize database ==="
"$VENV_DIR/bin/python3" -c "import sys; sys.path.insert(0, '$INSTALL_DIR'); from app import init_db; init_db()"
echo "Database seeded."

echo ""
echo "=== Admin credentials ==="
read -rp "Admin username [admin]: " ADMIN_USER_INPUT
ADMIN_USER="${ADMIN_USER_INPUT:-admin}"
read -rsp "Admin password: " ADMIN_PASS_INPUT
echo ""
PASS_HASH=$("$VENV_DIR/bin/python3" -c "from werkzeug.security import generate_password_hash; import sys; pwd=sys.stdin.read(); print(generate_password_hash(pwd.rstrip('\n')))" <<< "$ADMIN_PASS_INPUT")

export _SP_INSTALL_USER="$ADMIN_USER"
"$VENV_DIR/bin/python3" -c "
import yaml, os
cfg = yaml.safe_load(open(os.environ['INSTALL_DIR'] + '/config.yaml'))
cfg['admin']['user'] = os.environ['_SP_INSTALL_USER']
open(os.environ['INSTALL_DIR'] + '/config.yaml','w').write(yaml.dump(cfg, default_flow_style=False))
"
echo "Credentials set: user=$ADMIN_USER"

SECRET_KEY=$("$VENV_DIR/bin/python3" -c "import secrets; print(secrets.token_hex(32))")
ENV_FILE="$HOME/.config/status-page/env"
mkdir -p "$(dirname "$ENV_FILE")"
cat > "$ENV_FILE" << ENVEOF
STATUS_ADMIN_PASS_HASH=$PASS_HASH
STATUS_SECRET_KEY=$SECRET_KEY
PYTHONUNBUFFERED=1
ENVEOF
chmod 0600 "$ENV_FILE"
echo "Env file created at $ENV_FILE"

if [ "$ROOT" -eq 1 ]; then
    echo ""
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
ExecStart=$VENV_DIR/bin/gunicorn --bind 127.0.0.1:8920 --workers 2 --timeout 30 app:app
Restart=on-failure
RestartSec=5
EnvironmentFile=$ENV_FILE
Environment="PATH=$VENV_DIR/bin:/usr/local/bin:/usr/bin"

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
    echo "  source $ENV_FILE"
    echo "  $VENV_DIR/bin/gunicorn --bind 127.0.0.1:8920 --workers 2 --timeout 30 app:app"
fi

echo ""
echo "========================================"
echo "  Deployment complete!"
echo "  Install dir: $INSTALL_DIR"
echo "  Admin user: $ADMIN_USER"
echo "========================================"
