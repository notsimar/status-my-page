#!/usr/bin/env bash
# dev-setup.sh — Developer quick-start for status-my-page
# Creates a local venv, installs deps, seeds DB, and runs Flask dev server.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
ENV_FILE="$ROOT_DIR/.env.local"

echo "=== status-my-page developer setup ==="
echo "Root: $ROOT_DIR"

if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

echo "Installing requirements..."
"$VENV_DIR/bin/pip" install --upgrade pip >/dev/null
"$VENV_DIR/bin/pip" install -r "$ROOT_DIR/requirements.txt" >/dev/null

echo "Initializing database..."
STATUS_DISABLE_HEALTHCHECKS=1 "$VENV_DIR/bin/python3" -c "
import sys
sys.path.insert(0, '$ROOT_DIR')
from app import init_db
init_db()
print('DB initialized')
"

if [ ! -f "$ENV_FILE" ]; then
    echo "Generating development env..."
    SECRET_KEY=$("$VENV_DIR/bin/python3" -c "import secrets; print(secrets.token_hex(32))")
    PASS_HASH=$("$VENV_DIR/bin/python3" -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('devpassword'))")
    cat > "$ENV_FILE" << EOF
STATUS_DISABLE_HEALTHCHECKS=1
STATUS_ADMIN_PASS_HASH=$PASS_HASH
STATUS_SECRET_KEY=$SECRET_KEY
EOF
    chmod 600 "$ENV_FILE"
fi

echo ""
echo "Setup complete!"
echo ""
echo "To start dev server:"
echo "  source $VENV_DIR/bin/activate"
echo "  export STATUS_ADMIN_PASS_HASH=$(grep STATUS_ADMIN_PASS_HASH $ENV_FILE | cut -d= -f2-)"
echo "  export STATUS_SECRET_KEY=$(grep STATUS_SECRET_KEY $ENV_FILE | cut -d= -f2-)"
echo "  export STATUS_DISABLE_HEALTHCHECKS=1"
echo "  flask --app app run --host 127.0.0.1 --port 8920"
echo ""
echo "Admin user: admin"
echo "Admin password: devpassword (change in $ENV_FILE or config)"
