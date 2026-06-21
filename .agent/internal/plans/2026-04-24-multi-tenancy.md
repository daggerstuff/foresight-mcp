# Real Multi-Tenancy Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use executing-plans to implement this plan task-by-task.

**Goal:** Replace the global mutable tenant context with per-request tenant isolation, making Foresight MCP safe for concurrent multi-tenant access.

**Architecture:** Replace global `TENANT_ID` constant and `_tenant_context` singleton with `contextvars`-based request-scoped tenant resolution. Add tenant_id columns to entity/graph tables. Enforce tenant scoping in all queries. Add authentication middleware on the MCP transport layer.

**Tech Stack:** Python contextvars, FastMCP middleware, SQLite ALTER TABLE migrations

---

## Current State (Problems)

1. **Global constant**: `config.TENANT_ID` is a module-level constant from `os.environ` — shared across all requests
2. **Mutable singleton**: `_tenant_context` is a global `TenantContext | None` — `switch_tenant()` mutates it for every concurrent request
3. **Missing schema columns**: `memory_entities`, `entity_relationships`, `memory_entity_links` tables lack `tenant_id` — entities leak across tenants
4. **Missing schema columns**: `events`, `hooks`, `operations`, `projections`, `decay_config`, `schema_migrations` tables lack `tenant_id`
5. **No per-request context**: MCP middleware has `context` parameter but nothing extracts tenant from it
6. **No authentication**: Any client can call `switch_tenant` to access any tenant's data
7. **Thread safety**: Global `_tenant_context` has no lock — concurrent requests race

## Design Decisions

- **contextvars over thread-locals**: `contextvars.ContextVar` is async-safe (works with asyncio) and thread-safe. Each coroutine/task gets its own copy. This is the Python-idiomatic way to do request-scoped state.
- **Tenant resolution from MCP context headers**: The MCP protocol allows metadata/headers on requests. The `TenantMiddleware` will extract `tenant_id` from request metadata, validate it exists, and set the contextvar. No tenant_id parameter on individual tools.
- **Backward compatibility**: If no tenant header is provided, default to `DEFAULT_TENANT_ID` ("default"). This keeps single-tenant deployments working without any configuration change.
- **Schema migration via ALTER TABLE**: Add nullable `tenant_id` columns first, backfill with "default", then make NOT NULL. This avoids table rebuilds and preserves existing data.
- **Remove `switch_tenant` tool**: It's architecturally incompatible with per-request isolation. Replace with documentation that tenant is set via request headers.
- **Auth is a separate phase**: Authentication middleware (API keys, JWT, etc.) is out of scope for this plan. The tenant middleware validates that a tenant exists but does not authenticate the caller. A subsequent plan should add auth.

---

### Task 1: Create TenantContextVar module

**Files:**
- Create: `foresight_mcp/tenant_context.py`

**Step 1: Write the failing test**

```python
# tests/test_tenant_context.py
import asyncio
from foresight_mcp.tenant_context import (
    get_current_tenant_id,
    set_current_tenant_id,
    reset_tenant_context,
    DEFAULT_TENANT_ID,
)

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
        await asyncio.gather(asyncio.create_task(task_a()), asyncio.create_task(task_b()))

    asyncio.run(main())
    assert results["a"] == "tenant-a"
    assert results["b"] == "tenant-b"
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tenant_context.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'foresight_mcp.tenant_context'`

**Step 3: Write minimal implementation**

