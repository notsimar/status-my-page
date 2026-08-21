"""Configuration management for status-my-page.

Handles loading, saving, and backup rotation of config.yaml.
"""

import os
import shutil
import tempfile
import threading
from pathlib import Path

import yaml

from constants import (
    NUM_CONFIG_BACKUPS,
    CONFIG_FILENAME,
    ARCHIVE_DIR_NAME,
    INSTANCE_DIR_NAME,
    DB_FILENAME,
)

# Module-level config path (set by init_config_paths)
CONFIG_PATH: Path | None = None
BASE_DIR: Path | None = None
DB_PATH: Path | None = None
ARCHIVES_DIR: Path | None = None
STATIC_DIR: Path | None = None

# Config cache
_cfg_cache: dict | None = None
_ITEM_NAMES: list[str] = []
_CFG_ADMIN_USER = "admin"
_SERVER_HOST = "0.0.0.0"
_SERVER_PORT = 8920
_SECRET_KEY_ENV = "STATUS_SECRET_KEY"
_LOGO_PATH: str | None = None

_CONFIG_LOCK = threading.Lock()


def init_config_paths(base_dir: Path) -> None:
    """Initialize module-level paths. Call once at startup."""
    global CONFIG_PATH, BASE_DIR, DB_PATH, ARCHIVES_DIR, STATIC_DIR
    BASE_DIR = base_dir
    CONFIG_PATH = BASE_DIR / CONFIG_FILENAME
    DB_PATH = BASE_DIR / INSTANCE_DIR_NAME / DB_FILENAME
    ARCHIVES_DIR = BASE_DIR / ARCHIVE_DIR_NAME
    STATIC_DIR = BASE_DIR / "static"
    _load_config_uncached()


def _read_section(cfg: dict, key: str) -> dict:
    """Return a config section, searching the top level first, then ``_base``.

    ``_save_runtime`` restructures config by moving ``admin``/``server`` under
    ``_base`` on the first mutation. Before that restructure those sections sit
    at the top level. Searching both keeps the getters working in either format,
    so the admin identity and server settings survive a save (without this, a
    non-default admin username silently stops working after the first mutation).
    """
    val = cfg.get(key)
    if isinstance(val, dict) and val:
        return val
    base = cfg.get("_base")
    if isinstance(base, dict) and isinstance(base.get(key), dict):
        return base[key]
    return {}


def _load_config_uncached() -> dict:
    """Load config.yaml without caching. Used at startup."""
    global _cfg_cache, _ITEM_NAMES, _CFG_ADMIN_USER, _SERVER_HOST, _SERVER_PORT, _SECRET_KEY_ENV, _LOGO_PATH
    if CONFIG_PATH is None:
        raise RuntimeError("Config paths not initialized. Call init_config_paths() first.")
    try:
        with open(CONFIG_PATH) as f:
            _cfg_cache = yaml.safe_load(f)
    except FileNotFoundError:
        _cfg_cache = {}
    if _cfg_cache is None:
        _cfg_cache = {}

    _ITEM_NAMES = _cfg_cache.get("items", [])
    admin_sec = _read_section(_cfg_cache, "admin")
    server_sec = _read_section(_cfg_cache, "server")
    _CFG_ADMIN_USER = admin_sec.get("user", "admin")
    _SERVER_HOST = server_sec.get("host", "0.0.0.0")
    _SERVER_PORT = server_sec.get("port", 8920)
    _SECRET_KEY_ENV = server_sec.get("secret_key_env", "STATUS_SECRET_KEY")
    _LOGO_PATH = _cfg_cache.get("logo", {}).get("path")
    return _cfg_cache


def load_config() -> dict:
    """Return config (always reads from disk to reflect changes)."""
    return _load_config_uncached()


def reload_config() -> dict:
    """Force reload config from disk (alias for load_config)."""
    return _load_config_uncached()


# ── Public accessors ────────────────────────────────────────────────

def get_item_names() -> list[str]:
    return _ITEM_NAMES


def get_admin_user() -> str:
    return _CFG_ADMIN_USER


def get_server_host() -> str:
    return _SERVER_HOST


def get_server_port() -> int:
    return _SERVER_PORT


def get_secret_key_env() -> str:
    return _SECRET_KEY_ENV


def get_base_dir() -> Path:
    if BASE_DIR is None:
        raise RuntimeError("Config paths not initialized. Call init_config_paths() first.")
    return BASE_DIR


