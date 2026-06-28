"""Tests for request-scoped tenant context (contextvars)."""

import asyncio
import os
import sqlite3
import tempfile
from unittest.mock import patch

from foresight_mcp.backend import SqliteBackend
from foresight_mcp.server import init_db, switch_tenant
from foresight_mcp.tenant_context import (
    DEFAULT_TENANT_ID,
    get_current_tenant_id,
    reset_tenant_context,
    set_current_tenant_id,
)


def _ephemeral_connection(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def test_get_default_tenant():
    reset_tenant_context()
    assert get_current_tenant_id() == DEFAULT_TENANT_ID


def test_set_tenant_id():
    reset_tenant_context()
    set_current_tenant_id("acme-corp")
    assert get_current_tenant_id() == "acme-corp"


def test_reset_restores_default():
    reset_tenant_context()
    set_current_tenant_id("acme-corp")
    reset_tenant_context()
    assert get_current_tenant_id() == DEFAULT_TENANT_ID


def test_switch_tenant_default_is_stable_after_bootstrap():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        backend = SqliteBackend(db_path=tmp.name)
        with patch("foresight_mcp.server.get_db_connection", lambda: _ephemeral_connection(tmp.name)):
            init_db(backend=backend)
            result = switch_tenant(DEFAULT_TENANT_ID)
        assert result == f"Switched to tenant '{DEFAULT_TENANT_ID}'"

        conn = sqlite3.connect(tmp.name)
        try:
            row = conn.execute("SELECT name FROM tenants WHERE id = ?", (DEFAULT_TENANT_ID,)).fetchone()
            assert row is not None
        finally:
            conn.close()
    finally:
        os.unlink(tmp.name)


def test_switch_tenant_unknown_returns_not_found():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        backend = SqliteBackend(db_path=tmp.name)
        with patch("foresight_mcp.server.get_db_connection", lambda: _ephemeral_connection(tmp.name)):
            init_db(backend=backend)
            result = switch_tenant("no-such-tenant")
        assert result == "Tenant 'no-such-tenant' not found"
    finally:
        os.unlink(tmp.name)


def test_contextvar_isolation_between_tasks():
    """Each asyncio task gets its own tenant context."""
    reset_tenant_context()
    results = {}

    async def task_a():
        set_current_tenant_id("tenant-a")
        await asyncio.sleep(0.01)
        results["a"] = get_current_tenant_id()

    async def task_b():
        set_current_tenant_id("tenant-b")
        await asyncio.sleep(0.01)
        results["b"] = get_current_tenant_id()

    async def main():
        await asyncio.gather(
            asyncio.create_task(task_a()),
            asyncio.create_task(task_b()),
        )

    asyncio.run(main())
    assert results["a"] == "tenant-a"
    assert results["b"] == "tenant-b"


def test_sequential_set_overrides():
    reset_tenant_context()
    set_current_tenant_id("first")
    set_current_tenant_id("second")
    assert get_current_tenant_id() == "second"