```python
# foresight_mcp/tenant_context.py
"""Request-scoped tenant context using contextvars.

Replaces the global _tenant_context singleton and TENANT_ID constant
with per-request isolation that works correctly with asyncio and threading.
"""
from __future__ import annotations

from contextvars import ContextVar
from .config import DEFAULT_TENANT_ID

_current_tenant: ContextVar[str] = ContextVar(
    "foresight_tenant_id", default=DEFAULT_TENANT_ID
)


def get_current_tenant_id() -> str:
    """Get the tenant ID for the current request context."""
    return _current_tenant.get()


def set_current_tenant_id(tenant_id: str) -> None:
    """Set the tenant ID for the current request context."""
    _current_tenant.set(tenant_id)


def reset_tenant_context() -> None:
    """Reset tenant context to default (for testing)."""
    _current_tenant.set(DEFAULT_TENANT_ID)
```

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_tenant_context.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add foresight_mcp/tenant_context.py tests/test_tenant_context.py
git commit -m "feat: add contextvars-based request-scoped tenant context"
```

---

### Task 2: Create TenantMiddleware for FastMCP

**Files:**
- Create: `foresight_mcp/tenant_middleware.py`
- Modify: `foresight_mcp/server.py` (register middleware)

**Step 1: Write the failing test**

```python
# tests/test_tenant_middleware.py
import json
from unittest.mock import MagicMock, AsyncMock
from foresight_mcp.tenant_middleware import TenantMiddleware


async def test_tenant_from_request_meta():
    mw = TenantMiddleware()
    context = MagicMock()
    context.meta = {"tenant_id": "acme-corp"}  # or whatever FastMCP provides
    call_next = AsyncMock(return_value="result")

    # Call middleware
    await mw.on_call_tool(context, call_next)

    # Verify tenant was set
    from foresight_mcp.tenant_context import get_current_tenant_id
    assert get_current_tenant_id() == "acme-corp"


async def test_default_tenant_when_no_meta():
    from foresight_mcp.tenant_context import reset_tenant_context
    reset_tenant_context()
    mw = TenantMiddleware()
    context = MagicMock()
    context.meta = {}
    call_next = AsyncMock(return_value="result")

    await mw.on_call_tool(context, call_next)

    from foresight_mcp.tenant_context import get_current_tenant_id
    assert get_current_tenant_id() == "default"
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tenant_middleware.py -v`
Expected: FAIL

**Step 3: Investigate FastMCP middleware context API**

Before implementing, check how FastMCP middleware receives request metadata. Look at `fastmcp.server.middleware` and the `context` object passed to `on_call_tool`. The implementation needs to match whatever structure FastMCP provides for accessing request headers/metadata.

If FastMCP context doesn't expose headers directly, fall back to extracting `tenant_id` from tool arguments (as an optional parameter on every tool), with a deprecation path toward proper header-based resolution.

**Step 4: Write minimal implementation**

```python
# foresight_mcp/tenant_middleware.py
"""FastMCP middleware that resolves tenant from request context."""
from __future__ import annotations

import logging
from .tenant_context import get_current_tenant_id, set_current_tenant_id
from .config import DEFAULT_TENANT_ID

logger = logging.getLogger(__name__)


class TenantMiddleware:
    """Resolves tenant_id from request context and sets the contextvar.

    Resolution order:
    1. Request metadata/headers (if available from MCP transport)
    2. Tool argument `tenant_id` (backward compat, deprecated)
    3. DEFAULT_TENANT_ID
    """

    async def on_call_tool(self, context, call_next):
        tenant_id = self._resolve_tenant(context)
        set_current_tenant_id(tenant_id)
        try:
            return await call_next(context)
        finally:
            # Reset to default after request completes
            set_current_tenant_id(DEFAULT_TENANT_ID)

    def _resolve_tenant(self, context) -> str:
        # Try request metadata first
        meta = getattr(context, 'meta', None) or {}
        if isinstance(meta, dict) and "tenant_id" in meta:
            return meta["tenant_id"]

        # Try arguments (backward compat)
        arguments = getattr(context, 'arguments', None) or {}
        if isinstance(arguments, dict) and "tenant_id" in arguments:
            return arguments["tenant_id"]

        return DEFAULT_TENANT_ID
```

**Step 5: Register middleware in server.py**

In `server.py`, add `TenantMiddleware` to the middleware list before `InputValidationMiddleware`:

```python
from .tenant_middleware import TenantMiddleware

