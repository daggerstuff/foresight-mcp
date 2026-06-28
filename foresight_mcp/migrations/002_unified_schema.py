"""
Migration 002 — Unified Memory Schema columns.

Adds `schema_version` and `source_service` columns to the memories table.
These fields track which schema version wrote each row and which service
originally created the memory — enabling audit trails and selective sync.

Sprint 1 — ADHD-318: Design Unified Memory Schema
Epic: ADHD-3 Foresight Memory Architecture
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..backend.base import DatabaseBackend



import re as _re

def _safe_identifier(name: str) -> str:
    """Whitelist: only allow alphanumeric + underscore identifiers."""
    if not _re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', name):
        raise ValueError(f"Unsafe identifier: {name!r}")
    return name


def migrate(backend: DatabaseBackend) -> None:
    """Add unified schema tracking columns to the memories table."""
    if backend.table_exists("memories"):
        _add_column_if_missing(backend, "memories", "schema_version", "TEXT DEFAULT '0.0.0'")
        _add_column_if_missing(backend, "memories", "source_service", "TEXT DEFAULT 'foresight'")
        backend.execute("CREATE INDEX IF NOT EXISTS idx_memories_source_service ON memories(source_service, tenant_id)")
        backend.execute("CREATE INDEX IF NOT EXISTS idx_memories_schema_version ON memories(schema_version)")
        backend.execute(
            "UPDATE memories SET schema_version = '0.0.0', source_service = 'foresight' "
            "WHERE schema_version IS NULL OR schema_version = ''"
        )

    if backend.table_exists("memory_versions"):
        _add_column_if_missing(backend, "memory_versions", "schema_version", "TEXT DEFAULT '0.0.0'")
        _add_column_if_missing(backend, "memory_versions", "source_service", "TEXT DEFAULT 'foresight'")
        backend.execute(
            "UPDATE memory_versions SET schema_version = '0.0.0', source_service = 'foresight' "
            "WHERE schema_version IS NULL OR schema_version = ''"
        )


def _add_column_if_missing(backend: DatabaseBackend, table: str, column: str, definition: str) -> None:
    """Add a column to a table only if it does not already exist."""
    if not backend.column_exists(table, column):
        backend.execute(f"ALTER TABLE {_safe_identifier(table)} ADD COLUMN {_safe_identifier(column)} {definition}")

