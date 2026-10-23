"""Connection pool and migrations."""
from __future__ import annotations

import os
from pathlib import Path

from psycopg_pool import ConnectionPool

MIGRATIONS = Path(__file__).resolve().parents[1] / "db" / "migrations"
_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        url = os.environ.get("DATABASE_URL", "postgresql://lab:lab@localhost/lab")
        # min_size=0 and a short idle time let Neon free tier suspend compute when nobody is using it.
        _pool = ConnectionPool(url, min_size=0, max_size=3, max_idle=60, kwargs={"autocommit": True}, open=True)
    return _pool


def migrate():
    """Applies every migration file once, in name order."""
    with pool().connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz DEFAULT now())")
        done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
        for f in sorted(MIGRATIONS.glob("*.sql")):
            if f.name in done:
                continue
            with conn.transaction():
                conn.execute(f.read_text())
                conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (f.name,))
