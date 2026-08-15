# Migration Plan: SQLite → PostgreSQL for status-my-page

## Overview

Migrate `~/Developer/status-my-page` from direct `sqlite3` calls to SQLAlchemy Core, enabling PostgreSQL as the production backend while keeping SQLite available for local dev and tests.

---

## Current State

| Aspect | Detail |
|--------|--------|
| **DB driver** | `sqlite3` (stdlib), used directly throughout `app.py` |
| **Tables** | `status_items` (id, name, status, notes, position), `status_history` (id, item_id, event_type, old_value, new_value, occurred) |
| **Connection pattern** | SQLite file at `instance/status.db`, per-request via `get_db()` using Flask's `g`, closed in `teardown_appcontext` |
| **Schema migrations** | Inline `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ADD COLUMN` (try/except) in `init_db()` |
| **Special features** | WAL mode PRAGMA, `sqlite3.Row` row factory, `_archive_db_snapshot()` reads raw SQLite for JSON archives |
| **Tests** | `conftest.py` patches `m.DB_PATH`, uses `sqlite3.connect()` directly in `db_conn()` helper and fixtures like `id_a`/`id_b` |
| **Deployment** | `install.sh` runs `$VENV/bin/python3 -c "from app import init_db; init_db()"` to seed DB |

---

## Architecture Decision: SQLAlchemy Core (not ORM)

**Why Core over ORM:**
- Current code is already plain SQL with parameterized queries — minimal conceptual shift
- No entity/model mapping overhead; we keep SQL strings and just swap the execution layer
- Smaller change surface area = fewer bugs to introduce
- Performance difference between Core and raw sqlite3 is negligible for a status page with ~12 items

