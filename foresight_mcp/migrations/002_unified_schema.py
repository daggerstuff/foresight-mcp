"""
Migration 002 — Unified Memory Schema columns.

Adds `schema_version` and `source_service` columns to the memories table.
These fields track which schema version wrote each row and which service
originally created the memory — enabling audit trails and selective sync.

Sprint 1 — ADHD-318: Design Unified Memory Schema
Epic: ADHD-3 Foresight Memory Architecture
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    """Add unified schema tracking columns to the memories table."""
    # Get existing table names
    existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    if "memories" in existing:
        _add_column_if_missing(conn, "memories", "schema_version", "TEXT DEFAULT '0.0.0'")
        _add_column_if_missing(conn, "memories", "source_service", "TEXT DEFAULT 'foresight'")
        # Index for service-specific queries (e.g. sync jobs)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_source_service ON memories(source_service, tenant_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_schema_version ON memories(schema_version)")
        # Back-fill existing rows — they were all written by foresight pre-unification
        conn.execute(
            "UPDATE memories SET schema_version = '0.0.0', source_service = 'foresight' "
            "WHERE schema_version IS NULL OR schema_version = ''"
        )

    if "memory_versions" in existing:
        # Also patch memory_versions to carry the same tracking fields
        _add_column_if_missing(conn, "memory_versions", "schema_version", "TEXT DEFAULT '0.0.0'")
        _add_column_if_missing(conn, "memory_versions", "source_service", "TEXT DEFAULT 'foresight'")
        conn.execute(
            "UPDATE memory_versions SET schema_version = '0.0.0', source_service = 'foresight' "
            "WHERE schema_version IS NULL OR schema_version = ''"
        )

    conn.commit()


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    """Add a column to a table only if it does not already exist."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
