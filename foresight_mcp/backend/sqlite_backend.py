"""SQLite backend — wraps the existing ConnectionPool behind DatabaseBackend.

This is the default backend used when ``FORESIGHT_DB_URL`` is not set.
It preserves full backward compatibility with the existing SQLite database
at ``FORESIGHT_DB_PATH`` (or the compiled-in default).
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Generator

from .base import DatabaseBackend
from ..connection_pool import ConnectionPool
from ..config import DB_PATH

logger = logging.getLogger("foresight_sqlite_backend")


class SqliteBackend(DatabaseBackend):
    """DatabaseBackend implementation backed by a SQLite ConnectionPool."""

    def __init__(
        self,
        db_path: str | None = None,
        max_size: int = 10,
        max_idle_seconds: int = 300,
    ) -> None:
        self._db_path = db_path
        self._max_size = max_size
        self._max_idle_seconds = max_idle_seconds
        self._pool: ConnectionPool | None = None

    # ------------------------------------------------------------------
    # DatabaseBackend lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        path = self._db_path or DB_PATH
        logger.debug("Initialising SqliteBackend with db_path=%s", path)
        self._pool = ConnectionPool(
            db_path=path,
            max_size=max_size,
            max_idle_seconds=max_idle_seconds,
        )

    def close(self) -> None:
        if self._pool is not None:
            logger.debug("Closing SqliteBackend pool")
            self._pool.close_all()
            self._pool = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    @contextmanager
    def connection(self) -> Generator[Any, None, None]:
        """Acquire a pooled SQLite connection and release it on exit."""
        if self._pool is None:
            raise RuntimeError("SqliteBackend not connected. Call connect() first.")
        conn = self._pool.acquire()
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            self._pool.release(conn)

    # ------------------------------------------------------------------
    # Convenience — minor optimisation over base-class defaults
    # ------------------------------------------------------------------

    def execute(self, sql: str, params: tuple | dict = ()) -> None:
        if self._pool is None:
            raise RuntimeError("SqliteBackend not connected. Call connect() first.")
        with self._pool.acquire() as conn:
            conn.execute(sql, params)
            conn.commit()

    def execute_many(self, sql: str, params_list: list[tuple]) -> None:
        if self._pool is None:
            raise RuntimeError("SqliteBackend not connected. Call connect() first.")
        with self._pool.acquire() as conn:
            conn.executemany(sql, params_list)
            conn.commit()

    def fetch(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        if self._pool is None:
            raise RuntimeError("SqliteBackend not connected. Call connect() first.")
        with self._pool.acquire() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def fetch_one(self, sql: str, params: tuple | dict = ()) -> dict | None:
        if self._pool is None:
            raise RuntimeError("SqliteBackend not connected. Call connect() first.")
        with self._pool.acquire() as conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    # ------------------------------------------------------------------
    # Pool introspection
    # ------------------------------------------------------------------

    @property
    def stats(self) -> dict:
        if self._pool is None:
            return {"idle": 0, "in_use": 0, "max_size": self._max_size}
        return self._pool.stats