mcp = FastMCP("Foresight", middleware=[
    TenantMiddleware(),
    InputValidationMiddleware(),
    RateLimitMiddleware(),
])
```

**Step 6: Run tests**

Run: `python -m pytest tests/test_tenant_middleware.py tests/test_tenant_context.py -v`
Expected: PASS

**Step 7: Commit**

```bash
git add foresight_mcp/tenant_middleware.py tests/test_tenant_middleware.py foresight_mcp/server.py
git commit -m "feat: add TenantMiddleware for per-request tenant resolution"
```

---

### Task 3: Add tenant_id to entity and graph tables

**Files:**
- Modify: `foresight_mcp/graph_store.py` (schema + queries)
- Create: `foresight_mcp/migrations/001_add_tenant_to_graph_tables.py`

**Step 1: Write the failing test**

```python
# tests/test_graph_store_tenant.py
import tempfile
import os
from foresight_mcp.graph_store import GraphStore
from foresight_mcp.entity_extractor import Entity

def test_entity_has_tenant_id():
    db_path = tempfile.mktemp(suffix=".db")
    store = GraphStore(db_path)
    entity = Entity(id="e1", name="Alice", entity_type="person")
    store.upsert_entity(entity, user_id="u1", tenant_id="acme-corp")

    # Verify tenant_id column exists and is set
    import sqlite3
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT tenant_id FROM memory_entities WHERE id = ?", ("e1",)).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "acme-corp"
    os.unlink(db_path)

def test_entity_isolated_by_tenant():
    db_path = tempfile.mktemp(suffix=".db")
    store = GraphStore(db_path)
    entity = Entity(id="e1", name="Alice", entity_type="person")
    store.upsert_entity(entity, user_id="u1", tenant_id="acme-corp")

    # Different tenant should not see this entity
    result = store.get_entity("e1", user_id="u1", tenant_id="other-corp")
    assert result is None
    os.unlink(db_path)
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_graph_store_tenant.py -v`
Expected: FAIL — `upsert_entity() got an unexpected keyword argument 'tenant_id'`

**Step 3: Modify graph_store schema and queries**

Changes to `foresight_mcp/graph_store.py`:

1. **Schema**: Add `tenant_id TEXT NOT NULL DEFAULT 'default'` to all three tables:
   - `memory_entities`: add column, add to UNIQUE constraint `(tenant_id, user_id, name, entity_type)`
   - `entity_relationships`: add column, add to UNIQUE constraint
   - `memory_entity_links`: add column

2. **Indexes**: Add `CREATE INDEX IF NOT EXISTS idx_entities_tenant ON memory_entities(tenant_id)` and same for relationships and links.

3. **All query methods**: Add `tenant_id` parameter, add `AND tenant_id = ?` to every WHERE clause.

4. **upsert_entity**: Change signature to `upsert_entity(self, entity, user_id, tenant_id=None)`. If tenant_id is None, use `get_current_tenant_id()`.

5. **All other methods** (get_entity, get_entities_by_type, find_entities_by_name, add_relationship, get_relationships, link_memory_to_entity, get_entities_for_memory, traverse_graph): Same pattern — add `tenant_id` param defaulting to contextvar.

**Step 4: Create migration for existing databases**

```python
# foresight_mcp/migrations/001_add_tenant_to_graph_tables.py
"""Migration: Add tenant_id to graph tables."""
import sqlite3
import logging

logger = logging.getLogger(__name__)

MIGRATION_VERSION = 1

