"""Tests for the backend-agnostic migration runner (PIX-3992).

Verifies that ``foresight_mcp.backend.backend_migrations.run_migrations``
correctly bootstraps a fresh Schema (versions 1..11) against the SQLite
backend. A clean postgreSQL happy path is exercised only when
``psycopg`` is installed and ``FORESIGHT_DB_URL_TEST`` points at a
reachable DSN; otherwise that case is skipped.
"""

from __future__ import annotations

import os
import tempfile

import pytest
from foresight_mcp.backend import SCHEMA_MIGRATIONS
from foresight_mcp.backend.backend_migrations import (
    current_version,
    run_migrations,
)
from foresight_mcp.backend.sqlite_backend import SqliteBackend

# =============================================================================
# SQLite backend — primary test surface (no external services required)
# =============================================================================


class _TestConnectionPool:
    """Minimal in-memory connection pool satisfying ``DatabaseBackend``."""

    def __init__(self):
        import sqlite3

        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row

    def acquire(self):
        return self._conn

    def release(self, _conn):
        return

    def close_all(self):
        self._conn.close()

    @property
    def stats(self):
        return {"idle": 1, "in_use": 0, "max_size": 1}


class TestSqliteMigrationRunner:
    def test_empty_db_runs_versions_one_through_max(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "foresight.sqlite")
            backend = SqliteBackend(db_path=db)
            backend.connect()
            try:
                applied = run_migrations(backend)
                max_version = max(SCHEMA_MIGRATIONS)
                assert applied == list(range(1, max_version + 1))
                assert current_version(backend) == max_version
            finally:
                backend.close()

    def test_idempotent_on_re_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "foresight.sqlite")
            backend = SqliteBackend(db_path=db)
            backend.connect()
            try:
                first = run_migrations(backend)
                second = run_migrations(backend)
                assert first, "first run should apply at least one version"
                assert second == [], "second run should be a no-op"
                assert current_version(backend) == max(SCHEMA_MIGRATIONS)
            finally:
                backend.close()

    def test_key_tables_present_after_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "foresight.sqlite")
            backend = SqliteBackend(db_path=db)
            backend.connect()
            try:
                run_migrations(backend)
                required = {
                    "tenants",
                    "memories",
                    "memory_versions",
                    "decay_config",
                    "curation_runs",
                    "context_blocks",
                    "memory_relationships",
                    "memory_embeddings",
                    "documents",
                    "document_chunks",
                    "memory_decay_events",
                    "injection_runs",
                    "schema_migrations",
                }
                missing = sorted(name for name in required if not backend.table_exists(name))
                assert missing == [], f"missing tables after migration: {missing}"
            finally:
                backend.close()

    def test_memories_round_trip_after_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "foresight.sqlite")
            backend = SqliteBackend(db_path=db)
            backend.connect()
            try:
                run_migrations(backend)
                backend.execute(
                    "INSERT INTO memories (id, content, created_at) VALUES (?, ?, ?)",
                    ("mem-1", "hello world", "2026-01-01T00:00:00+00:00"),
                )
                rows = backend.fetch("SELECT content FROM memories WHERE id = ?", ("mem-1",))
                assert len(rows) == 1
                assert rows[0]["content"] == "hello world"
            finally:
                backend.close()


# =============================================================================
# Base-class helpers — shared by both backends, exercised here
# =============================================================================


class TestBackendBaseSchemaHelpers:
    def test_table_exists_true_for_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "foresight.sqlite")
            backend = SqliteBackend(db_path=db)
            backend.connect()
            try:
                backend.execute("CREATE TABLE t_seen (id INTEGER PRIMARY KEY)")
                assert backend.table_exists("t_seen") is True
            finally:
                backend.close()

    def test_table_exists_false_for_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "foresight.sqlite")
            backend = SqliteBackend(db_path=db)
            backend.connect()
            try:
                assert backend.table_exists("definitely_missing_table") is False
            finally:
                backend.close()

    def test_get_version_zero_when_predicate_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "foresight.sqlite")
            backend = SqliteBackend(db_path=db)
            backend.connect()
            try:
                assert backend.get_version() == 0
            finally:
                backend.close()


# =============================================================================
# Psycopg v3 optional test — runs only if the extra is installed AND an env
# var points at a reachable Postgres DSN. This avoids hard-failing CI when
# runners do not have a database available.
# =============================================================================


@pytest.mark.skipif(
    os.environ.get("FORESIGHT_DB_URL_TEST") is None,
    reason="FORESIGHT_DB_URL_TEST not set; skipping Postgres integration test",
)
class TestPostgresMigrationRunner:
    def test_migrations_apply(self):
        dsn = os.environ["FORESIGHT_DB_URL_TEST"]
        from foresight_mcp.backend.postgres_backend import PostgresBackend

        backend = PostgresBackend(dsn=dsn)
        backend.connect()
        try:
            backend.execute("DROP TABLE IF EXISTS schema_migrations CASCADE")
            backend.execute("DROP TABLE IF EXISTS memories CASCADE")
            applied = run_migrations(backend)
            max_version = max(SCHEMA_MIGRATIONS)
            assert applied == list(range(1, max_version + 1))
            assert current_version(backend) == max_version
            assert backend.table_exists("memories") is True
        finally:
            backend.close()
