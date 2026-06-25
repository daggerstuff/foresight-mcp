"""Abstract database backend for Foresight MCP.

Defines the DatabaseBackend protocol that all storage backends must implement.
Provides a clean abstraction over connection acquisition, query execution,
and transaction lifecycle so that consumers work identically against SQLite
(local development) and PostgreSQL (production / Neon).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DatabaseBackend(ABC):
    """Abstract base class for database backends.

    The core primitive is ``connection()`` — a context manager that yields
    a raw connection object.  Convenience methods (``execute``, ``fetch``,
    ``fetch_one``, ``execute_many``) are built on top and handle acquire /
    release / commit automatically for single-operation workflows.

    Multi-step transactions should use the ``connection()`` context manager
    directly::

        with backend.connection() as conn:
            conn.execute("INSERT INTO t (a) VALUES (?)", (1,))
            conn.execute("UPDATE t SET b = ? WHERE a = ?", (2, 1))
            conn.commit()
    """

    def __init__(self) -> None:
        self._backend_type: str | None = None

    @property
    def backend_type(self) -> str | None:
        return self._backend_type

    @abstractmethod
    def connect(self) -> None:
        """Initialise the backend (create pool, connect, run migrations, etc.)."""

    @abstractmethod
    def close(self) -> None:
        """Shut down all connections and release resources."""

    @abstractmethod
    def connection(self) -> AbstractContextManager[Any]:
        """Return a context manager that yields a raw connection.

        The connection is acquired from the pool on enter and released /
        returned on exit.
        """

    # ------------------------------------------------------------------
    # Convenience methods — override in subclasses for optimisation
    # ------------------------------------------------------------------

    def execute(self, sql: str, params: tuple | dict = ()) -> None:
        """Execute a write query (INSERT / UPDATE / DELETE / DDL) with auto-commit."""
        with self.connection() as conn:
            conn.execute(sql, params)
            conn.commit()

    def execute_many(self, sql: str, params_list: list[tuple]) -> None:
        """Execute a write query for multiple parameter sets with auto-commit."""
        with self.connection() as conn:
            conn.executemany(sql, params_list)
            conn.commit()

    def fetch(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        """Execute a read query and return all result rows as dicts."""
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def fetch_one(self, sql: str, params: tuple | dict = ()) -> dict | None:
        """Execute a read query and return the first row as a dict, or None."""
        with self.connection() as conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    # ------------------------------------------------------------------
    # Schema helpers — default implementations inspect information_schema
    # (PostgreSQL) and sqlite_master (SQLite). Subclasses may override for
    # faster paths. These power the backend-agnostic migration runner in
    # :mod:`backend_migrations`.
    # ------------------------------------------------------------------

    def table_exists(self, table_name: str) -> bool:
        if self._backend_type == "postgresql":
            rows = self.fetch(
                "SELECT 1 FROM information_schema.tables WHERE table_name = ? LIMIT 1",
                (table_name,),
            )
            return bool(rows)
        if self._backend_type == "sqlite":
            rows = self.fetch(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table_name,),
            )
            return bool(rows)
        # Fallback to original behavior if backend type not detected yet
        try:
            rows = self.fetch(
                "SELECT 1 FROM information_schema.tables WHERE table_name = ? LIMIT 1",
                (table_name,),
            )
            if rows:
                return True
        except Exception:
            pass
        try:
            rows = self.fetch(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table_name,),
            )
            return bool(rows)
        except Exception:
            return False

    def get_version(self, table: str = "schema_migrations") -> int:
        if not self.table_exists(table):
            return 0
        try:
            row = self.fetch_one(f"SELECT COALESCE(MAX(version), 0) AS v FROM {table}")
            return int((row or {}).get("v", 0) or 0)
        except Exception:
            return 0

    def set_version(self, version: int, table: str = "schema_migrations") -> None:
        if not self.table_exists(table):
            self.execute(f"CREATE TABLE {table} (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        self.execute(
            f"INSERT INTO {table} (version, applied_at) VALUES (?, ?)",
            (version, _now_iso()),
        )
