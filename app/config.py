"""
Shared configuration and database connectivity.

All DB access for the app + NLP goes through here. Two roles are supported:
  * default (read/write) — used by the pipeline only.
  * read-only — used by the NLP executor, so a parsed query can NEVER mutate data.

Connections are served from a ThreadedConnectionPool so we don't open a fresh
Postgres socket on every request.

No secrets in code; everything via environment variables.
"""
import os
import threading
import psycopg2
import psycopg2.extras
import psycopg2.pool
from contextlib import contextmanager


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-change-me")
    PGHOST = os.environ.get("PGHOST", "/tmp/pgrun")
    PGPORT = os.environ.get("PGPORT", "5433")
    PGDATABASE = os.environ.get("PGDATABASE", "police_management")
    PGUSER = os.environ.get("PGUSER", "postgres")
    PGPASSWORD = os.environ.get("PGPASSWORD")  # may be None for local trust auth
    # Read-only role for NLP query execution (falls back to PGUSER if unset).
    PG_RO_USER = os.environ.get("PG_RO_USER", os.environ.get("PGUSER", "postgres"))
    PG_RO_PASSWORD = os.environ.get("PG_RO_PASSWORD")
    LANGUAGES = ["en", "gu"]
    BABEL_DEFAULT_LOCALE = "en"


def _conn_kwargs(read_only=False):
    kw = dict(
        host=Config.PGHOST, port=Config.PGPORT,
        dbname=Config.PGDATABASE,
        user=Config.PG_RO_USER if read_only else Config.PGUSER,
    )
    pw = Config.PG_RO_PASSWORD if read_only else Config.PGPASSWORD
    if pw:
        kw["password"] = pw
    return kw


# ---- Connection pools (lazy-init, one per role) ----------------------------
_pools = {}
_pool_lock = threading.Lock()

# Pool size: 1–10 connections. Adjust for production if needed.
_POOL_MIN = 1
_POOL_MAX = 10


def _get_pool(read_only=False):
    """Return (or create) the connection pool for the given role."""
    key = "ro" if read_only else "rw"
    if key not in _pools:
        with _pool_lock:
            if key not in _pools:  # double-checked locking
                _pools[key] = psycopg2.pool.ThreadedConnectionPool(
                    _POOL_MIN, _POOL_MAX, **_conn_kwargs(read_only)
                )
    return _pools[key]


@contextmanager
def get_conn(read_only=False):
    """Context-managed pooled connection. read_only=True opens a read-only transaction."""
    pool = _get_pool(read_only)
    conn = pool.getconn()
    try:
        if read_only:
            conn.set_session(readonly=True, autocommit=False)
        yield conn
        if not read_only:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def query(sql, params=None, read_only=True):
    """Run a SELECT and return list[dict]. Read-only by default."""
    with get_conn(read_only=read_only) as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params or [])
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows

