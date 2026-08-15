# Migration Plan: SQLite → PostgreSQL (Optional) for status-my-page

**Date:** 2026-08-14  
**Branch strategy:** `experimental/postgres-migration → main`  
**Approach:** SQLAlchemy Core — keeps all existing SQL strings intact under a `text()` wrapper; minimal conceptual shift, no ORM.

---

## Why This Is Optional Right Now

| Metric | SQLite (current) | PostgreSQL | Verdict |
|--------|-----------------|------------|---------|
| Data volume | ~12 items + history (capped at 100/item) | Same | No benefit |
| Concurrency | WAL mode handles 2 Gunicorn workers fine | Pooled connections | Marginal benefit |
| Durability | File on disk, WAL journal | Transaction log with WAL | **Only real advantage** |
| Deployment complexity | Zero — file is self-contained | Requires DB server + network | **SQLite wins** |
| Restore speed | Copy one file | pg_dump → psql restore | SQLite wins |

**The single compelling reason to migrate:** crash durability. SQLite on a filesystem without battery-backed cache (e.g., certain cloud VMs, Raspberry Pi) can corrupt the WAL journal under hard power loss. PostgreSQL writes to a transaction log and recovers atomically.

---

## Current State

| Aspect | Detail |
|--------|--------|
| **Driver** | `sqlite3` (stdlib), all raw SQL with `?` params |
| **Tables** | `status_items` (id, name, status, notes, position), `status_history` (id, item_id, event_type, old_value, new_value, occurred) |
| **Connection** | Per-request via Flask's `g`, closed in `teardown_appcontext`. WAL mode PRAGMA on every open. |
| **Schema migration** | Inline `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ADD COLUMN` (try/except) in `init_db()` |
| **SQLite-specific features used** | `PRAGMA journal_mode=WAL`, `sqlite3.Row` row factory, `lastrowid` on INSERT, `COALESCE(MAX(...), 0)`, auto-increment IDs via SQLite's ROWID |
| **Special paths** | `_archive_db_snapshot()` opens a separate raw `sqlite3.connect()` to read DB before `init_db()` re-seeds it |
| **Tests** | All 163 tests run on a temp-file SQLite DB via `conftest.py` patching `m.DB_PATH` |

---

## Architecture: SQLAlchemy Core (not ORM)

- Existing code is already parameterized SQL — we just swap the execution layer from `sqlite3.Connection` to `sqlalchemy.engine.Connection`
- All raw SQL strings go through `sqlalchemy.text()` which normalizes parameter types per-dialect
- No entity/model mapping, no Alembic needed (schema fits in one `init_db()`)

---

## Phase 1: `db.py` — Connection + Engine Layer (NEW module)

Drop a new file `db.py` that owns all DB lifecycle. Current `app.py` functions call into it. Key design:

```python
# db.py — Minimal abstraction over SQLAlchemy engine

import os
from pathlib import Path
from sqlalchemy import create_engine, text, event
from sqlalchemy.pool import StaticPool

BASE_DIR = Path(__file__).resolve().parent

def _get_db_url() -> str:
    """Postgres URL from env var; falls back to SQLite file for dev/test."""
    url = os.environ.get("DB_URL")
    if url:
        return url
    # Same path as current DB_PATH in app.py
    db_path = BASE_DIR / "instance" / "status.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{db_path}"

_ENGINE_URL = _get_db_url()

def make_engine(url: str | None = None):
    """Create (or re-create) the SQLAlchemy engine. Used at startup and in tests."""
    target = url or _ENGINE_URL
    engine_kwargs = {"pool_pre_ping": True}
    # Single-connection pool for SQLite (thread-local safety)
    if "sqlite" in target:
        engine_kwargs["poolclass"] = StaticPool
    engine = create_engine(target, **engine_kwargs)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _):
        if "sqlite" in str(_ENGINE_URL):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

    return engine

engine = make_engine()


def get_session():
    """Yield an engine connection bound to Flask g (per-request)."""
    from flask import g
    if "db" not in g:
        g.db = engine.connect()
    return g.db


def close_session(exc):
    """Close the session at end of request."""
    from flask import g
    db = g.pop("db", None)
    if db is not None:
        db.close()


def is_postgres(db_conn) -> bool:
    return db_conn.engine.dialect.name == "postgresql"
```

