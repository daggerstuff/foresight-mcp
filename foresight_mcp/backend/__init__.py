"""Database backend package for Foresight MCP.

Provides the ``DatabaseBackend`` protocol and concrete implementations:

* ``SqliteBackend`` — default, wraps the existing SQLite connection pool
* ``PostgresBackend`` — PostgreSQL via psycopg v3
* ``RedisCompanion`` — optional cross-process cache with graceful degradation

Use ``create_backend()`` to instantiate the correct backend based on
the ``FORESIGHT_DB_URL`` environment variable; use
``run_migrations(backend)`` for the backend-agnostic migration runner
backed by the portable DDL phases in :mod:`schema_ddl`.
"""

from __future__ import annotations

from .backend_factory import create_backend
from .backend_migrations import run_migrations
from .base import DatabaseBackend
from .postgres_backend import PostgresBackend
from .redis_companion import RedisCompanion
from .schema_ddl import MIGRATIONS as SCHEMA_MIGRATIONS
from .sqlite_backend import SqliteBackend

__all__ = [
    "SCHEMA_MIGRATIONS",
    "DatabaseBackend",
    "PostgresBackend",
    "RedisCompanion",
    "SqliteBackend",
    "create_backend",
    "run_migrations",
]
