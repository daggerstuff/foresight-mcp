"""Migration 001: Add tenant_id to graph tables.

Adds tenant_id column to memory_entities, entity_relationships,
and memory_entity_links tables for multi-tenant isolation.
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

MIGRATION_VERSION = 1


def migrate(conn: sqlite3.Connection) -> None:
    """Add tenant_id column to graph tables."""
    # Get existing table names
    existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    for table in ("memory_entities", "entity_relationships", "memory_entity_links"):
        if table not in existing:
            continue
        cursor = conn.execute(f"PRAGMA table_info({table})")
        columns = [row[1] for row in cursor.fetchall()]
        if "tenant_id" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'")
            logger.info(f"Added tenant_id column to {table}")

    for table in ("memory_entities", "entity_relationships", "memory_entity_links"):
        if table not in existing:
            continue
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_tenant ON {table}(tenant_id)")