**Changes in `app.py`:**
- Remove `import sqlite3` and `DB_PATH` (now owned by `db._get_db_url()`)
- Replace `get_db()` → `from db import get_session; db = get_session()`
- Replace `@app.teardown_appcontext close_db` → `from db import close_session as _close_session; @app.teardown_appcontext def close_db(exc): _close_session(exc)`
- Every `db.execute("SQL", params)` becomes `db.execute(text("SQL"), params)`
- `row["column_name"]` uses result-row dict access via `.mappings()` on the result:
  ```python
  # Before (sqlite3.Row):
  row = db.execute("SELECT id, name FROM ...").fetchone()
  name = row["name"]

  # After (SQLAlchemy Core with .mappings()):
  row = db.execute(text("SELECT id, name FROM ...")).mappings().fetchone()
  name = row["name"]
  ```

---

## Phase 2: Handle SQLite-Only Patterns

Several current patterns are SQLite-specific and need per-dialect handling:

### 2.1 `lastrowid` (L768 in `api_add`)

**Current:** `cursor.lastrowid` returns the auto-increment ID after INSERT.

**Replacement:**
```python
from sqlalchemy.dialects import sqlite as sqla_sqlite, postgresql as sqla_pg

# For SQLite:
result = db.execute(sqla_sqlite.insert(status_items).values(...))
new_id = result.inserted_primary_key[0]

# OR simpler — use RETURNING which works on both dialects:
from sqlalchemy import text
res = db.execute(text("INSERT INTO status_items (name, status, position) VALUES (:name, 'green', :pos) RETURNING id"), {"name": name, "pos": max_pos + 1})
new_id = res.scalar()
```

**Recommendation:** Use `RETURNING id` — supported by both SQLite ≥3.35 and PostgreSQL. The app already uses Python 3.14 so minimal SQLite version concern. Add a fallback for older SQLite: `SELECT last_insert_rowid()` immediately after INSERT.

### 2.2 `_archive_db_snapshot()` (L220–270)

Opens its own raw `sqlite3.connect()` — this is **SQLite-only** by construction.

**Replacement:** Keep it as a standalone function that works on the SQLite engine only. When running under Postgres, skip archiving (the config.yaml `_runtime` already provides crash-safe restore). Add explicit check:

```python
def _archive_db_snapshot(db):
    if os.environ.get("STATUS_NO_ARCHIVE"):
        return
    url = db.engine.url
    # Archive only makes sense for file-backed SQLite
    if "sqlite" not in str(url) or "memory" in str(url):
        return

    rows = list(db.execute(text(
        "SELECT id, name, status, notes, position FROM status_items ORDER BY position"
    )).mappings().fetchall())
    # ... rest is the same (json.dump to archives/)
```

This simplifies: no separate `sqlite3` connection needed; use the active session. But we must call it before `init_db()` does DDL. Since `init_db()` uses its own engine connection now, pass the existing request-bound session or create a dedicated one.

### 2.3 `CREATE TABLE IF NOT EXISTS ... AUTOINCREMENT` → Postgres DDL

SQLite `INTEGER PRIMARY KEY AUTOINCREMENT` maps to PostgreSQL `SERIAL PRIMARY KEY`. In raw SQL strings:

```python
def _create_status_items(sql):
    if is_postgres(db_conn):
        sql = (
            "CREATE TABLE status_items ("
            "id SERIAL PRIMARY KEY, name VARCHAR(255) NOT NULL UNIQUE, "
            "status VARCHAR(20) NOT NULL DEFAULT 'green', "
            "notes TEXT DEFAULT '', position INTEGER NOT NULL DEFAULT 0)"
        )
    db_conn.execute(text(sql))
```

**Alternatively**, since we're already using SQLAlchemy core and the schema is tiny, consider defining it with `Table` objects:

```python
from sqlalchemy import Table, Column, Integer, String, Text, MetaData

metadata = MetaData()

status_items_table = Table(
    "status_items", metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(255), nullable=False, unique=True),
    Column("status", String(20), default="green"),
    Column("notes", Text, default=""),
    Column("position", Integer, default=0),
)

# In init_db(): status_items_table.create(engine, checkfirst=True)
```

This is cleaner, handles DDL differences automatically (SQLite gets `INTEGER PRIMARY KEY`, Postgres gets `SERIAL`), and costs only ~20 lines of Python. I'd recommend this approach for `init_db()` schema creation while keeping route-level queries as raw `text()` SQL strings.

### 2.4 `ON DELETE CASCADE` — SQLite vs Postgres FK enforcement