def get_config_path() -> Path:
    if CONFIG_PATH is None:
        raise RuntimeError("Config paths not initialized. Call init_config_paths() first.")
    return CONFIG_PATH


def get_db_path() -> Path:
    if DB_PATH is None:
        raise RuntimeError("Config paths not initialized. Call init_config_paths() first.")
    return DB_PATH


def get_archives_dir() -> Path:
    if ARCHIVES_DIR is None:
        raise RuntimeError("Config paths not initialized. Call init_config_paths() first.")
    return ARCHIVES_DIR


def get_static_dir() -> Path:
    if STATIC_DIR is None:
        raise RuntimeError("Config paths not initialized. Call init_config_paths() first.")
    return STATIC_DIR


def _resolve_logo_rel() -> str | None:
    """Normalize ``logo.path`` to a static-dir-relative path, or None.

    Shared guard for both the URL and local-path resolvers: rejects empty
    values and any ``..`` traversal; accepts ``/static/x`` or ``static/x``
    input. Single source of truth so the two resolvers can't drift.
    """
    if not _LOGO_PATH:
        return None
    rel = str(_LOGO_PATH).strip().lstrip("/")
    if not rel or ".." in Path(rel).parts:
        return None
    if rel.startswith("static/"):
        rel = rel[len("static/"):]
    return rel


def get_logo_url() -> str:
    """Return the public URL for the configured logo, or "" if not configured.

    ``logo.path`` in config.yaml may be given as a path relative to the
    static dir (``logos/my-logo.png``) or as a full URL (``/static/my-logo.png``);
    both normalize to ``/static/...``. Traversal (``..``) or empty values
    refuse to resolve — an attacker-controlled ``logo.path`` must not be able
    to point the page at another path on the static server.
    """
    rel = _resolve_logo_rel()
    return f"/static/{rel}" if rel else ""


def get_logo_local_path() -> Path | None:
    """Absolute filesystem path of the configured logo, or None.

    Single source of truth for resolving ``logo.path`` to a real file: it must
    exist, be non-empty, and (after any ``static/`` prefix is stripped) stay
    within the static dir with no ``..`` traversal. The static-HTML exporter
    uses this to inline the logo as a data URI.
    """
    if STATIC_DIR is None:
        return None
    rel = _resolve_logo_rel()
    if rel is None:
        return None
    candidate = (STATIC_DIR / rel).resolve()
    # Containment + existence checks
    try:
        static_root = STATIC_DIR.resolve()
        within_static = static_root in candidate.parents or candidate.parent == static_root
    except (OSError, RuntimeError):
        within_static = False
    if not within_static:
        return None
    if not candidate.is_file() or candidate.stat().st_size == 0:
        return None
    return candidate


# ── YAML runtime persistence ────────────────────────────────────────

def _rotate_backups() -> None:
    """Rotate backup files: current → bak1, bak1→bak2, ..., bakN-1→bakN.

    Preserves the last N versions of config.yaml on disk so you can recover
    from bad automation or accidental changes. All file ops run under the
    _CONFIG_LOCK (held by callers) for thread safety.
    """
    if CONFIG_PATH is None:
        return
    cfg_base = CONFIG_PATH
    if not cfg_base.exists():
        return

    backup_dir = cfg_base.parent

    # 1. Delete oldest rotation candidate (beyond retention count)
    oldest = backup_dir / f"{cfg_base.name}.bak{NUM_CONFIG_BACKUPS}"
    if oldest.exists():
        oldest.unlink()

    # 2. Shift existing backups upward: bak4→bak5, bak3→bak4, …, bak1→bak2
    for i in range(NUM_CONFIG_BACKUPS - 1, 0, -1):
        src = backup_dir / f"{cfg_base.name}.bak{i}"
        dst = backup_dir / f"{cfg_base.name}.bak{i + 1}"
        if src.exists():
            src.rename(dst)

    # 3. Save current config.yaml as bak1 (before the new write overwrites it)
    bak1 = backup_dir / f"{cfg_base.name}.bak1"
    shutil.copy2(str(cfg_base), str(bak1))

    # 4. Backups mirror the live config, which can hold secrets (admin hash
    #    via inline YAML on some deployments); never leave them world-read-
    #    able. Reassert 0600 on every rotated copy (rename preserves mode,
    #    but installs predating this fix may have 0644 files).
    import stat as _stat
    for bak in sorted(backup_dir.glob(f"{cfg_base.name}.bak*")):
        try:
            bak.chmod(_stat.S_IRUSR | _stat.S_IWUSR)  # 0600
        except OSError:
            pass