**Why not alembic:**
- Schema is tiny (2 tables, 0 planned schema changes that can't be handled inline)
- `init_db()` already handles migration via CREATE IF NOT EXISTS + safe ALTER TABLE — just needs Postgres translations

---

## Phase 1: Dependency & Connection Layer

### 1.1 Add dependencies to `requirements.txt`

```
flask>=3.0,<4.0
gunicorn>=21.0,<23.0
pyyaml>=6.0,<7.0
sqlalchemy>=2.0,<3.0      # Modern Core API
psycopg2-binary>=2.9       # PostgreSQL driver (dev/staging)
```

> **Production note**: `install.sh` should install `psycopg2` (not `-binary`) plus system deps `libpq-dev` for CPython extension compilation.

### 1.2 Add `DB_URL` environment variable

Postgres connection string via env var:
```bash
DB_URL=postgresql+psycopg2://statuspage:secretpass@localhost:5432/status_page_db
```

Fallback chain in code:
1. `os.environ.get("DB_URL")` if set → use it
2. Otherwise fall back to SQLite at existing path (dev/test)

### 1.3 Create `db.py` module

Extract all DB logic into a new `db.py`:

```python
# db.py — Database abstraction layer (SQLite ↔ PostgreSQL)

import os
from pathlib import Path
from sqlalchemy import create_engine, text, event
from sqlalchemy.orm import sessionmaker

BASE_DIR = Path(__file__).resolve().parent

def get_db_url() -> str:
    """Postgres URL from env; falls back to SQLite for local dev."""
    url = os.environ.get("DB_URL")
    if url:
        return url
    db_path = BASE_DIR / "instance" / "status.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{db_path}"

_db_url = get_db_url()
engine = create_engine(_db_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    """Enable WAL mode for SQLite (no-op for Postgres)."""
    if "sqlite" in str(_db_url):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


def get_session():
    """Get a DB session for the current request (Flask g-bound via app.py)."""
    if "db" not from flask import g:
        g.db = SessionLocal()
    return g.db


def close_session(exc):
    """Close session at end of request."""
    db = g.pop("db", None)
    if db is not None:
        db.close()
```

### 1.4 Rewrite `app.py` DB functions

Replace these patterns:

| Before (sqlite3) | After (SQLAlchemy Core) |
|---|---|
| `g.db.execute("SELECT ...", params)` | `get_session().execute(text("SELECT ..."), params)` |
| `db.row_factory = sqlite3.Row` then `row["name"]` | `row.mappings()` or use column index; or map results to dicts manually |
| `db.execute("PRAGMA journal_mode=WAL")` | Removed — handled by SQLAlchemy event listener in `db.py` |
| `db.commit()` | `get_session().commit()` |
| `sqlite3.connect(str(DB_PATH))` (in `init_db`) | Use `engine.connect()` from `db.py` |

**Functions to rewrite in order:**
1. `get_all_items()` — SELECT with CASE sorting
2. `toggle_item()` — UPDATE + sub-queries for name lookup
3. `update_item_name()` — simple UPDATE
4. `reorder_items()` — batch UPDATEs
5. `set_notes()` — SELECT current notes → _record_history → UPDATE
6. `_record_history()` — INSERT + DELETE (pruning)
7. `api_history` route — fetchall from status_history
8. `api_add` route — INSERT + SELECT back all names
9. `api_delete` route — cascade DELETE + re-index positions
10. `_archive_db_snapshot()` — raw query for JSON snapshot
11. `init_db()` — CREATE TABLE, ALTER TABLE, seeds

### 1.5 Schema translation: SQLite → Postgres

```sql
-- status_items
CREATE TABLE IF NOT EXISTS status_items (
    id       SERIAL PRIMARY KEY,
    name     VARCHAR(255) NOT NULL UNIQUE,
    status   VARCHAR(20) NOT NULL DEFAULT 'green',
    notes    TEXT DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0
);

-- status_history
CREATE TABLE IF NOT EXISTS status_history (
    id         SERIAL PRIMARY KEY,
    item_id    INTEGER NOT NULL REFERENCES status_items(id) ON DELETE CASCADE,
    event_type VARCHAR(20) NOT NULL DEFAULT 'status',
    old_value  TEXT DEFAULT '',
    new_value  TEXT DEFAULT '',
    occurred   TEXT NOT NULL
);
```

**Key differences handled:**
- `INTEGER PRIMARY KEY AUTOINCREMENT` → `SERIAL PRIMARY KEY` (in Postgres Core, use `Column(Integer, primary_key=True, autoincrement=True)` or raw DDL with IF/ELSE on dialect)
- `REFERENCES status_items(id)` — SQLite doesn't enforce FK by default; Postgres does. Add `ON DELETE CASCADE` since we want history deleted when items are deleted.

Since we're using raw SQL strings (not SQLAlchemy schema objects), handle dialect differences in `init_db()`:

```python
def _is_postgres(session):
    return session.bind.dialect.name == "postgresql"

# In init_db():
if _is_postgres(session):
    session.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm;"))  # optional: for future searches
    # SERIAL is implied by PostgreSQL syntax
```

### 1.6 Update `teardown_appcontext` in `app.py`

```python
from db import close_session as db_close_session

@app.teardown_appcontext
def close_db(exc):
    db_close_session(exc)
```

---

## Phase 2: Migration Script

### 2.1 Create `migrate_to_postgres.py`

A one-shot script that dumps SQLite → loads into Postgres:

```python
#!/usr/bin/env python3
"""Migrate data from SQLite to PostgreSQL.

Usage:
    DB_URL=postgresql+psycopg2://user:pass@host/db .venv/bin/python3 migrate_to_postgres.py
"""

import sqlite3
from pathlib import Path
from sqlalchemy import create_engine, text

DB_PATH = Path(__file__).parent / "instance" / "status.db"

def dump_sqlite():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    
    items = conn.execute(
        "SELECT id, name, status, notes, position FROM status_items ORDER BY position"
    ).fetchall()
    
    history = conn.execute(
        "SELECT item_id, event_type, old_value, new_value, occurred FROM status_history ORDER BY id"
    ).fetchall()
    
    conn.close()
    return items, history

def load_postgres():
    import os
    from sqlalchemy.dialects.postgresql import SERIAL
    
    engine = create_engine(os.environ["DB_URL"])
    
    with engine.connect() as conn:
        # Create tables (PostgreSQL syntax)
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS status_items (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL UNIQUE,
                status VARCHAR(20) NOT NULL DEFAULT 'green',
                notes TEXT DEFAULT '',
                position INTEGER NOT NULL DEFAULT 0
            )
        """))
        
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS status_history (
                id SERIAL PRIMARY KEY,
                item_id INTEGER NOT NULL REFERENCES status_items(id) ON DELETE CASCADE,
                event_type VARCHAR(20) NOT NULL DEFAULT 'status',
                old_value TEXT DEFAULT '',
                new_value TEXT DEFAULT '',
                occurred TEXT NOT NULL
            )
        """))
        
        # Insert items — preserve IDs so history item_id references stay valid
        for item in dump_sqlite()[0]:
            conn.execute(text("""
                INSERT INTO status_items (id, name, status, notes, position)
                VALUES (:id, :name, :status, :notes, :position)
                ON CONFLICT (name) DO UPDATE SET
                    status = EXCLUDED.status,
                    notes = EXCLUDED.notes,
                    position = EXCLUDED.position
            """), dict(item))
        
        # Insert history — use RETURNING to avoid ID conflicts
        for h in dump_sqlite()[1]:
            conn.execute(text("""
                INSERT INTO status_history (item_id, event_type, old_value, new_value, occurred)
                VALUES (:item_id, :event_type, :old_value, :new_value, :occurred)
            """), dict(h))
        
        # Update Postgres sequences to surpass max ID
        conn.execute(text("""
            SELECT setval('status_items_id_seq', COALESCE((SELECT MAX(id) FROM status_items), 1))
        """))
        conn.execute(text("""
            SELECT setval('status_history_id_seq', COALESCE((SELECT MAX(id) FROM status_history), 1))
        """))
        
        conn.commit()
    
    print("Migration complete.")

load_postgres()
```

### 2.2 Dry-run mode

Add `--dry-run` flag that prints `pg_dump`-style output instead of writing:

```bash
DB_URL=postgresql+psycopg2://... .venv/bin/python3 migrate_to_postgres.py --dry-run
```

---

## Phase 3: Test Compatibility

### 3.1 Update `conftest.py`

Two options:

**Option A (recommended): Tests stay on SQLite**
- Most minimal change — patch `app.DB_URL` to point at in-memory SQLite or temp file
- Patch `db.engine` and `db.SessionLocal` after creating them
- All existing test assertions remain valid

```python
# conftest.py — changes needed:
import app as m
from db import create_engine, SessionLocal

_td = tempfile.mkdtemp(prefix="mc_")
_sqlite_url = f"sqlite:///{_td}/instance/status.db"
Path(f"{_td}/instance").mkdir(parents=True)

m.DB_URL = _sqlite_url  # overrides get_db_url() if we make it non-module-level
# Or refactor db.py so engine creation is deferrable per-test
```

**Option B: Test against Postgres too** — deferred; requires CI Postgres container.

I recommend **Option A** for now and adding a `pytest-postgresql` integration later via GitHub Actions or local Docker.

### 3.2 Handle SQLite-specific test helpers

The `db_conn()` fixture helper in `conftest.py` currently does raw `sqlite3.connect()`. Replace with SQLAlchemy:

```python
def db_conn(A):
    from sqlalchemy import create_engine, text
    conn = A.engine.connect()
    yield conn
    conn.close()
```

---

## Phase 4: Deployment Updates

### 4.1 `install.sh` changes

Add PostgreSQL provisioning step before DB init:

```bash
# After package manager detection
echo "=== Setting up PostgreSQL ==="
if [ "$PKG_MGR" = "apt" ]; then
    apt install -y postgresql postgresql-client libpq-dev
elif [ "$PKG_MGR" = "dnf" ] || [ "$PKG_MGR" = "yum" ]; then
    $PKG_MGR install -y postgresql-server postgresql-devel
    postgresql-setup --initdb
fi

systemctl enable --now postgresql

# Create DB and user (run as postgres user)
su - postgres -c "psql -c \"CREATE USER statuspage WITH PASSWORD 'GENERATED_PASSWORD';\"" \
su - postgres -c "psql -c \"CREATE DATABASE status_page_db OWNER statuspage;\""
```

Replace the SQLite init line with:
```bash
export DB_URL="postgresql+psycopg2://statuspage:PASSWORD@localhost/status_page_db"
"$VENV_DIR/bin/python3" migrate_to_postgres.py    # creates tables (first run)
# Or keep init_db() which now handles both dialects
```

### 4.2 `install.sh` requirements.txt handling

Replace `psycopg2-binary` with `psycopg2 + libpq-dev` for production compilation:

```bash
"$VENV_DIR/bin/pip" install psycopg2 -r "$INSTALL_DIR/requirements.txt" --quiet
```

Remove `psycopg2-binary` from production `requirements.txt`; keep it only in a dev-specific requirements file.

### 4.3 Gunicorn worker strategy (Postgres compatibility)

Current: `--workers 2` with SQLite WAL mode. This worked because WAL allows concurrent reads while one writer holds the lock.

With Postgres + connection pooling, this is fine as-is. SQLAlchemy's engine handles pool pre-ping and stale connection recovery.

---

## Phase 5: Rollback Plan

Keep `instance/status.db` until Postgres is verified stable for 1 week. Add a rollback command:

```bash
# Rollback to SQLite: unset DB_URL, restart service
sudo systemctl stop status-page
sudo rm /etc/environment.d/db-url.conf    # or wherever DB_URL lives
sudo systemctl start status-page
```

The app falls back to SQLite automatically when `DB_URL` is not set.

---

## File Change Summary

| File | Action |
|------|--------|
| `requirements.txt` | Add `sqlalchemy>=2.0,<3.0`, `psycopg2-binary>=2.9` |
| `db.py` | **NEW** — SQLAlchemy engine, session factory, pragmas |
| `app.py` | Replace all `sqlite3` usage with `db.SessionLocal` + `text()` |
| `migrate_to_postgres.py` | **NEW** — SQLite → Postgres one-shot migration |
| `config.yaml` | No change (config persistence via YAML is DB-agnostic) |
| `tests/conftest.py` | Update to use SQLAlchemy; keep SQLite for tests |
| `tests/*.py` | Fix imports if they reference `sqlite3` directly |
| `install.sh` | Add PG provisioning, `DB_URL` env var, psycopg2 install |
| `.env` (dev) | Add optional `DB_URL=...` line |

---

## Risk Assessment

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Data loss during migration | Low | JSON archive exists; dry-run validates before write |
| `ON DELETE CASCADE` deletes history we wanted | Medium | Audit `_runtime.history` in YAML — it's the canonical backup |
| Connection pooling issues with gunicorn workers | Low | SQLAlchemy `pool_pre_ping=True`; each worker gets its own connections |
| Performance regression | Very low | ~12 rows, Postgres is faster than SQLite for small queries |
| Test breakage due to dialect differences | Medium | Keep tests on SQLite; add Postgres-specific test suite later |
| `_runtime` YAML persistence broken by migration | Low | It's DB-agnostic (reads/writes config.yaml), unaffected |

---

## Execution Order

1. ✅ Write `db.py` (new module)
2. ✅ Update `requirements.txt`, install deps in venv
3. ✅ Refactor `app.py`: replace SQLite calls with SQLAlchemy Core
4. ✅ Write `migrate_to_postgres.py`
5. ✅ Dry-run migration against local Postgres (or Docker container)
6. ✅ Run actual migration
7. ✅ Update `conftest.py` for SQLAlchemy, verify tests pass on SQLite
8. ✅ Update `install.sh` for Postgres deployment
9. ✅ Deploy to production with `DB_URL` set
10. ✅ Observe for 1 week → delete SQLite fallback or keep as backup
