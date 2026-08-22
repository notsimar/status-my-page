"""Healthcheck integration for status-my-page.

Connects the healthcheck module with the app's configuration and database.
"""

import threading

from statuspage.config import (
    get_base_dir,
    get_db_path,
    get_config_path,
    load_config,
)
from constants import MAX_HISTORY_PER_ITEM

# Healthcheck thread reference
_HEALTHCHECK_THREAD: threading.Thread | None = None
_HEALTHCHECK_START_LOCK = threading.Lock()
_MODULE_CONFIGURED = False


def is_configured() -> bool:
    """Whether the underlying healthcheck module has been configured.

    False when started with STATUS_DISABLE_HEALTHCHECKS=1 (dev/test mode) —
    routes must guard on this instead of letting the underlying module's
    RuntimeError surface as an HTTP 500.
    """
    return _MODULE_CONFIGURED


def configure_healthcheck_module() -> None:
    """Initialize healthcheck module with paths from app config. Call once at startup."""
    global _MODULE_CONFIGURED
    import healthcheck as hc
    hc.configure_healthcheck(
        get_base_dir(),
        get_db_path(),
        get_config_path(),
        load_config,
        MAX_HISTORY_PER_ITEM,
    )
    _MODULE_CONFIGURED = True


def start_healthchecks() -> None:
    """Start the healthcheck background daemon thread if not already running."""
    global _HEALTHCHECK_THREAD
    with _HEALTHCHECK_START_LOCK:
        if _HEALTHCHECK_THREAD is not None and _HEALTHCHECK_THREAD.is_alive():
            return
        import healthcheck as hc
        t = threading.Thread(target=hc._healthcheck_worker, daemon=True, name="healthcheck")
        t.start()
        _HEALTHCHECK_THREAD = t


def run_healthchecks_once() -> dict[str, dict]:
    """Public entry-point: runs all healthchecks once (no DB mutation)."""
    import healthcheck as hc
    return hc.run_healthchecks_once()


def get_configured_healthchecks() -> dict[str, dict]:
    """Return all configured healthchecks for API."""
    import healthcheck as hc
    hc_data = hc._parse_healthchecks()
    # Convert set -> list for JSON serializability
    out: dict[str, dict] = {}
    for name, details in hc_data.items():
        d = dict(details)
        if "healthy_codes" in d:
            d["healthy_codes"] = sorted(d["healthy_codes"])
        out[name] = d
    return out