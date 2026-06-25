"""Backend-agnostic migration runner for Foresight MCP.

Runs pending schema migrations against any ``DatabaseBackend`` implementation
— the SQLite default or the PostgreSQL backend — and records each applied
version in ``schema_migrations``. Equivalent semantics to the legacy
``foresight_mcp.migrations.run_migrations(db_path)`` helper, but usable
against a backend chosen at runtime via ``FORESIGHT_DB_URL``.

Usage::

    from foresight_mcp.backend import create_backend
    from foresight_mcp.backend.backend_migrations import run_migrations

    backend = create_backend()
    backend.connect()
    run_migrations(backend)
    backend.close()

The runner is idempotent: re-invoking it on an up-to-date database is a
no-op. Per-version statements are wrapped in a single transaction so a
mid-migration crash is recovered cleanly on the next call.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .base import DatabaseBackend
from .schema_ddl import MIGRATIONS

logger = logging.getLogger(__name__)


# Substrings that, when present in the database error message, indicate the
# statement is already applied (idempotent on re-run). The PostgreSQL backend
# raises distinct messages for "duplicate column" / "already exists"; SQLite
# raises "OperationalError: duplicate column name" and similar.
_IDEMPOTENT_SQLITE_HINTS: tuple[str, ...] = (
    "duplicate column",
    "already exists",
)
_IDEMPOTENT_PG_HINTS: tuple[str, ...] = (
    "already exists",
    "duplicate column",
)


def _is_idempotent_error(exc: Exception) -> bool:
    """Return True if ``exc`` indicates the statement is already applied."""
    message = str(exc).lower()
    return any(hint in message for hint in _IDEMPOTENT_SQLITE_HINTS + _IDEMPOTENT_PG_HINTS)


def _ensure_schema_migrations_table(backend: DatabaseBackend) -> None:
    """Create the ``schema_migrations`` tracker table if missing.

    DDL is portable: psycopg v3 ``PostgresBackend._translate_sql`` rewrites
    ``?`` → ``%s`` automatically so the same statement runs against both
    SQLite and PostgreSQL.
    """
    if backend.table_exists("schema_migrations"):
        return
    with backend.connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def _applied_versions(backend: DatabaseBackend) -> set[int]:
    """Return the set of migration versions already applied to the backend."""
    if not backend.table_exists("schema_migrations"):
        return set()
    rows = backend.fetch("SELECT version FROM schema_migrations")
    return {int(r["version"]) for r in rows if r.get("version") is not None}


def _applied_at_iso() -> str:
    """Return the current UTC timestamp in ISO 8601."""
    return datetime.now(timezone.utc).isoformat()


def run_migrations(backend: DatabaseBackend) -> list[int]:
    """Run all pending migrations against ``backend``.

    Returns the list of versions newly applied (in ascending order). If the
    backend is already up to date, returns an empty list.
    """
    _ensure_schema_migrations_table(backend)
    applied = _applied_versions(backend)

    newly_applied: list[int] = []
    for version in sorted(MIGRATIONS):
        if version in applied:
            continue

        statements = MIGRATIONS[version]
        try:
            with backend.connection() as conn:
                # SQLite requires BEGIN IMMEDIATE for schema DDL to prevent
                # "database is locked" errors under concurrent write access.
                if backend.backend_type == "sqlite":
                    conn.execute("BEGIN IMMEDIATE")
                for stmt in statements:
                    try:
                        conn.execute(stmt)
                    except Exception as exc:
                        if _is_idempotent_error(exc):
                            logger.debug(
                                "Migration %s: skipping already-applied statement (%s)",
                                version,
                                exc,
                            )
                            continue
                        raise
                conn.commit()
        except Exception:
            logger.exception("Migration %s failed; aborting before any version is recorded", version)
            raise

        backend.set_version(version)
        logger.info("Applied migration %s", version)
        newly_applied.append(version)

    return newly_applied


def current_version(backend: DatabaseBackend) -> int:
    """Return the highest applied schema version (0 if none)."""
    _ensure_schema_migrations_table(backend)
    return backend.get_version()


__all__ = ["current_version", "run_migrations"]
