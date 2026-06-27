"""Tests for generate_recovery_payload — session resume / compaction payload."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from foresight_mcp.server import generate_recovery_payload


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    """Isolate DB per test (same pattern as test_server.py)."""
    db_file = tmp_path / "test_memory.db"
    monkeypatch.setenv("FORESIGHT_DB_PATH", str(db_file))

    import foresight_mcp.config as config_module
    import foresight_mcp.connection_pool as conn_pool_module
    from foresight_mcp.connection_pool import reset_pool
    from foresight_mcp.backend import SqliteBackend
    from foresight_mcp.server import init_db

    monkeypatch.setattr(config_module, "DB_PATH", str(db_file))
    monkeypatch.setattr(conn_pool_module, "DB_PATH", str(db_file))
    reset_pool()

    from foresight_mcp.tenant_context import set_current_account_id, set_current_user_id

    set_current_user_id("_recovery_test_user_")
    set_current_account_id("_recovery_test_")

    backend = SqliteBackend(db_path=str(db_file))
    backend.connect()
    try:
        init_db(backend=backend)
    finally:
        backend.close()
    yield
    reset_pool()

    from foresight_mcp.tenant_context import reset_tenant_context

    reset_tenant_context()


def _insert_memory(conn, memory_id: str, content: str, **overrides):
    """Insert a memory row with sensible defaults."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT OR IGNORE INTO memories
        (id, content, content_hash, tenant_id, user_id, scope, retention, category,
         bank_id, created_at, updated_at, tags, emotional_context, metrics,
         is_ghost, synthesized_from, version, importance, activation_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            memory_id,
            content,
            overrides.get("content_hash"),
            overrides.get("tenant_id", "_recovery_test_"),
            overrides.get("user_id", "_recovery_test_user_"),
            overrides.get("scope", "session"),
            overrides.get("retention", "short_term"),
            overrides.get("category", "fact"),
            overrides.get("bank_id", "default"),
            overrides.get("created_at", now),
            overrides.get("updated_at", now),
            overrides.get("tags", "[]"),
            overrides.get("emotional_context", "{}"),
            overrides.get("metrics", "{}"),
            overrides.get("is_ghost", 0),
            overrides.get("synthesized_from", "[]"),
            overrides.get("version", 1),
            overrides.get("importance", 0.5),
            overrides.get("activation_count", 1),
        ),
    )
    conn.commit()


# =============================================================================
# Tests
# =============================================================================


def _get_conn():
    """Get a direct connection to the test DB (for setup/assert)."""
    from foresight_mcp.config import DB_PATH

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def test_recovery_with_session_memories():
    """generate_recovery_payload returns session-scoped memories in recovery payload."""
    conn = _get_conn()
    _insert_memory(
        conn,
        "rec-sess-1",
        "User prefers concise TypeScript type definitions with explicit generics.",
        scope="session",
        importance=0.9,
    )
    _insert_memory(
        conn,
        "rec-sess-2",
        "Session discussed database schema migration for the user profiles table.",
        scope="session",
        importance=0.7,
    )
    conn.close()

    result = generate_recovery_payload(session_id="test-session-1")

    assert "Recovery Context" in result
    assert "test-session-1" in result
    assert "rec-sess-1" in result
    assert "rec-sess-2" in result
    assert "TypeScript" in result or "database" in result


def test_recovery_falls_back_to_high_confidence_memories():
    """When few session memories exist, high-confidence project memories are included."""
    conn = _get_conn()
    _insert_memory(
        conn,
        "rec-sess-1",
        "User is working on the authentication module.",
        scope="session",
        importance=0.6,
    )
    _insert_memory(
        conn,
        "rec-proj-1",
        "Project uses React 19 with server components for the frontend architecture.",
        scope="fact",
        importance=0.9,
    )
    _insert_memory(
        conn,
        "rec-proj-2",
        "API design follows OpenAPI 3.1 with strict input validation patterns.",
        scope="fact",
        importance=0.85,
    )
    conn.close()

    result = generate_recovery_payload(session_id="test-session-2")

    assert "Recovery Context" in result
    assert "rec-sess-1" in result
    assert "rec-proj-1" in result
    assert "rec-proj-2" in result


