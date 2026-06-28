"""PostgreSQL backend — wraps psycopg v3 sync behind DatabaseBackend.

Designed for Neon (serverless Postgres) and any standard PostgreSQL cluster.
Uses ``psycopg_pool.ConnectionPool`` for thread-safe connection management and
``psycopg.rows.dict_row`` for dict-like row access that mirrors the SQLite backend.

Key dialect differences handled internally:
- Parameter placeholders: ``?`` → ``%s``
- ``INTEGER PRIMARY KEY AUTOINCREMENT`` → ``SERIAL``
- ``BLOB`` → ``BYTEA``
- Neon requires ``sslmode=require``
"""

from __future__ import annotations

import logging
import re
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from .base import DatabaseBackend

logger = logging.getLogger("foresight_postgres_backend")

# ---------------------------------------------------------------------------
# Dialect translation helpers
# ---------------------------------------------------------------------------

_PARAM_RE = re.compile(r"(?<!\%)\?(?!\%)")

_AUTOINCREMENT_RE = re.compile(
    r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT",
    re.IGNORECASE,
)

_BLOB_RE = re.compile(r"\bBLOB\b", re.IGNORECASE)


def _translate_sql(sql: str) -> str:
    """Translate SQLite-flavoured SQL to PostgreSQL dialect.

    - ``?`` positional placeholders → ``%s``
    - ``INTEGER PRIMARY KEY AUTOINCREMENT`` → ``SERIAL``
    - ``BLOB`` → ``BYTEA``
    """
    out = _AUTOINCREMENT_RE.sub("SERIAL", sql)
    out = _BLOB_RE.sub("BYTEA", out)
    out = _PARAM_RE.sub("%s", out)
    return out


def _translate_params(params: tuple | dict) -> tuple | dict:
    """psycopg v3 accepts the same tuple/dict shapes; pass through."""
    return params


def _row_to_dict(row: Any) -> dict:
    """Normalise a single row to ``dict``."""
    if isinstance(row, dict):
        return row
    return dict(row)


# ---------------------------------------------------------------------------
# PostgresBackend
# ---------------------------------------------------------------------------


class PostgresBackend(DatabaseBackend):
    """DatabaseBackend implementation backed by psycopg v3 sync + ConnectionPool.

    Parameters
    ----------
    dsn :
        PostgreSQL connection string (e.g. ``postgresql://user:pass@host/db``).
        Neon DSNs should include ``sslmode=require``; the backend appends it
        automatically when absent.
    min_pool_size :
        Minimum connections kept open in the pool.
    max_pool_size :
        Maximum concurrent connections.
    """

    def __init__(
        self,
        dsn: str,
        min_pool_size: int = 2,
        max_pool_size: int = 10,
    ) -> None:
        self._dsn = self._ensure_sslmode(dsn)
        self._min_pool_size = min_pool_size
        self._max_pool_size = max_pool_size
        self._pool: Any = None  # psycopg_pool.ConnectionPool | None

    # ------------------------------------------------------------------
    # DatabaseBackend lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        logger.debug("Initialising PostgresBackend with dsn=%s", self._redact_dsn())
        self._pool = ConnectionPool(
            self._dsn,
            min_size=self._min_pool_size,
            max_size=self._max_pool_size,
            kwargs={"row_factory": dict_row},
            open=True,
        )
        self._backend_type = "postgresql"
        logger.debug("Postgres connection pool open (min=%d, max=%d)", self._min_pool_size, self._max_pool_size)

    def close(self) -> None:
        if self._pool is not None:
            logger.debug("Closing PostgresBackend pool")
            self._pool.close(timeout=10.0)
            self._pool = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    @contextmanager
    def connection(self) -> Generator[Any]:
        """Acquire a pooled psycopg connection and release it on exit.

        psycopg v3 connections from ``ConnectionPool.connection()`` already
        implement the context-manager protocol (auto-release on exit).
        """
        if self._pool is None:
            raise RuntimeError("PostgresBackend not connected. Call connect() first.")
        with self._pool.connection() as conn:
            yield conn

    # ------------------------------------------------------------------
    # Convenience overrides — translate SQLite SQL to PostgreSQL
    # ------------------------------------------------------------------

    def execute(self, sql: str, params: tuple | dict = ()) -> None:
        pg_sql = _translate_sql(sql)
        pg_params = _translate_params(params)
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(pg_sql, pg_params)
            conn.commit()

    def execute_many(self, sql: str, params_list: list[tuple]) -> None:
        pg_sql = _translate_sql(sql)
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(pg_sql, params_list)
            conn.commit()

    def fetch(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        pg_sql = _translate_sql(sql)
        pg_params = _translate_params(params)
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(pg_sql, pg_params)
                rows = cur.fetchall()
            conn.commit()
        return [_row_to_dict(row) for row in rows]

    def fetch_one(self, sql: str, params: tuple | dict = ()) -> dict | None:
        pg_sql = _translate_sql(sql)
        pg_params = _translate_params(params)
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(pg_sql, pg_params)
                row = cur.fetchone()
            conn.commit()
        return _row_to_dict(row) if row else None

    # ------------------------------------------------------------------
    # Schema helpers
    # ------------------------------------------------------------------

    def table_exists(self, table_name: str) -> bool:
        result = self.fetch_one(
            "SELECT 1 FROM information_schema.tables WHERE table_name = %s LIMIT 1",
            (table_name,),
        )
        return result is not None

    def column_exists(self, table_name: str, column_name: str) -> bool:
        result = self.fetch_one(
            "SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = %s LIMIT 1",
            (table_name, column_name),
        )
        return result is not None

    def get_version(self) -> int:
        if not self.table_exists("schema_migrations"):
            return 0
        row = self.fetch_one("SELECT MAX(version) AS version FROM schema_migrations")
        return int(row["version"]) if row and row["version"] is not None else 0

    def set_version(self, version: int, applied_at: str) -> None:
        self.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (%s, %s) ON CONFLICT (version) DO NOTHING",
            (version, applied_at),
        )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def stats(self) -> dict:
        if self._pool is None:
            return {"idle": 0, "in_use": 0, "max_size": self._max_pool_size}
        try:
            idle = self._pool._pool.free()
            in_use = self._pool._pool.size() - idle
        except Exception:
            idle, in_use = 0, 0
        return {"idle": idle, "in_use": in_use, "max_size": self._max_pool_size}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ensure_sslmode(dsn: str) -> str:
        """Append ``sslmode=require`` when not already present."""
        if "sslmode=" in dsn:
            return dsn
        separator = "&" if "?" in dsn else "?"
        return f"{dsn}{separator}sslmode=require"

    def _redact_dsn(self) -> str:
        """Return the DSN with password redacted for logging."""
        return re.sub(r"://([^:]+):([^@]+)@", r"://\1:****@", self._dsn)