def migrate(conn: sqlite3.Connection) -> None:
    """Add tenant_id column to memory_entities, entity_relationships, memory_entity_links."""
    for table in ("memory_entities", "entity_relationships", "memory_entity_links"):
        # Check if column already exists
        cursor = conn.execute(f"PRAGMA table_info({table})")
        columns = [row[1] for row in cursor.fetchall()]
        if "tenant_id" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'")
            logger.info(f"Added tenant_id column to {table}")

    # Add indexes
    for table in ("memory_entities", "entity_relationships", "memory_entity_links"):
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_tenant ON {table}(tenant_id)")
```

**Step 5: Run tests**

Run: `python -m pytest tests/test_graph_store_tenant.py -v`
Expected: PASS

**Step 6: Commit**

```bash
git add foresight_mcp/graph_store.py foresight_mcp/migrations/ tests/test_graph_store_tenant.py
git commit -m "feat: add tenant_id to entity and graph tables with isolation"
```

---

### Task 4: Add tenant_id to remaining tables

**Files:**
- Modify: `foresight_mcp/event_bus.py` (events table)
- Modify: `foresight_mcp/hooks.py` (hooks table)
- Modify: `foresight_mcp/sync.py` (operations table)
- Modify: `foresight_mcp/projections/builder.py` (projections table)
- Modify: `foresight_mcp/server.py` (decay_config table)
- Create: `foresight_mcp/migrations/002_add_tenant_to_remaining_tables.py`

**Step 1: Write failing tests**

For each table, write a test that verifies:
1. The `tenant_id` column exists
2. Queries are scoped by tenant

```python
# tests/test_tenant_remaining_tables.py
import tempfile
import sqlite3
import os

def test_events_table_has_tenant_id():
    """Events table should have tenant_id column."""
    from foresight_mcp.event_bus import EventStore
    store = EventStore(db_path=tempfile.mktemp(suffix=".db"))
    # ... create event with tenant_id, verify it's stored
```

**Step 2: Run tests to verify they fail**

**Step 3: Add tenant_id columns and update queries**

For each table:
1. Add `tenant_id TEXT NOT NULL DEFAULT 'default'` column
2. Add tenant_id index
3. Add `tenant_id` parameter to all public methods
4. Default tenant_id to `get_current_tenant_id()` when not provided
5. Add `AND tenant_id = ?` to all WHERE clauses

**Tables to migrate:**

| Table | File | Notes |
|-------|------|-------|
| `events` | `event_bus.py` | Add to Event dataclass, append(), all query methods |
| `hooks` | `hooks.py` | Add to schema, all CRUD methods |
| `operations` | `sync.py` | Add to schema, query methods |
| `projections` | `projections/builder.py` | Add to schema, query methods |
| `decay_config` | `server.py` | Add to schema, query methods |

Note: `schema_migrations` does NOT need tenant_id — it's a system-level table.

**Step 4: Run tests**

Run: `python -m pytest tests/ -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add foresight_mcp/event_bus.py foresight_mcp/hooks.py foresight_mcp/sync.py \
  foresight_mcp/projections/builder.py foresight_mcp/server.py \
  foresight_mcp/migrations/ tests/test_tenant_remaining_tables.py
git commit -m "feat: add tenant_id to events, hooks, operations, projections, decay_config tables"
```

---

### Task 5: Replace global TENANT_ID with contextvar in server.py

**Files:**
- Modify: `foresight_mcp/server.py`

**Step 1: Write the failing test**

```python
# tests/test_server_tenant_isolation.py
def test_store_memory_uses_context_tenant():
    """store_memory should use the request-scoped tenant, not the global constant."""
    from foresight_mcp.tenant_context import set_current_tenant_id, reset_tenant_context
    reset_tenant_context()
    set_current_tenant_id("acme-corp")
    # Call store_memory and verify it stores with tenant_id="acme-corp"
    # not with the global TENANT_ID
```

**Step 2: Run test to verify it fails**

**Step 3: Replace all TENANT_ID references in server.py**

This is the largest change. Replace every `TENANT_ID` reference in server.py with `get_current_tenant_id()`.

Key changes:
- Import `get_current_tenant_id` from `.tenant_context`
- Replace all `TENANT_ID` in SQL query parameters with `get_current_tenant_id()`
- In `_check_rate_limit`: replace `tid = tenant_id or TENANT_ID` with `tid = tenant_id or get_current_tenant_id()`
- In stream producer init: `environment=get_current_tenant_id() or "dev"`

**Step 4: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add foresight_mcp/server.py tests/test_server_tenant_isolation.py
git commit -m "feat: replace global TENANT_ID with request-scoped contextvar"
```

---

### Task 6: Deprecate switch_tenant and remove global _tenant_context

**Files:**
- Modify: `foresight_mcp/server.py`

**Step 1: Write the failing test**