def _load_runtime() -> dict:
    """Return empty dict (runtime state is maintained in SQLite database)."""
    return {}


def _save_runtime(data: dict) -> None:
    """No-op: config.yaml is read-only input, runtime state is maintained in DB."""
    pass


# ── Healthchecks config persistence ────────────────────────────────

# Guards load-modify-save sequences on the healthchecks section. The save
# itself is locked by _CONFIG_LOCK, but callers that read-then-write need
# this outer lock so concurrent admin requests can't lose updates.
HEALTHCHECKS_CFG_LOCK = threading.Lock()


def _load_healthchecks() -> dict:
    """Return raw healthchecks dict from config.yaml (no parsing/sanitisation)."""
    data = load_config()
    return data.get("healthchecks", {}) or {}


def _save_healthchecks(healthchecks: dict) -> None:
    """Atomically write healthchecks section into config.yaml with backup rotation."""
    with _CONFIG_LOCK:
        # 1. Read current config FIRST (consistent snapshot before any file ops)
        cfg_data = load_config()
        if not isinstance(cfg_data, dict):
            cfg_data = {"items": list(_ITEM_NAMES), "_base": {}}

        # 2. Preserve known top-level keys under _base during a rewrite
        for section in ("admin", "server"):
            if section in cfg_data and section not in cfg_data.get("_base", {}):
                cfg_data.setdefault("_base", {})[section] = cfg_data.pop(section, {})

        # 3. Apply healthchecks data
        cfg_data["healthchecks"] = healthchecks

        # 4. Rotate backups of the ORIGINAL file (before we overwrite it)
        _rotate_backups()

        # 5. Atomic write: temp file + os.replace
        if CONFIG_PATH is None:
            return
        path = CONFIG_PATH
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".config_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                yaml.dump(cfg_data, fh, default_flow_style=False, sort_keys=False)
            os.replace(tmp_path, path)
        finally:
            # Clean up temp file if replace failed
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _load_rss() -> dict:
    """Return raw rss section from config.yaml ({} when absent)."""
    data = load_config()
    sec = data.get("rss")
    return sec if isinstance(sec, dict) else {}


def _save_rss(rss: dict) -> None:
    """Atomically write rss section into config.yaml with backup rotation."""
    _save_section("rss", rss)


def _load_settings() -> dict:
    """Return raw settings section from config.yaml ({} when absent)."""
    data = load_config()
    sec = data.get("settings")
    return sec if isinstance(sec, dict) else {}


def history_enabled() -> bool:
    """Whether the per-service history timeline is exposed (default: off).

    Read on every call so an admin toggle from another browser tab or a
    direct config.yaml edit takes effect without a restart. Opt-in via
    ``settings: {history_enabled: true}`` in config.yaml or the admin UI.
    """
    return bool(_load_settings().get("history_enabled", False))


def healthchecks_enabled() -> bool:
    """Whether automated background healthchecking is enabled (default: true).

    Read dynamically by the worker loop or endpoints. Opt-out via
    ``settings: {healthchecks_enabled: false}`` in config.yaml or the admin UI.
    """
    return bool(_load_settings().get("healthchecks_enabled", True))


def _save_section(section: str, section_data: dict) -> None:
    """Atomically rewrite one top-level config.yaml section (backup rotation)."""
    with _CONFIG_LOCK:
        cfg_data = load_config()
        if not isinstance(cfg_data, dict):
            cfg_data = {"items": list(_ITEM_NAMES), "_base": {}}
        # Preserve known top-level keys under _base during a rewrite
        for key in ("admin", "server"):
            if key in cfg_data and key not in cfg_data.get("_base", {}):
                cfg_data.setdefault("_base", {})[key] = cfg_data.pop(key, {})
        cfg_data[section] = section_data
        _rotate_backups()
        if CONFIG_PATH is None:
            return
        path = CONFIG_PATH
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".config_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                yaml.dump(cfg_data, fh, default_flow_style=False, sort_keys=False)
            os.replace(tmp_path, path)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        # Refresh in-memory logo cache if config changed
        global _LOGO_PATH
        _LOGO_PATH = cfg_data.get("logo", {}).get("path")


def _save_settings(settings: dict) -> None:
    """Atomically write settings section into config.yaml with backup rotation."""
    _save_section("settings", settings)