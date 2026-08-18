#!/usr/bin/env bash
# ==============================================================================
# Database Backup & Restore Wrapper for status-my-page
# ==============================================================================

set -euo pipefail

# Resolve script & project root directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PYTHON_BIN="${PROJECT_ROOT}/.venv/bin/python3"
BACKUP_SCRIPT="${PROJECT_ROOT}/scripts/backup_db.py"

# Fallback to system python3 if venv is missing
if [ ! -x "${PYTHON_BIN}" ]; then
    PYTHON_BIN="python3"
fi

if [ ! -f "${BACKUP_SCRIPT}" ]; then
    echo "Error: Backup script not found at ${BACKUP_SCRIPT}" >&2
    exit 1
fi

show_help() {
    cat << 'EOF'
Usage: ./scripts/backup.sh [COMMAND|OPTION] [ARGUMENTS...]

Wrapper for status-my-page SQLite database backup, restore, and prune operations.

Commands & Options:
  (no args)             Create a new database backup (default retention: 30 days)
  -r, --retention DAYS  Create a backup and prune older than DAYS (e.g. -r 7)
  -l, --list            List all available backup files with size and age
  -p, --prune [DAYS]    Prune backups older than DAYS (default: 30 days) without creating a new backup
  --restore FILENAME    Restore live database from a specific backup file (interactive confirmation)
  -h, --help            Display this help message and exit

Examples:
  ./scripts/backup.sh                    # Standard backup with 30-day retention
  ./scripts/backup.sh -r 14              # Backup + keep last 14 days
  ./scripts/backup.sh --list             # Show all stored backups
  ./scripts/backup.sh -p 7               # Clean up backups older than 7 days
  ./scripts/backup.sh --restore status_20260818_134532.db
EOF
}

# Parse options
case "${1:-}" in
    -h|--help|help)
        show_help
        exit 0
        ;;
    -l|--list|list)
        exec "${PYTHON_BIN}" "${BACKUP_SCRIPT}" --list
        ;;
    -p|--prune|prune)
        shift || true
        if [ $# -ge 1 ] && [[ "$1" =~ ^[0-9]+$ ]]; then
            exec "${PYTHON_BIN}" "${BACKUP_SCRIPT}" --prune --retention "$1"
        else
            exec "${PYTHON_BIN}" "${BACKUP_SCRIPT}" --prune "$@"
        fi
        ;;
    --restore|restore)
        shift || true
        if [ $# -lt 1 ]; then
            echo "Error: --restore requires a backup filename argument." >&2
            echo "Run '$0 --list' to see available backups." >&2
            exit 1
        fi
        exec "${PYTHON_BIN}" "${BACKUP_SCRIPT}" --restore "$1"
        ;;
    -r|--retention)
        shift || true
        if [ $# -lt 1 ]; then
            echo "Error: --retention requires a number of days." >&2
            exit 1
        fi
        exec "${PYTHON_BIN}" "${BACKUP_SCRIPT}" --retention "$1"
        ;;
    *)
        # Forward any other arguments directly to backup_db.py
        exec "${PYTHON_BIN}" "${BACKUP_SCRIPT}" "$@"
        ;;
esac