```python
# tests/test_switch_tenant_deprecated.py
def test_switch_tenant_returns_deprecation_warning():
    """switch_tenant should warn that it's deprecated."""
    result = switch_tenant("some-tenant")
    assert "deprecated" in result.lower() or "no longer supported" in result.lower()
```

**Step 2: Run test to verify it fails**

**Step 3: Deprecate switch_tenant**

Change `switch_tenant` to return a deprecation message instead of mutating global state:

```python
@mcp.tool()
def switch_tenant(tenant_id: str) -> str:
    """
    DEPRECATED: Switch current tenant context.

    Tenant is now resolved per-request via request headers/metadata.
    This tool is kept for backward compatibility but no longer switches
    the global tenant context.

    Args:
        tenant_id: Tenant to switch to (ignored)

    Returns:
        Deprecation notice
    """
    return (
        f"switch_tenant is deprecated. Tenant is now resolved per-request "
        f"via request headers/metadata. Current tenant: {get_current_tenant_id()}. "
        f"See documentation for configuring tenant headers."
    )
```

**Step 4: Remove global _tenant_context and related functions**

Remove:
- `_tenant_context` global variable
- `TenantContext` dataclass
- `get_tenant_context()` function
- `set_tenant_context()` function

Update `get_tenant_isolation_status()` to use `get_current_tenant_id()` instead of `TENANT_ID`.

**Step 5: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: ALL PASS (some tests may need updating if they used switch_tenant)

**Step 6: Commit**

```bash
git add foresight_mcp/server.py tests/test_switch_tenant_deprecated.py
git commit -m "feat: deprecate switch_tenant, remove global _tenant_context singleton"
```

---

### Task 7: Run migration on startup

**Files:**
- Modify: `foresight_mcp/server.py` (call migrations in `_init_db`)

**Step 1: Write the failing test**

```python
# tests/test_migrations.py
def test_migrations_run_on_existing_db():
    """Starting the server on an existing DB without tenant_id columns should add them."""
    import tempfile
    db_path = tempfile.mktemp(suffix=".db")
    # Create tables without tenant_id (old schema)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE memory_entities (id TEXT PRIMARY KEY, user_id TEXT, name TEXT)")
    conn.commit()
    conn.close()

    # Run migrations
    from foresight_mcp.migrations import run_migrations
    run_migrations(db_path)

    # Verify columns exist
    conn = sqlite3.connect(db_path)
    cursor = conn.execute("PRAGMA table_info(memory_entities)")
    columns = [row[1] for row in cursor.fetchall()]
    conn.close()
    assert "tenant_id" in columns
```

**Step 2: Run test to verify it fails**

**Step 3: Implement migration runner**

Create a migration runner that:
1. Checks `schema_migrations` table for applied versions
2. Runs pending migrations in order
3. Records each migration as applied

```python
# foresight_mcp/migrations/__init__.py
"""Database migrations runner."""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

# Ordered list of migrations
MIGRATIONS = [
    (1, "foresight_mcp.migrations.001_add_tenant_to_graph_tables"),
    (2, "foresight_mcp.migrations.002_add_tenant_to_remaining_tables"),
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
                import importlib
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
```

**Step 4: Call migrations from server._init_db()**

Add at the end of `_init_db()`:

```python
from .migrations import run_migrations
run_migrations(DB_PATH)
```

**Step 5: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: ALL PASS

**Step 6: Commit**

```bash
git add foresight_mcp/migrations/ foresight_mcp/server.py tests/test_migrations.py
git commit -m "feat: add migration runner, run tenant migrations on startup"
```

---

### Task 8: Update existing tests for multi-tenancy

**Files:**
- Modify: `tests/` (all test files that reference tenant_id)

**Step 1: Identify tests needing updates**

Search all test files for:
- Direct use of `TENANT_ID` constant (should use contextvar)
- Calls to `switch_tenant` (should use `set_current_tenant_id`)
- Database queries that don't include `tenant_id` in WHERE clauses

**Step 2: Update each test file**

