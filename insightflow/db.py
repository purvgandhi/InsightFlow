"""PostgreSQL connection handling for the API.

On Vercel each function instance is reused across requests (and with Fluid
compute may serve several at once), so we keep one small, lazily-opened pool
per instance instead of opening a connection per query. Point DATABASE_URL at
the provider's *pooled* endpoint (Neon: the "-pooler" host) so many instances
share a server-side PgBouncer. prepare_threshold=None disables server-side
prepared statements, which PgBouncer's transaction mode does not support.
"""

import os
from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

STATEMENT_TIMEOUT_MS = 8000
_pool: ConnectionPool | None = None


class DatabaseNotConfigured(RuntimeError):
    pass


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise DatabaseNotConfigured("DATABASE_URL is not set")
    return url


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=database_url(),
            min_size=0,
            max_size=int(os.environ.get("DB_POOL_MAX", "3")),
            timeout=5,            # seconds to wait for a connection before failing fast
            max_idle=60,          # close idle connections so a frozen instance holds few
            open=True,
            kwargs={
                "autocommit": True,
                "row_factory": dict_row,
                "prepare_threshold": None,
                "connect_timeout": 5,
            },
            check=ConnectionPool.check_connection,  # drop connections the server closed
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def cursor():
    # SET LOCAL lasts only for this transaction, so it is safe behind PgBouncer
    # (a session-level SET could leak to another client sharing the connection).
    with get_pool().connection() as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
        yield cur


def fetch_all(sql: str, params: dict | None = None) -> list[dict]:
    with cursor() as cur:
        cur.execute(sql, params or {})
        return cur.fetchall()


def fetch_one(sql: str, params: dict | None = None) -> dict | None:
    with cursor() as cur:
        cur.execute(sql, params or {})
        return cur.fetchone()
