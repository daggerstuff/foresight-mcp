"""Tests for tenant isolation in Foresight memory system using store_memory and query_memories."""

import contextlib
import os
import sqlite3
import tempfile
from unittest.mock import patch

from foresight_mcp.server import query_memories, store_memory
from foresight_mcp.tenant_context import (
    reset_tenant_context,
    set_current_tenant_id,
)


def _make_temp_db(db_path: str) -> None:
    """Create a fresh temporary database and run full migrations."""
    from foresight_mcp.backend import SqliteBackend
    from foresight_mcp.server import init_db

    backend = SqliteBackend(db_path=db_path)
    backend.connect()
    try:
        init_db(backend=backend)
    finally:
        backend.close()


def test_tenant_isolation():
    """Tests that memories are isolated by tenant ID in store_memory and query_memories."""
    # Create temporary DBs for two tenants
    tenant_db1 = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tenant_db2 = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tenant_db1.close()
    tenant_db2.close()
    _make_temp_db(tenant_db1.name)
    _make_temp_db(tenant_db2.name)

    # Simulate Tenant A
    set_current_tenant_id("tenant_a")
    with (
        patch("foresight_mcp.server.get_db_connection", lambda: sqlite3.connect(tenant_db1.name)),
        patch("foresight_mcp.server.BANK_ID", "tenant_a"),
    ):
        result = store_memory(content="Confidential data for tenant A", user_id="user_a")
        assert "Stored" in result

    # Simulate Tenant B
    set_current_tenant_id("tenant_b")
    with (
        patch("foresight_mcp.server.get_db_connection", lambda: sqlite3.connect(tenant_db2.name)),
        patch("foresight_mcp.server.BANK_ID", "tenant_b"),
    ):
        result = store_memory(content="Confidential data for tenant B", user_id="user_b")
        assert "Stored" in result

    # Verify Tenant A cannot see Tenant B memory
    set_current_tenant_id("tenant_a")
    with (
        patch("foresight_mcp.server.get_db_connection", lambda: sqlite3.connect(tenant_db1.name)),
        patch("foresight_mcp.server.BANK_ID", "tenant_a"),
    ):
        results = query_memories("tenant_b")  # Search for tenant B memory
        assert "tenant b" not in results.lower(), "Tenant A should NOT see Tenant B's memories"

    # Verify Tenant B cannot see Tenant A memory
    set_current_tenant_id("tenant_b")
    with (
        patch("foresight_mcp.server.get_db_connection", lambda: sqlite3.connect(tenant_db2.name)),
        patch("foresight_mcp.server.BANK_ID", "tenant_b"),
    ):
        results = query_memories("tenant_a")
        assert "tenant a" not in results.lower(), "Tenant B should NOT see Tenant A's memories"

    # Cleanup
    with contextlib.suppress(OSError):
        os.unlink(tenant_db1.name)
    with contextlib.suppress(OSError):
        os.unlink(tenant_db2.name)

    reset_tenant_context()