**Current:** SQLite doesn't enforce foreign keys by default (you need `PRAGMA foreign_keys=ON` per-connection). History rows are manually deleted in the app code before deleting items.

**Postgres:** Enforces FKs by default. We'd add `ON DELETE CASCADE` to the table definition so if the logic ever has a race, cascades happen at the DB level as a safety net.

For SQLite, keep the manual delete logic (which already works). For Postgres, `ON DELETE CASCADE` via SQLAlchemy Table makes it redundant but harmless.

---

## Phase 3: Dialect-Neutral SQL in Queries

Almost no changes needed for the query layer. Our SQL uses standard constructs that work on both dialects:

| Construct | SQLite | Postgres | Status |
|-----------|--------|----------|--------|
| Parameter substitution (`?`) | ✅ native | ✅ via `text()`: SQLAlchemy converts to `%s`/`$1` | No change in Python code — use `text()` |
| `SELECT * FROM` | ✅ | ✅ | Same |
| `ORDER BY CASE WHEN ...` | ✅ | ✅ | Same |
| `COALESCE(MAX(col), 0)` | ✅ | ✅ | Same |
| Subquery in NOT IN (SELECT ...) | ✅ | ✅ | Same |
| `executemany()` | ✅ | ✅ via `.execution_options(executemany_values=...)` or loop `.execute(text())` | Minor change for seeding — use SQLAlchemy's `insert().values()` bulk API instead of raw `db.executemany()` |

**One notable change:** `db.executemany("INSERT INTO ... VALUES (?, ?)", [...])` in `init_db()`. Replace with:

```python
from sqlalchemy import insert, text
rows = [(n, max_pos + i + 1) for i, n in enumerate(new_items)]
if is_postgres(db_conn):
    db_conn.execute(insert(status_items_table).values(rows))  # bulk insert via SQLAlchemy
else:
    db_conn.executemany(
        text("INSERT INTO status_items (name, status, position) VALUES (:n, 'green', :p)"),
        [{"n": n, "p": max_pos + i + 1} for i, n in enumerate(new_items)]
    )
```

---

## Phase 4: Migration Script — `migrate_to_postgres.py` (NEW file)

For users who want to move existing data from SQLite to a live PostgreSQL instance. One-shot, idempotent.

```python
#!/usr/bin/env python3
"""Migrate status-data from SQLite → PostgreSQL.

Usage (from project root):
    DB_URL=postgresql+psycopg2://user:pass@host/db .venv/bin/python3 migrate_to_postgres.py
    # or dry-run to preview SQL:
    DB_URL=... .venv/bin/python3 migrate_to_postgres.py --dry-run
"""
import argparse
import json
import os
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "instance" / "status.db"


def dump_sqlite() -> tuple[list[dict], list[dict]]:
    if not DB_PATH.exists():
        print("No SQLite database found — nothing to migrate."); exit(0)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    items = [dict(r) for r in conn.execute(
        "SELECT id, name, status, notes, position FROM status_items ORDER BY position"
    ).fetchall()]
    history = [dict(r) for r in conn.execute(
        "SELECT item_id, event_type, old_value, new_value, occurred FROM status_history ORDER BY id"
    ).fetchall()]
    conn.close()
    return items, history


def load_postgres(items, history, dry_run=False):
    from sqlalchemy import create_engine, text

    db_url = os.environ.get("DB_URL")
    if not db_url:
        print("DB_URL required. E.g.: DB_URL=postgresql+psycopg2://user:pass@host/db python3 migrate_to_postgres.py"); exit(1)
    engine = create_engine(db_url, echo=dry_run)

    stmts = [
        "CREATE TABLE IF NOT EXISTS status_items ("
        "id SERIAL PRIMARY KEY, name VARCHAR(255) NOT NULL UNIQUE, "
        "status VARCHAR(20) NOT NULL DEFAULT 'green', "
        "notes TEXT DEFAULT '', position INTEGER NOT NULL DEFAULT 0)",

        "CREATE TABLE IF NOT EXISTS status_history ("
        "id SERIAL PRIMARY KEY, item_id INTEGER REFERENCES status_items(id) ON DELETE CASCADE, "
        "event_type VARCHAR(20) NOT NULL DEFAULT 'status', "
        "old_value TEXT DEFAULT '', new_value TEXT DEFAULT '', occurred TEXT NOT NULL)",
    ]

    with engine.begin() as conn:
        for ddl in stmts:
            if not dry_run:
                conn.execute(text(ddl))
            print(f"  DDL: {ddl[:80]}...")

        # Upsert items (preserve IDs so FK references stay valid)
        for item in items:
            col = text("""
                INSERT INTO status_items (id, name, status, notes, position)
                VALUES (:id, :name, :status, :notes, :position)
                ON CONFLICT (name) DO UPDATE SET
                    status = EXCLUDED.status,
                    notes = EXCLUDED.notes,
                    position = EXCLUDED.position
            """)
            if not dry_run:
                conn.execute(col, item)

        # Insert history
        for h in history:
            ins = text("""
                INSERT INTO status_history (item_id, event_type, old_value, new_value, occurred)
                VALUES (:item_id, :event_type, :old_value, :new_value, :occurred)
            """)
            if not dry_run:
                conn.execute(ins, h)

        # Fix sequences after inserting with explicit IDs
        seq_stmts = [
            "SELECT setval('status_items_id_seq', COALESCE((SELECT MAX(id) FROM status_items), 1))",
            "SELECT setval('status_history_id_seq', COALESCE((SELECT MAX(id) FROM status_history), 1))",
        ]
        for sq in seq_stmts:
            if not dry_run:
                conn.execute(text(sq))

    print(f"\n{'DRY-RUN' if dry_run else 'Migration'} complete: {len(items)} items, {len(history)} history entries.")


def main():
    parser = argparse.ArgumentParser(description="Migrate SQLite → PostgreSQL")
    parser.add_argument("--dry-run", action="store_true", help="Print DDL but don't write")
    args = parser.parse_args()

    items, history = dump_sqlite()
    load_postgres(items, history, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
```

