"""Abstract database backend for Foresight MCP.

Defines the DatabaseBackend protocol that all storage backends must implement.
Provides a clean abstraction over connection acquisition, query execution,
and transaction lifecycle so that consumers work identically against SQLite
(local development) and PostgreSQL (production / Neon).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from typing import Any


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