def test_recovery_empty_memory_set():
    """generate_recovery_payload degrades gracefully when no memories exist."""
    result = generate_recovery_payload(session_id="empty-session")

    assert "0 memories" in result
    assert "No session or project memories" in result


def test_recovery_excludes_specified_memory_ids():
    """Exclude memory IDs passed in exclude_memory_ids are filtered out."""
    conn = _get_conn()
    _insert_memory(
        conn,
        "rec-to-exclude",
        "This memory should be excluded from the recovery payload.",
        scope="session",
        importance=0.9,
    )
    _insert_memory(
        conn,
        "rec-to-keep",
        "This memory should appear in the recovery payload.",
        scope="session",
        importance=0.8,
    )
    conn.close()

    result = generate_recovery_payload(
        session_id="exclude-test",
        exclude_memory_ids="rec-to-exclude",
    )

    assert "rec-to-keep" in result
    assert "rec-to-exclude" not in result


def test_recovery_respects_max_chars_budget():
    """When max_chars is set, output is truncated to fit the budget."""
    conn = _get_conn()
    _insert_memory(
        conn,
        "rec-budget-1",
        "A" * 500,  # 500 chars of content
        scope="session",
        importance=0.9,
    )
    _insert_memory(
        conn,
        "rec-budget-2",
        "B" * 500,  # 500 chars of content
        scope="session",
        importance=0.8,
    )
    _insert_memory(
        conn,
        "rec-budget-3",
        "C" * 500,  # 500 chars of content
        scope="session",
        importance=0.7,
    )
    conn.close()

    result = generate_recovery_payload(session_id="budget-test", max_chars=200)

    # Total output should be <= 200 chars
    assert len(result) <= 220, f"Expected <= 220 chars, got {len(result)}"


def test_recovery_dedup_same_content():
    """Memories with duplicate content are deduplicated (first occurrence kept)."""
    conn = _get_conn()
    shared_content = "User prefers Python type hints in all function signatures."
    _insert_memory(conn, "rec-dedup-1", shared_content, scope="session", importance=0.9)
    _insert_memory(conn, "rec-dedup-2", shared_content, scope="session", importance=0.8)
    conn.close()

    result = generate_recovery_payload(session_id="dedup-test")

    # "rec-dedup-1" appears in output (higher importance, first seen)
    assert "rec-dedup-1" in result
    # "rec-dedup-2" has same content so it should be deduplicated
    assert "rec-dedup-2" not in result


def test_recovery_dedup_cross_scope():
    """Session and project memories with same content are deduplicated."""
    conn = _get_conn()
    shared = "Shared insight about database indexing strategies."
    _insert_memory(conn, "rec-cross-1", shared, scope="session", importance=0.9)
    _insert_memory(conn, "rec-cross-2", shared, scope="fact", importance=0.95)
    conn.close()

    result = generate_recovery_payload(session_id="cross-dedup")

    assert "rec-cross-1" in result
    assert "rec-cross-2" not in result


def test_recovery_prioritizes_session_over_project():
    """Session memories appear before project memories in the output."""
    conn = _get_conn()
    _insert_memory(conn, "proj-1", "Project-level memory about architecture.", scope="fact", importance=0.95)
    _insert_memory(conn, "sess-1", "Session memory about current task.", scope="session", importance=0.7)
    conn.close()

    result = generate_recovery_payload(session_id="priority-test")

    # Both should appear
    assert "sess-1" in result
    assert "proj-1" in result


def test_recovery_exclude_multiple_ids():
    """Multiple comma-separated memory IDs can be excluded."""
    conn = _get_conn()
    _insert_memory(conn, "excl-a", "Memory A to exclude.", scope="session", importance=0.9)
    _insert_memory(conn, "excl-b", "Memory B to exclude.", scope="session", importance=0.8)
    _insert_memory(conn, "keep-c", "Memory C to keep.", scope="session", importance=0.7)
    conn.close()

    result = generate_recovery_payload(
        session_id="multi-exclude",
        exclude_memory_ids="excl-a, excl-b",
    )

    assert "keep-c" in result
    assert "excl-a" not in result
    assert "excl-b" not in result