Run order: `pip install psycopg2-binary`, then `DB_URL=postgresql+psycopg2://... python3 migrate_to_postgres.py --dry-run` → review → run without flag.

---

## Phase 5: Test Compatibility

**Recommendation: tests stay on SQLite.** The entire test suite (163 tests) patches `m.DB_PATH` to a temp file — the most minimal change for test compatibility with SQLAlchemy.

Changes needed in `conftest.py`:

```python
import tempfile
from pathlib import Path
import app as m
from db import make_engine, get_session

# Patch DB_URL so engine creates from temp SQLite, not the real one:
_tdir = tempfile.mkdtemp(prefix="mc_")
_cfg_path = Path(_tdir) / "config.yaml"
_sqlite_url = f"sqlite:///{Path(_tdir) / 'instance' / 'status.db'}"
Path(f"{_tdir}/instance").mkdir(parents=True)

# Re-create engine pointing at temp SQLite
m.engine = make_engine(_sqlite_url)

# Keep patching other paths as before:
m.CONFIG_PATH = _cfg_path
m.DB_PATH = Path(_tdir) / "instance" / "status.db"  # for _archive_db_snapshot guard
# ... rest of conftest unchanged
```

**Why not test against Postgres too?** Add that later via `pytest-postgresql` or a GitHub Actions matrix job. Premature complexity with no benefit — the SQLAlchemy Core abstraction ensures correctness via code, not via dialect-specific testing. If a future query uses `RETURNING` or other dialect-only features, we'll add a regression test at that time.

---

## Phase 6: Deployment Updates

### `install.sh` additions (optional block)

```bash
# Detect if DB_URL is set → install Postgres client, skip it for SQLite mode
if [ -n "${DB_URL:-}" ] && echo "$DB_URL" | grep -q "postgresql"; then
    echo "[+] PostgreSQL mode enabled via DB_URL"

    # System deps for psycopg2 C extension (production builds)
    if command -v apt &>/dev/null; then
        apt install -y libpq-dev gcc python3-dev  # psycopg2 build deps
    elif command -v dnf &>/dev/null; then
        dnf install -y postgresql-devel gcc python3-devel
    fi

    pip install psycopg2-binary  # works for most cases; swap to psycopg2 for strict prod
else
    echo "[+] SQLite mode (no DB_URL set) — no additional deps needed"
fi
```

### Gunicorn worker count

Current: `--workers 2` with SQLite WAL.  
With Postgres + SQLAlchemy connection pooling, the same works fine. No change needed.

---

## Phase 7: Rollback Plan

Keep `instance/status.db` around after migration. App falls back to SQLite when `DB_URL` is not set — literally just unset the env var and restart.

```bash
# Quick rollback: remove DB_URL from .env or systemd unit, restart
# Data survives because config.yaml _runtime persists all state independently of the DB
```

---

## Execution Order (Checklist)

1. Create `db.py` (connection factory + engine builder + session helpers)
2. Add `sqlalchemy>=2.0,<3.0` and `psycopg2-binary>=2.9` to `requirements.txt`
3. Rewrite `app.py`:
   - Remove `import sqlite3`, remove `DB_PATH` constant
   - Replace `get_db()` with `from db import get_session; db = get_session()`
   - Wrap all raw SQL strings in `text("...")`
   - Add `.mappings()` to result rows for dict-style column access (`row["name"]`)
   - Swap `executemany()` in `init_db()` for SQLAlchemy bulk insert
   - Use `RETURNING id` (with fallback) in `api_add()` instead of `cursor.lastrowid`
   - Make `_archive_db_snapshot()` dialect-aware (skip for non-SQLite)
4. Rewrite `init_db()` schema creation using SQLAlchemy `Table`/`Column` objects (auto-dialect DDL)
5. Create `migrate_to_postgres.py` with `--dry-run` support
6. Update `conftest.py`: patch the engine, keep tests on SQLite
7. Run full test suite — target: 0 new failures
8. Test against local Postgres (Docker: `docker run --name stg-pg -e POSTGRES_PASSWORD=test -p 5432:5432 postgres:16-alpine`, then `DB_URL=postgresql+psycopg2://postgres:test@localhost/staging python3 migrate_to_postgres.py` + manual smoke test)
9. Branch → PR → merge after verification

---

## File Change Summary

| File | Action | Effort |
|------|--------|--------|
| `requirements.txt` | Add `sqlalchemy`, `psycopg2-binary` | Trivial |
| `db.py` | **NEW** — engine, session factory, pragmas | ~50 lines |
| `app.py` | Replace sqlite3 with SQLAlchemy Core throughout | ~834 lines touched, ~60 SQL calls wrapped in `text()`, ~15 result-row mappings added |
| `migrate_to_postgres.py` | **NEW** — SQLite→Postgres one-shot migration | ~90 lines |
| `tests/conftest.py` | Patch engine instead of DB_PATH | ~10 lines changed |
| `tests/test_mc_dc.py` | Fix `_query()` helper to use SQLAlchemy `.mappings()` if needed | Minor — keep sqlite3.connect() for direct access since tests run on SQLite-only |
| `install.sh` | Optional PG deps block (skipped if no DB_URL) | ~15 lines conditional |
| `config.yaml` | No change needed | 0 |

---

## Risk Assessment

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Data loss during migration | Low | `--dry-run` validates before write; JSON archives exist as backup; config.yaml `_runtime` has redundant state |
| Test breakage from SQLAlchemy row access | Medium | All 163 tests stay on SQLite; `.mappings()` preserves dict-style column access identical to `sqlite3.Row` |
| Connection pooling issues with Gunicorn | Low | `pool_pre_ping=True`, per-worker isolation via Flask's `g`, SQLite uses `StaticPool` for thread safety |
| Performance regression | Very low | ~12 rows, negligible query count; Postgres is actually faster under concurrent load |
| `_runtime` YAML persistence broken | Very low | DB-agnostic — reads/writes config.yaml independently |
| Migration script ID conflicts | Low | Uses `ON CONFLICT (name) DO UPDATE SET` + sequence fix with `setval()` |

---

## Key Decision Points

1. **ORM vs Core:** SQLAlchemy Core chosen because the app already writes raw SQL — just needs the execution layer swapped. ORM would require defining models and rewriting every query, adding complexity for zero benefit at this data scale.

2. **Test on SQLite only:** The 163 existing tests use temp-file SQLite. Adding Postgres to the test matrix is a future enhancement (`pytest-postgresql` + Docker in CI). Dialect correctness is verified by code review and a manual smoke-test run against real Postgres.

3. **Migration script vs `init_db()` as source of truth:** The migration script copies existing data from SQLite→Postgres as a one-time operation. After that, `init_db()` handles schema creation for greenfield deployments on either backend.

4. **`returning_id` fallback:** Use `RETURNING id` in inserts (standard SQL since PostgreSQL 8.2 and SQLite 3.35+ which shipped in 2021). Fallback to `SELECT last_insert_rowid()` / `currval()` for pre-3.35 SQLite but this is unlikely needed.
