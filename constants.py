"""Centralized constants for status-my-page.

All magic numbers, limits, and configuration defaults live here.
"""

# ── Config backup rotation ──────────────────────────────────────────
NUM_CONFIG_BACKUPS = 5  # How many old versions of config.yaml to keep


# ── Database / History limits ───────────────────────────────────────
MAX_HISTORY_PER_ITEM = 100      # Prune older entries per item to bound table growth
HISTORY_RUNTIME_CAP = 25        # History entries persisted to config.yaml per item (survives restarts)


# ── Authentication / Rate limiting ──────────────────────────────────
MAX_LOGIN_ATTEMPTS = 5          # Failures before IP lockout
LOCKOUT_SECONDS = 30            # How long to block that IP after lockout

MUTATION_MAX = 60               # Max mutations per IP within window
MUTATION_WINDOW = 60            # Mutation rate-limit window (seconds)

MAX_CSRF_FAILURES = 3           # Bad CSRF tokens before session wipe


# ── Input validation limits ─────────────────────────────────────────
MAX_TEXT_LENGTH = 500           # Hard cap for free-text fields (notes, etc.)
MAX_NAME_LENGTH = 128           # Service name max length
MAX_USERNAME_LENGTH = 64        # Login username/password per-field max length


# ── Healthcheck defaults ────────────────────────────────────────────
HEALTHCHECK_INTERVAL_DEFAULT = 60    # Seconds between checks
HEALTHCHECK_TIMEOUT_DEFAULT = 10     # Seconds per attempt
HEALTHCHECK_RETRIES_DEFAULT = 2      # Consecutive failures before degraded/red
CURL_MAX_REDIRS = 5                  # Max redirects for curl

# Healthcheck lock stale threshold (seconds)
HEALTHCHECK_LOCK_STALE_SECONDS = 300
HEALTHCHECK_LOCK_REFRESH_SECONDS = 30


# ── Server defaults ─────────────────────────────────────────────────
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8920
DEFAULT_SECRET_KEY_ENV = "STATUS_SECRET_KEY"


# ── Status enum order (for sorting) ─────────────────────────────────
# Red first (worst), then degraded, then green (best)
STATUS_SORT_ORDER = {"red": 0, "degraded": 1, "green": 2}
STATUS_CYCLE = ["green", "degraded", "red"]


# ── Seed items (default services if config empty) ───────────────────
DEFAULT_SEED_ITEMS = [
    "Web Server", "Database", "API Gateway", "CDN", "Auth Service",
    "Payment Processing", "Email Service", "Storage", "Cache Layer",
    "Message Queue", "Search Engine", "ML Pipeline", "Monitoring",
    "Backup System", "DNS",
]


# ── Archive settings ────────────────────────────────────────────────
ARCHIVE_DIR_NAME = "archives"
INSTANCE_DIR_NAME = "instance"
DB_FILENAME = "status.db"
CONFIG_FILENAME = "config.yaml"