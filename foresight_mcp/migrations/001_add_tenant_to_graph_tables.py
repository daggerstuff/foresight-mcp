"""Migration 001: Add tenant_id to graph tables.

Adds tenant_id column to memory_entities, entity_relationships,
and memory_entity_links tables for multi-tenant isolation.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..backend.base import DatabaseBackend

logger = logging.getLogger(__name__)

MIGRATION_VERSION = 1



ALLOWED_TABLES = {
    "entities", "relationships", "memories", "memory_embeddings",
    "memory_links", "clusters", "memory_decay", "memory_versions"
}

def _safe_table(name: str) -> str:
    if name not in ALLOWED_TABLES:
        raise ValueError(f"Table {name!r} not in allowed list")
    return name


def migrate(backend: DatabaseBackend) -> None:
    """Add tenant_id column to graph tables."""
    for table in ("memory_entities", "entity_relationships", "memory_entity_links"):
        if not backend.table_exists(table):
            continue
        if not backend.column_exists(table, "tenant_id"):
            backend.execute(f"ALTER TABLE {_safe_table(table)} ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'")
            logger.info(f"Added tenant_id column to {table}")

    for table in ("memory_entities", "entity_relationships", "memory_entity_links"):
        if not backend.table_exists(table):
            continue
        backend.execute(f"CREATE INDEX IF NOT EXISTS idx_{_safe_table(table)}_tenant ON {_safe_table(table)}(tenant_id)")

