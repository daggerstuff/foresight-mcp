"""Database migrations runner.

Runs pending schema migrations in order, recording each in the
schema_migrations table so they are idempotent across restarts.
"""

from __future__ import annotations

import importlib
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..backend.base import DatabaseBackend

logger = logging.getLogger(__name__)

MIGRATIONS = [
    (1, "foresight_mcp.migrations.001_add_tenant_to_graph_tables"),
    (2, "foresight_mcp.migrations.002_unified_schema"),
]


def run_migrations(backend: DatabaseBackend) -> None:
    """Run all pending migrations against the given backend."""
    backend.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
    """)

    applied = {row["version"] for row in backend.fetch("SELECT version FROM schema_migrations")}

    for version, module_path in MIGRATIONS:
        if version not in applied:
            mod = importlib.import_module(module_path)
            mod.migrate(backend)
            backend.set_version(version, datetime.now(timezone.utc).isoformat())
            logger.info(f"Applied migration {version}")