#!/usr/bin/env bash
# install.sh — Deploy status page as an unprivileged user
# Run as the target user (no sudo required):
#   cd status-page && ./install.sh [INSTALL_DIR]
# Default INSTALL_DIR = $HOME/.local/share/status-page

set -euo pipefail

INSTALL_DIR="${1:-$HOME/.local/share/status-page}"
SERVICE_USER="$(whoami)"
SYSTEMD_NAME="status-page.service"

echo ""
echo "=== Validating installation path ==="
# Resolve to absolute path and validate
case "$INSTALL_DIR" in
    /*) : ;;  # absolute path OK
    *) echo "ERROR: Install path must be absolute: $INSTALL_DIR"; exit 1 ;;
esac
# Prevent path traversal — reject ../ sequences
if echo "$INSTALL_DIR" | grep -q '\.\.\/'; then
    echo "ERROR: Invalid install path (traversal not allowed): $INSTALL_DIR"
    exit 1
fi
INSTALL_DIR=$(realpath -m "$INSTALL_DIR")
export INSTALL_DIR
echo "Install directory: $INSTALL_DIR"
echo "Service user: $SERVICE_USER"
echo ""

# ---- Pre-flight checks ----
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found. Install it first:"
    echo "  Debian/Ubuntu: apt install -y python3 python3-venv"
    echo "  RHEL/Fedora/CentOS: dnf install -y python3"
    exit 1
fi

PYTHON_VER=$(python3 --version | awk '{print $2}' | cut -d. -f1,2)
echo "Python version: $PYTHON_VER"

# Detect package manager
if command -v apt &>/dev/null; then
    PKG_MGR="apt"
elif command -v dnf &>/dev/null; then
    PKG_MGR="dnf"
elif command -v yum &>/dev/null; then
    PKG_MGR="yum"
else
    echo "ERROR: Unsupported package manager. Install python3 + python3-venv manually."
    exit 1
fi

# ---- Install system deps ----
echo ""
echo "=== Installing system dependencies ==="
# Package manager installs need root — skip gracefully if we are non-root
if [ "$(id -u)" -eq 0 ]; then
    if [ "$PKG_MGR" = "apt" ]; then
        export DEBIAN_FRONTEND=noninteractive
        apt update -qq && apt install -y python3 python3-venv python3-pip gunicorn curl iputils-ping 2>&1 | grep -v "^+" | tail -5
    elif [ "$PKG_MGR" = "dnf" ] || [ "$PKG_MGR" = "yum" ]; then
        $PKG_MGR install -y python3 python3-venv python3-pip python3-gunicorn curl iputils 2>&1 | tail -5
    fi
else
    echo "⚠️ Not running as root — skipping system package installs. Ensure python3, python3-venv, pip, curl are available."
fi

echo ""
echo "=== Creating service user ==="
if [ "$(id -u)" -eq 0 ]; then
    if ! id "$SERVICE_USER" &>/dev/null; then
        useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
        echo "Created user: $SERVICE_USER"
    else
        echo "User already exists: $SERVICE_USER"
    fi
else
    echo "Running as user $SERVICE_USER (non-root), skipping system user creation."
fi

# ---- Deploy files ----
echo ""
echo "=== Deploying to $INSTALL_DIR ==="
mkdir -p "$INSTALL_DIR"/{instance,logs,archives}

# Restrict archive dir (contains JSON snapshots with service state/notes)
chmod 0750 "$INSTALL_DIR/archives"

# Copy everything from current directory (where this script lives)
cp -r app.py healthcheck.py input_filter.py constants.py config.yaml requirements.txt \
      statuspage/ templates/ static/ \
      tests/ docs/ start.sh stop.sh restart.sh rebuild.sh cleanup.sh install.sh README.md \
      "$INSTALL_DIR/"

# Make scripts executable
chmod +x "$INSTALL_DIR"/*.sh 2>/dev/null || true

# Set ownership (best-effort, skip if not root)
if [ "$(id -u)" -eq 0 ]; then
    chown -R root:"$SERVICE_USER" "$INSTALL_DIR"
    chown -R "$SERVICE_USER":"$SERVICE_USER" \
        "$INSTALL_DIR" \
        "$INSTALL_DIR/config.yaml" \
        "$INSTALL_DIR/instance" \
        "$INSTALL_DIR/logs" \
        "$INSTALL_DIR/archives"
else
    echo "Non-root: keeping existing ownership, ensuring write access for $SERVICE_USER"
    chmod -R u+rwX "$INSTALL_DIR"
fi

# ---- Create Python venv ----
echo ""
echo "=== Creating Python virtual environment ==="
VENV_DIR="$INSTALL_DIR/.venv"
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi

echo "Installing Python dependencies…"
"$VENV_DIR/bin/pip" install --upgrade pip --quiet 2>/dev/null || true
"$VENV_DIR/bin/pip" install -r "$INSTALL_DIR/requirements.txt" --quiet 2>&1 | tail -3

# Fix permissions after venv install
if [ "$(id -u)" -eq 0 ]; then
    chown -R root:root "$VENV_DIR" 2>/dev/null || true
    chmod -R a+rX "$VENV_DIR" 2>/dev/null || true
    chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR"/{instance,logs} 2>/dev/null || true
else
    chmod -R u+rwX "$VENV_DIR" 2>/dev/null || true
fi

# ---- Initialize DB ----
echo ""
echo "=== Initializing database ==="
"$VENV_DIR/bin/python3" -c "import sys; sys.path.insert(0, '$INSTALL_DIR'); from app import init_db; init_db()"
echo "Database seeded with config items."

# ---- Generate password hash ----
echo ""
echo "=== Setting admin credentials ==="
read -rp "Admin username [admin]: " ADMIN_USER_INPUT
ADMIN_USER="${ADMIN_USER_INPUT:-admin}"

read -rsp "Admin password: " ADMIN_PASS_INPUT
echo ""

# Pass password via stdin to Python (no shell interpolation of user input)
PASS_HASH=$("$VENV_DIR/bin/python3" -c "
from werkzeug.security import generate_password_hash
import sys
pwd = sys.stdin.read()
print(generate_password_hash(pwd.rstrip('\n')))
" <<< "$ADMIN_PASS_INPUT")

# Write new admin user into config.yaml — use env vars to avoid shell injection
export _SP_INSTALL_USER="$ADMIN_USER"
export INSTALL_DIR
"$VENV_DIR/bin/python3" -c "
import yaml, os
cfg = yaml.safe_load(open(os.environ['INSTALL_DIR'] + '/config.yaml'))
cfg['admin']['user'] = os.environ['_SP_INSTALL_USER']
yaml.dump(cfg, open(os.environ['INSTALL_DIR'] + '/config.yaml', 'w'), default_flow_style=False)
"

echo "Credentials set: user=$ADMIN_USER"

SECRET_KEY=$("$VENV_DIR/bin/python3" -c "import secrets; print(secrets.token_hex(32))")

# ---- Create credentials env file (restricted permissions) ----
ENV_FILE="$HOME/.config/status-page/env"
mkdir -p "$(dirname "$ENV_FILE")"
cat > "$ENV_FILE" << ENVEOF
STATUS_ADMIN_PASS_HASH=$PASS_HASH
STATUS_SECRET_KEY=$SECRET_KEY
PYTHONUNBUFFERED=1
ENVEOF
chmod 0600 "$ENV_FILE"
echo "Env file created at $ENV_FILE (user-local)"

# ---- Create systemd service ----
echo ""
echo "=== Installing systemd service ($SYSTEMD_NAME) ==="
if [ "$(id -u)" -eq 0 ]; then
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
    echo "Service enabled. Starting…"
    systemctl start "$SYSTEMD_NAME"
else
    echo "⚠️ Non-root: skipping systemd service install. You can run manually:"
    echo "  $VENV_DIR/bin/gunicorn --bind 127.0.0.1:8920 --workers 2 --timeout 30 app:app"
fi

# ---- Verify ----
echo ""
echo "=== Verification ==="
sleep 1
if [ "$(id -u)" -eq 0 ] && systemctl is-active --quiet "$SYSTEMD_NAME"; then
    echo "✅ $SYSTEMD_NAME is running"
    
    # Quick HTTP check
    HTTP_CODE=$(curl -sf -o /dev/null -w "%{http_code}" http://localhost:8920/ 2>/dev/null || echo "000")
    if [ "$HTTP_CODE" = "200" ]; then
        echo "✅ Status page responding (HTTP $HTTP_CODE)"
    else
        echo "⚠️ HTTP check returned $HTTP_CODE — check logs with: journalctl -u $SYSTEMD_NAME -f"
    fi
    
    echo ""
    echo "========================================"
    echo "  Deployment complete!"
    echo "  URL: http://<server-ip>:8920/"
    echo "  Admin: $ADMIN_USER / <your password>"
    echo ""
    echo "  Commands:"
    echo "    systemctl status $SYSTEMD_NAME  → check status"
    echo "    systemctl restart $SYSTEMD_NAME  → restart after config changes"
    echo "    journalctl -u $SYSTEMD_NAME -f   → live logs"
    echo "========================================"
else
    if [ "$(id -u)" -ne 0 ]; then
        echo "Non-root install complete. Start manually:"
        echo "  source $ENV_FILE"
        echo "  $VENV_DIR/bin/gunicorn --bind 127.0.0.1:8920 --workers 2 --timeout 30 app:app"
    else
        echo "❌ Service failed to start. Check logs:"
        echo "   journalctl -u $SYSTEMD_NAME -n 50"
        exit 1
    fi
fi
