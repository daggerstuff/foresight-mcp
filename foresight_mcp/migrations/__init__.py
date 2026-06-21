"""Database migrations runner.

Runs pending schema migrations in order, recording each in the
schema_migrations table so they are idempotent across restarts.
"""

from __future__ import annotations

import importlib
import logging
import sqlite3

logger = logging.getLogger(__name__)

MIGRATIONS = [
    (1, "foresight_mcp.migrations.001_add_tenant_to_graph_tables"),
    (2, "foresight_mcp.migrations.002_unified_schema"),
]


def run_migrations(db_path: str) -> None:
    """Run all pending migrations."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
        """)

        applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()}

        for version, module_path in MIGRATIONS:
            if version not in applied:
                mod = importlib.import_module(module_path)
                mod.migrate(conn)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, datetime('now'))",
                    (version,),
                )
                conn.commit()
                logger.info(f"Applied migration {version}")
    finally:
        conn.close()