For each test:
- If it sets tenant directly: use `set_current_tenant_id()` and `reset_tenant_context()` in fixtures
- If it calls `switch_tenant`: update to use contextvar or accept deprecation
- Add `reset_tenant_context()` to test fixtures/teardown to prevent cross-test contamination

**Step 3: Add multi-tenant isolation test**

```python
# tests/test_multi_tenant_isolation.py
"""End-to-end tests verifying tenant data isolation."""
import asyncio
import tempfile
from foresight_mcp.tenant_context import set_current_tenant_id, reset_tenant_context


def test_tenant_a_cannot_see_tenant_b_memories():
    """Memories stored under tenant-a should not be visible to tenant-b."""
    # Store memory as tenant-a
    set_current_tenant_id("tenant-a")
    # ... store a memory

    # Switch to tenant-b and verify it's not visible
    set_current_tenant_id("tenant-b")
    # ... query memories, assert empty

    reset_tenant_context()


def test_concurrent_tenants_dont_interfere():
    """Two concurrent requests with different tenants should be isolated."""
    # Similar to the contextvar test but with actual memory operations
    pass
```

**Step 4: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add tests/
git commit -m "test: update existing tests for multi-tenant isolation"
```

---

### Task 9: Clean up config.py and remove stale imports

**Files:**
- Modify: `foresight_mcp/config.py`
- Modify: `foresight_mcp/server.py`
- Modify: `foresight_mcp/__init__.py`

**Step 1: Write the failing test**

Verify that importing `TENANT_ID` from config still works (backward compat) but that server.py no longer uses it for query scoping.

**Step 2: Update config.py**

Keep `DEFAULT_TENANT_ID` and `TENANT_ID` in config.py for backward compatibility (other modules may import them), but add a deprecation comment:

```python
# TENANT_ID is kept for backward compatibility but should not be used for
# query scoping. Use tenant_context.get_current_tenant_id() instead.
TENANT_ID = os.environ.get("FORESIGHT_TENANT_ID", DEFAULT_TENANT_ID)
```

**Step 3: Remove stale imports from server.py**

Remove `TENANT_ID` from the re-export list in `server.py` if it's no longer used internally (keep if other modules import it from server for backward compat).

**Step 4: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add foresight_mcp/config.py foresight_mcp/server.py foresight_mcp/__init__.py
git commit -m "refactor: deprecate TENANT_ID constant, add contextvar usage docs"
```

---

## Dependency Graph

```
Task 1 (contextvar module)
  └── Task 2 (TenantMiddleware) depends on Task 1
  └── Task 3 (graph tables) depends on Task 1
  └── Task 4 (remaining tables) depends on Task 1
  └── Task 5 (server.py TENANT_ID replacement) depends on Tasks 1-4
  └── Task 6 (deprecate switch_tenant) depends on Task 5
  └── Task 7 (migration runner) depends on Tasks 3-4
  └── Task 8 (update tests) depends on Tasks 1-7
  └── Task 9 (cleanup) depends on Tasks 5-6
```

Tasks 3 and 4 can run in parallel after Task 1.
Task 5 should wait for Tasks 3 and 4 (so all tables have tenant_id before server.py is updated).
Task 7 should wait for Tasks 3 and 4 (migrations must cover all tables).

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| FastMCP context doesn't expose headers | Fall back to tool argument `tenant_id` with deprecation path |
| ALTER TABLE on large databases is slow | Add nullable, backfill in batches, then NOT NULL |
| Existing tests break from tenant scoping | Task 8 is dedicated to test updates |
| graph_store.py uses raw sqlite3.connect() | Will need to pass tenant_id as parameter (can't use contextvar in background threads without explicit copy_context) |
| Third-party code imports TENANT_ID | Keep it in config.py with deprecation note |

## Out of Scope (Future Work)

- **Authentication middleware** (API keys, JWT) — separate plan
- **Per-tenant database isolation** — current design uses shared DB with tenant_id columns
- **Tenant resource quotas** — enforce memory count limits per tenant
- **Audit logging per tenant** — enhanced compliance exports
- **Tenant provisioning API** — automated tenant onboarding
