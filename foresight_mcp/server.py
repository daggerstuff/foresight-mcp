#!/usr/bin/env python3
"""
Foresight MCP Server - Full memory system with psychological safety features.
Restored from src/lib/ai/memory/ architecture.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import sqlite3
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, List, Any, Dict

from fastmcp import FastMCP

# Import restored memory system components
from .memory_types import (
    MemoryObject, EmotionalMetadata,
    EmpathyMetrics
)
from .memory_components import (
    MemoryCrisisTagger, SocraticGate, MemorySynthesizer, MemoryLinker
)
from .crisis_detection import get_crisis_service
from .subconscious import get_subconscious_agent, USER_PREFERENCES, PENDING_ITEMS, SESSION_PATTERNS
from .event_bus import get_event_bus, memory_stored, memory_retrieved, memory_updated, memory_deleted
from .websocket.subscriptions import SubscriptionManager
from .projections.builder import ProjectionBuilder
from .rate_limiter import RateLimitExceeded, get_rate_limiter
from .connection_pool import get_pool, PooledConnection

# Configuration - canonical source is now .config; re-exported here for
# backward compatibility so that existing `from .server import DB_PATH`
# still works during the transition.
from .config import (  # noqa: F401 - re-exports
    DB_PATH,
    USER_ID,
    BANK_ID,
    TENANT_ID,
    DEFAULT_DB_PATH,
    DEFAULT_USER_ID,
    DEFAULT_BANK_ID,
    DEFAULT_TENANT_ID,
    DEFAULT_RATE_LIMIT,
    DEFAULT_BURST_LIMIT,
)


def _run_async(coro):
    """Run an async coroutine safely, handling existing event loops.

    When an event loop is already running (e.g. inside an MCP server),
    asyncio.run() raises RuntimeError. This helper offloads the coroutine
    to a fresh loop in a background thread instead.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


def _check_rate_limit(tenant_id: str | None = None) -> None:
    """Check rate limit for tenant, raising RateLimitExceeded if exceeded."""
    tid = tenant_id or TENANT_ID
    # Look up tenant-specific limits from DB
    rate_limit = DEFAULT_RATE_LIMIT
    burst_limit = DEFAULT_BURST_LIMIT
    try:
        conn = get_db_connection()
        row = conn.execute(
            "SELECT rate_limit, burst_limit FROM tenants WHERE id = ?",
            (tid,)
        ).fetchone()
        conn.close()
        if row:
            rate_limit = row['rate_limit'] or DEFAULT_RATE_LIMIT
            burst_limit = row['burst_limit'] or DEFAULT_BURST_LIMIT
    except Exception:
        pass  # Fall back to defaults if DB unavailable

    limiter = get_rate_limiter()
    if not limiter.acquire(tid, rate_limit=rate_limit, burst_limit=burst_limit):
        remaining = limiter.get_remaining(tid)
        reset_time = time.time() + 60 / rate_limit
        raise RateLimitExceeded(remaining=remaining, reset_time=reset_time)


def get_db_connection():
    """Get a database connection from the pool.

    Returns a PooledConnection that delegates all attribute access to the
    underlying sqlite3.Connection. Calling .close() returns the connection
    to the pool instead of truly closing it.
    """
    pool = get_pool()
    conn = pool.acquire()
    return PooledConnection(conn, pool)


SCHEMA_VERSION = 2

_SCHEMA_MIGRATIONS = {
    1: [
        """CREATE TABLE IF NOT EXISTS tenants (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            rate_limit INTEGER DEFAULT 100,
            burst_limit INTEGER DEFAULT 20,
            created_at TEXT NOT NULL,
            config TEXT DEFAULT '{}'
        )""",
        """CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            scope TEXT DEFAULT 'session',
            retention TEXT DEFAULT 'short_term',
            category TEXT DEFAULT 'fact',
            user_id TEXT DEFAULT 'default',
            bank_id TEXT DEFAULT 'default',
            created_at TEXT NOT NULL,
            updated_at TEXT,
            tags TEXT DEFAULT '[]',
            emotional_context TEXT DEFAULT '{}',
            metrics TEXT DEFAULT '{}',
            vector_id TEXT,
            gist TEXT,
            is_ghost INTEGER DEFAULT 0,
            synthesized_from TEXT DEFAULT '[]',
            version INTEGER DEFAULT 1
        )""",
        """CREATE TABLE IF NOT EXISTS memory_versions (
            id TEXT PRIMARY KEY,
            memory_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            content TEXT NOT NULL,
            version INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            tags TEXT DEFAULT '[]',
            emotional_context TEXT DEFAULT '{}',
            metrics TEXT DEFAULT '{}',
            rollback_of TEXT DEFAULT NULL,
            FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
        )""",
        'CREATE INDEX IF NOT EXISTS idx_memories_tenant ON memories(tenant_id)',
        'CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id)',
        'CREATE INDEX IF NOT EXISTS idx_memories_content ON memories(content)',
        'CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope)',
        'CREATE INDEX IF NOT EXISTS idx_memories_tags ON memories(tags)',
        'CREATE INDEX IF NOT EXISTS idx_versions_memory ON memory_versions(memory_id)',
        'CREATE INDEX IF NOT EXISTS idx_versions_tenant ON memory_versions(tenant_id)',
        'CREATE INDEX IF NOT EXISTS idx_versions_created ON memory_versions(created_at)',
        'CREATE INDEX IF NOT EXISTS idx_tenants_id ON tenants(id)',
    ],
    2: [
        'ALTER TABLE memories ADD COLUMN accessed_at TEXT DEFAULT CURRENT_TIMESTAMP',
        'ALTER TABLE memories ADD COLUMN importance REAL DEFAULT 1.0',
        'ALTER TABLE memories ADD COLUMN decay_rate REAL DEFAULT 0.01',
        'ALTER TABLE memories ADD COLUMN activation_count INTEGER DEFAULT 0',
        'ALTER TABLE memories ADD COLUMN retrieval_count INTEGER DEFAULT 0',
        "ALTER TABLE memories ADD COLUMN strength_trend TEXT DEFAULT 'stable'",
        'ALTER TABLE memories ADD COLUMN last_retrieved_at TEXT',
        "ALTER TABLE memories ADD COLUMN category TEXT DEFAULT 'general'",
        'CREATE INDEX IF NOT EXISTS idx_memories_user_created ON memories(user_id, created_at DESC)',
        'CREATE INDEX IF NOT EXISTS idx_memories_user_accessed ON memories(user_id, accessed_at DESC)',
        'CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(user_id, importance DESC, created_at)',
        'CREATE INDEX IF NOT EXISTS idx_memories_strength_trend ON memories(user_id, strength_trend, created_at)',
        'CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(user_id, category, created_at DESC)',
        """CREATE TABLE IF NOT EXISTS decay_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            half_life_hours REAL DEFAULT 168.0,
            min_importance REAL DEFAULT 0.1,
            activation_boost REAL DEFAULT 1.2,
            strengthening_threshold INTEGER DEFAULT 5,
            stale_threshold REAL DEFAULT 0.2,
            UNIQUE(user_id, category)
        )""",
    ],
}


def init_db():
    """Initialize the database schema with idempotent versioned migrations."""
    db_path = Path(DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_db_connection()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
    """)

    applied = {
        row['version'] for row in
        conn.execute("SELECT version FROM schema_migrations").fetchall()
    }

    for version in sorted(_SCHEMA_MIGRATIONS):
        if version in applied:
            continue
        for stmt in _SCHEMA_MIGRATIONS[version]:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as e:
                err = str(e).lower()
                if 'duplicate column' in err or 'already exists' in err:
                    continue
                raise
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

    conn.close()

# Initialize database on module load
init_db()

# Initialize memory system components
_memory_system_initialized = False


# =============================================================================
# Version Management Functions
# =============================================================================

def get_memory_versions(memory_id: str, user_id: Optional[str] = None) -> str:
    """Get all versions of a memory."""
    uid = user_id or USER_ID
    conn = get_db_connection()

    # Verify memory exists
    row = conn.execute(
        "SELECT * FROM memories WHERE id = ? AND user_id = ? AND tenant_id = ?",
        (memory_id, uid, TENANT_ID)
    ).fetchone()

    if not row:
        conn.close()
        return f"Memory {memory_id} not found."

    # Get current version
    current_version = row['version'] if row else 1

    # Get version history
    versions = conn.execute(
        "SELECT * FROM memory_versions WHERE memory_id = ? AND tenant_id = ? ORDER BY version DESC",
        (memory_id, TENANT_ID)
    ).fetchall()
    conn.close()

    if not versions:
        return f"Memory {memory_id} (version {current_version}): No version history found."

    result = [f"Memory {memory_id} - {len(versions)} versions:", ""]
    for v in versions:
        result.append(f"  v{v['version']}: {v['content'][:50]}...")
        result.append(f"    Created: {v['created_at']}")
        if v['rollback_of']:
            result.append(f"    Rollback of: {v['rollback_of']}")

    return "\n".join(result)


def create_version_snapshot(memory_id: str, user_id: str, content: str,
                             tags: str, emotional_context: dict, metrics: dict,
                             version: int, rollback_of: Optional[str] = None) -> str:
    """Create a version snapshot before updating memory."""
    version_id = hashlib.sha256(
        f"{memory_id}{version}{datetime.now(timezone.utc).isoformat()}".encode()
    ).hexdigest()[:16]

    conn = get_db_connection()
    conn.execute("""
    INSERT INTO memory_versions (
        id, memory_id, content, version, created_at, tags, emotional_context, metrics, rollback_of
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        version_id, memory_id, content, version,
        datetime.now(timezone.utc).isoformat(), tags,
        json.dumps(emotional_context), json.dumps(metrics), rollback_of
    ))
    conn.commit()
    conn.close()
    return version_id


def rollback_to_version(memory_id: str, target_version: int, user_id: Optional[str] = None) -> str:
    """Rollback a memory to a specific version."""
    uid = user_id or USER_ID
    conn = get_db_connection()

    # Verify memory ownership first
    current = conn.execute(
        "SELECT * FROM memories WHERE id = ? AND user_id = ? AND tenant_id = ?",
        (memory_id, uid, TENANT_ID)
    ).fetchone()

    if not current:
        conn.close()
        return f"Memory {memory_id} not found"

    # Get the version content (tenant enforced via memory ownership above)
    version_row = conn.execute(
        "SELECT * FROM memory_versions WHERE memory_id = ? AND version = ? AND tenant_id = ?",
        (memory_id, target_version, TENANT_ID)
    ).fetchone()

    if not version_row:
        conn.close()
        return f"Version {target_version} not found for memory {memory_id}"

    # Snapshot current state before rollback
    create_version_snapshot(
        memory_id=memory_id,
        user_id=uid,
        content=current['content'],
        tags=current['tags'],
        emotional_context=json.loads(current['emotional_context'] or '{}'),
        metrics=json.loads(current['metrics'] or '{}'),
        version=current['version'] or 1,
        rollback_of=None
    )

    # Update to target version content
    new_version = target_version + 1
    conn.execute("""
    UPDATE memories SET
        content = ?, tags = ?, emotional_context = ?, metrics = ?,
        version = ?, updated_at = ?
    WHERE id = ? AND user_id = ?
    """, (
        version_row['content'], version_row['tags'],
        version_row['emotional_context'], version_row['metrics'],
        new_version, datetime.now(timezone.utc).isoformat(),
        memory_id, uid
    ))
    conn.commit()
    conn.close()

    # Emit rollback event
    from .event_bus import get_event_bus, memory_updated
    event_bus = get_event_bus()
    event_bus.publish(memory_updated(
        memory_id=memory_id,
        old_content=current['content'],
        new_content=version_row['content'],
        actor=uid
    ))

    return f"Rolled back memory {memory_id} to version {target_version}"


def get_memory_diff(memory_id: str, version1: int, version2: int, user_id: Optional[str] = None) -> Dict[str, Any]:
    """Get diff between two versions of a memory."""
    uid = user_id or USER_ID
    conn = get_db_connection()

    v1 = conn.execute(
        "SELECT * FROM memory_versions WHERE memory_id = ? AND version = ?",
        (memory_id, version1)
    ).fetchone()

    v2 = conn.execute(
        "SELECT * FROM memory_versions WHERE memory_id = ? AND version = ?",
        (memory_id, version2)
    ).fetchone()

    conn.close()

    if not v1 or not v2:
        return {"error": "One or both versions not found"}

    return {
        "memory_id": memory_id,
        "version1": {"version": version1, "content": v1['content']},
        "version2": {"version": version2, "content": v2['content']},
        "changed_fields": ["content"]
    }


# =============================================================================
# Memory System Components
# =============================================================================
# Memory System Components
# =============================================================================

_memory_system_initialized = False


def get_memory_system():
    """Get or initialize the memory system components."""
    global _memory_system_initialized
    if not _memory_system_initialized:
        _memory_system_initialized = True
    return {
        'tagger': MemoryCrisisTagger(get_crisis_service('high')),
        'gate': None,  # Created per-evaluate to get fresh tagger
        'synthesizer': MemorySynthesizer(),
        'linker': MemoryLinker(),
    }


from fastmcp.server.middleware import Middleware as _Middleware


class RateLimitMiddleware(_Middleware):
    """FastMCP middleware that enforces per-tenant rate limiting on tool calls."""

    async def on_call_tool(self, context, call_next):
        try:
            _check_rate_limit()
        except RateLimitExceeded as e:
            from mcp.types import CallToolResult, TextContent
            return CallToolResult(
                content=[TextContent(type="text", text=str(e))],
                isError=True,
            )
        return await call_next(context)


mcp = FastMCP("Foresight", middleware=[RateLimitMiddleware()])

logger = logging.getLogger("foresight_server")


@mcp.tool()
def store_memory(content: str, category: str = "fact",
                 scope: str = "session", retention: str = "short_term",
                 emotional_context: Optional[dict] = None,
                 metrics: Optional[dict] = None,
                 user_id: Optional[str] = None) -> str:
    """
    Store a new memory with full psychological safety features.

    Args:
        content: The memory content to store
        category: Category label (default: "fact")
        scope: Memory scope - session, arc, trait, or fact
        retention: Retention policy - ephemeral, short_term, long_term, or permanent
        emotional_context: Emotional metadata (valence, arousal, dominance, primary_emotion, intensity)
        metrics: Empathy metrics (reciprocity, validation_accuracy, resistance_level)
        user_id: Optional user ID override

    Returns:
        Confirmation with memory ID and gate decision
    """
    memory_id = hashlib.sha256(
        f"{content}{datetime.now().isoformat()}".encode()
    ).hexdigest()[:16]

    uid = user_id or USER_ID

    # Deduplication: check for exact content match within same user+tenant
    content_hash = hashlib.sha256(content.strip().lower().encode()).hexdigest()[:16]
    conn = get_db_connection()
    existing = conn.execute(
        "SELECT id, importance, activation_count FROM memories "
        "WHERE user_id = ? AND tenant_id = ? AND content = ? AND is_ghost = 0 "
        "ORDER BY created_at DESC LIMIT 1",
        (uid, TENANT_ID, content.strip())
    ).fetchone()
    if existing:
        # Bump activation count instead of creating duplicate
        conn.execute(
            "UPDATE memories SET activation_count = activation_count + 1, "
            "updated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), existing['id'])
        )
        conn.commit()
        conn.close()
        return (f"Duplicate detected — bumped activation for existing memory "
                f"{existing['id']} (activations: {existing['activation_count'] + 1})")
    conn.close()

    # Parse emotional context if provided
    emo_ctx = None
    if emotional_context:
        emo_ctx = EmotionalMetadata(**emotional_context)

    # Parse metrics if provided
    met = None
    if metrics:
        met = EmpathyMetrics(**metrics)

    # Create memory object
    memory = MemoryObject.create(
        content=content,
        scope=scope,
        retention=retention,
        emotional_context=emo_ctx,
        metrics=met
    )
    memory.id = memory_id

    # Run through Socratic Gate
    ms = get_memory_system()
    gate = SocraticGate(ms['tagger'])

    gate_result = _run_async(gate.evaluate(memory, uid))

    # Apply tags from gate
    memory.tags = gate_result.suggested_tags

    # Store in database
    conn = get_db_connection()
    conn.execute("""
        INSERT INTO memories (
            id, content, scope, retention, category, user_id, bank_id,
            created_at, tags, emotional_context, metrics, is_ghost, synthesized_from
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        memory_id, content, scope, retention, category, uid, BANK_ID,
        datetime.now(timezone.utc).isoformat(),
        json.dumps(memory.tags),
        json.dumps(emotional_context or {}),
        json.dumps(metrics or {}),
        0,
        json.dumps([])
    ))
    conn.commit()
    conn.close()

    # Emit event
    event_bus = get_event_bus()
    event_bus.publish(memory_stored(memory_id=memory_id, content=content, actor=uid))

    # Build response
    response = f"Stored memory {memory_id}: {content[:50]}..."
    response += f"\nGate Decision: {gate_result.decision}"
    response += f"\nReason: {gate_result.reason}"
    if gate_result.suggested_tags:
        response += f"\nTags: {', '.join(gate_result.suggested_tags)}"
    if gate_result.anomaly_detected:
        response += "\n⚠️  ANOMALY DETECTED - Review required"

    return response


@mcp.tool()
def query_memories(query: str, user_id: Optional[str] = None,
                   limit: int = 5, offset: int = 0) -> str:
    """Search memories by content using a query string."""
    uid = user_id or USER_ID
    escaped = query.replace('!', '!!').replace('%', '!%').replace('_', '!_')
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT * FROM memories WHERE user_id = ? AND tenant_id = ? AND content LIKE ? ESCAPE '!' LIMIT ? OFFSET ?",
        (uid, TENANT_ID, f"%{escaped}%", limit, offset)
    ).fetchall()
    conn.close()

    if not rows:
        # Fallback to hybrid retriever for semantic + graph + temporal search
        try:
            from .hybrid_retriever import get_hybrid_retriever
            retriever = get_hybrid_retriever()
            hybrid_result = retriever.search(
                query, uid, tenant_id=TENANT_ID, limit=limit
            )
            if hybrid_result.results:
                results = []
                for r in hybrid_result.results:
                    signals = ', '.join(r.source_signals) if r.source_signals else 'hybrid'
                    results.append(
                        f"- [{r.memory_id}] {r.content} "
                        f"(score={r.combined_score:.3f}, signals={signals})"
                    )
                return f"Found {len(results)} memories (hybrid search):\n" + "\n".join(results)
        except Exception:
            logger.debug("Hybrid retriever fallback failed", exc_info=True)

        return f"No memories found matching '{query}'"

    # Emit events for retrieved memories
    event_bus = get_event_bus()
    for r in rows:
        event_bus.publish(memory_retrieved(memory_id=r['id'], query_context=query, actor=uid))

    results = [f"- [{r['id']}] ({r['scope']}/{r['retention']}) {r['content']}" for r in rows]
    return f"Found {len(results)} memories:\n" + "\n".join(results)


@mcp.tool()
def list_memories(user_id: Optional[str] = None,
                  limit: int = 10, offset: int = 0) -> str:
    """List all memories for a user, ordered by creation date."""
    uid = user_id or USER_ID
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT * FROM memories WHERE user_id = ? AND tenant_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (uid, TENANT_ID, limit, offset)
    ).fetchall()
    conn.close()

    if not rows:
        return "No memories found."

    results = [f"- [{r['id']}] ({r['scope']}/{r['retention']}) {r['content'][:80]}..." for r in rows]
    return f"Memories ({len(results)} shown):\n" + "\n".join(results)


@mcp.tool()
def get_memory(memory_id: str, user_id: Optional[str] = None) -> str:
    """Retrieve a specific memory by its ID with full metadata."""
    uid = user_id or USER_ID
    conn = get_db_connection()
    row = conn.execute(
        "SELECT * FROM memories WHERE id = ? AND user_id = ? AND tenant_id = ?",
        (memory_id, uid, TENANT_ID)
    ).fetchone()
    conn.close()

    if not row:
        return f"Memory {memory_id} not found."

    # Emit event
    event_bus = get_event_bus()
    event_bus.publish(memory_retrieved(memory_id=memory_id, query_context="", actor=uid))

    # Parse JSON fields
    tags = json.loads(row['tags'])
    emotional_context = json.loads(row['emotional_context'])
    metrics = json.loads(row['metrics'])
    synthesized_from = json.loads(row['synthesized_from'])

    result = f"[{row['id']}] ({row['scope']}/{row['retention']})\n"
    result += f"Content: {row['content']}\n"
    result += f"Tags: {', '.join(tags) if tags else 'none'}\n"
    if emotional_context:
        result += f"Emotional Context: {emotional_context}\n"
    if metrics:
        result += f"Metrics: {metrics}\n"
    if row['vector_id']:
        result += f"Vector ID: {row['vector_id']}\n"
    if row['gist']:
        result += f"Gist: {row['gist']}\n"
    if row['is_ghost']:
        result += "[GHOST NODE - Content archived]"

    return result


@mcp.tool()
def update_memory(memory_id: str, content: Optional[str] = None,
                  category: Optional[str] = None,
                  scope: Optional[str] = None,
                  retention: Optional[str] = None,
                  tags: Optional[List[str]] = None,
                  user_id: Optional[str] = None) -> str:
    """Update an existing memory's content or metadata."""
    uid = user_id or USER_ID
    conn = get_db_connection()
    row = conn.execute(
        "SELECT * FROM memories WHERE id = ? AND user_id = ? AND tenant_id = ?",
        (memory_id, uid, TENANT_ID)
    ).fetchone()

    if not row:
        conn.close()
        return f"Memory {memory_id} not found."

    updates = []
    values = []

    if content:
        # Create version snapshot before updating
        current_version = row['version'] or 1
        version_id = hashlib.sha256(
            f"{memory_id}{current_version}{datetime.now(timezone.utc).isoformat()}".encode()
        ).hexdigest()[:16]
        conn.execute("""
        INSERT INTO memory_versions (
            id, memory_id, content, version, created_at, tags, emotional_context, metrics, rollback_of
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            version_id, memory_id, row['content'], current_version,
            datetime.now(timezone.utc).isoformat(),
            row['tags'], row['emotional_context'], row['metrics'], None
        ))
        updates.append("content = ?")
        values.append(content)
    if category:
        updates.append("category = ?")
        values.append(category)
    if scope:
        updates.append("scope = ?")
        values.append(scope)
    if retention:
        updates.append("retention = ?")
        values.append(retention)
    if tags:
        updates.append("tags = ?")
        values.append(json.dumps(tags))

    if updates:
        updates.append("updated_at = ?")
        values.append(datetime.now(timezone.utc).isoformat())
        if content:
            current_version = row['version'] or 1
            updates.append("version = ?")
            values.append(current_version + 1)
        values.extend([memory_id, uid])
        conn.execute(
            f"UPDATE memories SET {', '.join(updates)} WHERE id = ? AND user_id = ?",
            values
        )
        conn.commit()

    conn.close()

    # Emit event
    event_bus = get_event_bus()
    event_bus.publish(memory_updated(memory_id=memory_id, old_content=row['content'], new_content=content or row['content'], actor=uid))

    return f"Updated memory {memory_id}"


@mcp.tool()
def delete_memory(memory_id: str, user_id: Optional[str] = None) -> str:
    """Delete a memory by its ID."""
    uid = user_id or USER_ID
    conn = get_db_connection()
    row = conn.execute(
        "SELECT id FROM memories WHERE id = ? AND user_id = ? AND tenant_id = ?",
        (memory_id, uid, TENANT_ID)
    ).fetchone()

    if not row:
        conn.close()
        return f"Memory {memory_id} not found."

    # Emit event before deletion
    event_bus = get_event_bus()
    event_bus.publish(memory_deleted(memory_id=memory_id, actor=uid))

    conn.execute("DELETE FROM memories WHERE id = ? AND user_id = ? AND tenant_id = ?", (memory_id, uid, TENANT_ID))
    conn.commit()
    conn.close()
    return f"Deleted memory {memory_id}"


@mcp.tool()
def synthesize_memories(user_id: Optional[str] = None) -> str:
    """
    Run synthesis on all memories to detect stance shifts and merge candidates.

    Returns:
        Synthesis results including merged IDs and detected shifts
    """
    uid = user_id or USER_ID
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT * FROM memories WHERE user_id = ? AND tenant_id = ? ORDER BY created_at LIMIT 500",
        (uid, TENANT_ID)
    ).fetchall()
    conn.close()

    if len(rows) < 5:
        return "Not enough memories for synthesis (need 5+, have %d)" % len(rows)

    # Convert to MemoryObject list
    memories = []
    for row in rows:
        emo = json.loads(row['emotional_context']) if row['emotional_context'] else None
        met = json.loads(row['metrics']) if row['metrics'] else None
        emo_obj = EmotionalMetadata(**emo) if emo else None
        met_obj = EmpathyMetrics(**met) if met else None

        mem = MemoryObject(
            id=row['id'],
            timestamp=row['created_at'],
            scope=row['scope'],
            retention=row['retention'],
            content=row['content'],
            tags=json.loads(row['tags']) or [],
            synthesized_from=json.loads(row['synthesized_from']) or [],
            is_ghost=bool(row.get('is_ghost', 0)),
            emotional_context=emo_obj,
            metrics=met_obj,
            vector_id=row.get('vector_id'),
            gist=row.get('gist')
        )
        memories.append(mem)

    # Run synthesis
    ms = get_memory_system()
    result = _run_async(ms['synthesizer'].synthesize(memories))

    if not result:
        return "Synthesis returned no results."

    output = [
        "=== Synthesis Results ===",
        f"Merged memories: {len(result.merged_ids)}",
        f"New memory ID: {result.new_memory_id}",
        f"Compression ratio: {result.compression_ratio:.2f}",
        f"Stance shifts detected: {len(result.stance_shifts)}"
    ]

    if result.stance_shifts:
        output.append("\n--- Stance Shifts ---")
        for shift in result.stance_shifts:
            output.append(
                f"  {shift.attribute}: {shift.old_value:.2f} → {shift.new_value:.2f} "
                f"(Δ={shift.delta:+.2f}, confidence={shift.confidence:.2f})"
            )

    return "\n".join(output)


@mcp.tool()
def archive_memory(memory_id: str, user_id: Optional[str] = None) -> str:
    """
    Archive a memory to a ghost node.
    Requires the memory to have a vector_id.
    """
    uid = user_id or USER_ID
    conn = get_db_connection()
    row = conn.execute(
        "SELECT * FROM memories WHERE id = ? AND user_id = ? AND tenant_id = ?",
        (memory_id, uid, TENANT_ID)
    ).fetchone()

    if not row:
        conn.close()
        return f"Memory {memory_id} not found."

    if not row.get('vector_id'):
        conn.close()
        return f"Cannot archive memory without vector_id. Embed first."

    # Create ghost node
    ms = get_memory_system()
    ghost = ms['linker'].to_ghost(
        MemoryObject(
            id=row['id'],
            timestamp=row['created_at'],
            scope=row['scope'],
            retention=row['retention'],
            content=row['content'],
            tags=json.loads(row['tags']) or [],
            synthesized_from=json.loads(row['synthesized_from']) or [],
            is_ghost=bool(row.get('is_ghost', 0)),
            vector_id=row['vector_id'],
            gist=row.get('gist')
        )
    )

    # Update database
    conn.execute("""
        UPDATE memories SET content = ?, is_ghost = 1, gist = ?
        WHERE id = ? AND user_id = ?
    """, (ghost.content, ghost.gist, memory_id, uid))
    conn.commit()
    conn.close()

    return f"Archived memory {memory_id} to ghost node. Gist: {ghost.gist}"


# =============================================================================
# Memory Versioning Tools
# =============================================================================

@mcp.tool()
def rollback_memory(memory_id: str, to_version: int, user_id: Optional[str] = None) -> str:
    """
    Rollback a memory to a previous version.

    Args:
        memory_id: The memory ID to rollback
        to_version: Version number to rollback to
        user_id: Optional user ID override

    Returns:
        Confirmation message
    """
    uid = user_id or USER_ID
    conn = get_db_connection()

    # Verify memory exists
    row = conn.execute(
        "SELECT * FROM memories WHERE id = ? AND user_id = ? AND tenant_id = ?",
        (memory_id, uid, TENANT_ID)
    ).fetchone()

    if not row:
        conn.close()
        return f"Memory {memory_id} not found."

    # Verify version exists (tenant enforced via memory ownership above)
    version_row = conn.execute(
        "SELECT * FROM memory_versions WHERE memory_id = ? AND version = ? AND tenant_id = ?",
        (memory_id, to_version, TENANT_ID)
    ).fetchone()

    if not version_row:
        conn.close()
        return f"Version {to_version} not found for memory {memory_id}."

    # Snapshot current state
    version_id = hashlib.sha256(
        f"{memory_id}{row['version']}{datetime.now(timezone.utc).isoformat()}".encode()
    ).hexdigest()[:16]

    conn.execute("""
    INSERT INTO memory_versions (
        id, memory_id, content, version, created_at, tags, emotional_context, metrics, rollback_of
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        version_id, memory_id, row['content'], row['version'] or 1,
        datetime.now(timezone.utc).isoformat(),
        row['tags'], row['emotional_context'], row['metrics'], None
    ))

    # Update to target version content
    new_version = to_version + 1
    conn.execute("""
    UPDATE memories SET
        content = ?, tags = ?, emotional_context = ?, metrics = ?,
        version = ?, updated_at = ?
    WHERE id = ? AND user_id = ?
    """, (
        version_row['content'], version_row['tags'],
        version_row['emotional_context'], version_row['metrics'],
        new_version, datetime.now(timezone.utc).isoformat(),
        memory_id, uid
    ))
    conn.commit()
    conn.close()

    # Emit event
    from .event_bus import get_event_bus, memory_updated
    event_bus = get_event_bus()
    event_bus.publish(memory_updated(
        memory_id=memory_id,
        old_content=row['content'],
        new_content=version_row['content'],
        actor=uid
    ))

    return f"Rolled back memory {memory_id} to version {to_version} (now at version {new_version})"


@mcp.tool()
def diff_memories(memory_id: str, version1: int, version2: int, user_id: Optional[str] = None) -> str:
    """
    Compare two versions of a memory.

    Args:
        memory_id: The memory ID to compare
        version1: First version number
        version2: Second version number
        user_id: Optional user ID override

    Returns:
        Diff output showing changes between versions
    """
    uid = user_id or USER_ID
    conn = get_db_connection()

    # Verify memory ownership first
    mem = conn.execute(
        "SELECT id FROM memories WHERE id = ? AND user_id = ? AND tenant_id = ?",
        (memory_id, uid, TENANT_ID)
    ).fetchone()

    if not mem:
        conn.close()
        return f"Memory {memory_id} not found."

    v1 = conn.execute(
        "SELECT content, created_at FROM memory_versions WHERE memory_id = ? AND version = ? AND tenant_id = ?",
        (memory_id, version1, TENANT_ID)
    ).fetchone()

    v2 = conn.execute(
        "SELECT content, created_at FROM memory_versions WHERE memory_id = ? AND version = ? AND tenant_id = ?",
        (memory_id, version2, TENANT_ID)
    ).fetchone()

    conn.close()

    if not v1:
        return f"Version {version1} not found for memory {memory_id}."
    if not v2:
        return f"Version {version2} not found for memory {memory_id}."

    # Simple diff output
    result = [
        f"Comparing versions of {memory_id}:",
        "",
        f"Version {version1} ({v1['created_at']}):",
        f"  {v1['content'][:100]}...",
        "",
        f"Version {version2} ({v2['created_at']}):",
        f"  {v2['content'][:100]}...",
    ]

    if v1['content'] == v2['content']:
        result.append("")
        result.append("No changes between versions.")
    else:
        result.append("")
        result.append("Content changed.")

    return "\n".join(result)


@mcp.tool()
def memory_status() -> str:
    """Get the current status of the memory system."""
    conn = get_db_connection()
    count = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE user_id = ?",
        (USER_ID,)
    ).fetchone()[0]

    # Count by scope
    scope_counts = conn.execute(
        "SELECT scope, COUNT(*) FROM memories WHERE user_id = ? GROUP BY scope",
        (USER_ID,)
    ).fetchall()

    # Count crisis signals
    crisis_count = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE user_id = ? AND tags LIKE '%CRISIS%'",
        (USER_ID,)
    ).fetchone()[0]

    conn.close()

    status = {
        "status": "healthy",
        "database": DB_PATH,
        "bank_id": BANK_ID,
        "user_id": USER_ID,
        "memory_count": count,
        "crisis_signals": crisis_count,
        "by_scope": {r[0]: r[1] for r in scope_counts}
    }

    return json.dumps(status, indent=2)


# =============================================================================
# Subconscious Memory Block Tools
# =============================================================================

@mcp.tool()
def get_subconscious_blocks(user_id: Optional[str] = None) -> str:
    """
    Get all subconscious memory blocks.

    Returns:
        JSON list of all non-empty memory blocks
    """
    uid = user_id or USER_ID
    agent = get_subconscious_agent(uid)
    blocks = agent.get_all_blocks()
    return json.dumps(blocks, indent=2)


@mcp.tool()
def get_subconscious_block(label: str, user_id: Optional[str] = None) -> str:
    """
    Get a specific subconscious memory block.

    Args:
        label: Block label (guidance, pending_items, project_context,
               session_patterns, user_preferences, self_improvement, tool_guidelines)
        user_id: Optional user ID override

    Returns:
        Block content or not found message
    """
    uid = user_id or USER_ID
    agent = get_subconscious_agent(uid)
    content = agent.get_block(label)
    if content:
        return f"[{label}]\n{content}"
    return f"Block '{label}' not found."


@mcp.tool()
def update_subconscious_block(
    label: str,
    content: str,
    user_id: Optional[str] = None
) -> str:
    """
    Update a subconscious memory block.

    Args:
        label: Block label to update
        content: New content for the block
        user_id: Optional user ID override

    Returns:
        Confirmation message
    """
    uid = user_id or USER_ID
    agent = get_subconscious_agent(uid)
    agent.update_guidance(content) if label == "guidance" else None
    if label != "guidance":
        agent.state.update_block(label, content)
    return f"Updated block '{label}'"


@mcp.tool()
def add_subconscious_guidance(line: str, user_id: Optional[str] = None) -> str:
    """
    Add a line to the guidance block.

    Args:
        line: Line to append to guidance
        user_id: Optional user ID override

    Returns:
        Confirmation message
    """
    uid = user_id or USER_ID
    agent = get_subconscious_agent(uid)
    agent.add_guidance_line(line)
    return f"Added guidance line: {line[:50]}..."


@mcp.tool()
def get_subconscious_whisper(user_id: Optional[str] = None) -> str:
    """
    Get the current whisper injection (guidance in XML format).

    Args:
        user_id: Optional user ID override

    Returns:
        XML formatted whisper message or empty if no guidance
    """
    uid = user_id or USER_ID
    agent = get_subconscious_agent(uid)
    whisper = agent.get_whisper()
    if not whisper:
        return "(No active guidance - whisper is empty)"
    return whisper


@mcp.tool()
def get_subconscious_context(user_id: Optional[str] = None) -> str:
    """
    Get all subconscious memory blocks as XML context.

    Args:
        user_id: Optional user ID override

    Returns:
        XML formatted memory blocks
    """
    uid = user_id or USER_ID
    agent = get_subconscious_agent(uid)
    return agent.get_full_context()


@mcp.tool()
def reset_subconscious_block(label: str, user_id: Optional[str] = None) -> str:
    """
    Reset a subconscious memory block to default.

    Args:
        label: Block label to reset
        user_id: Optional user ID override

    Returns:
        Confirmation message
    """
    uid = user_id or USER_ID
    agent = get_subconscious_agent(uid)
    agent.reset_block(label)
    return f"Reset block '{label}' to default"


@mcp.tool()
def clear_subconscious_block(label: str, user_id: Optional[str] = None) -> str:
    """
    Clear a subconscious memory block.

    Args:
        label: Block label to clear
        user_id: Optional user ID override

    Returns:
        Confirmation message
    """
    uid = user_id or USER_ID
    agent = get_subconscious_agent(uid)
    agent.clear_block(label)
    return f"Cleared block '{label}'"


def _bridge_subconscious_to_memories(agent, uid: str) -> int:
    """Bridge subconscious block extractions into the memory store.

    Reads the most recent items from user_preferences, pending_items,
    and session_patterns blocks and stores each as a deduplicated memory.

    Returns the number of new memories stored.
    """
    stored = 0
    now = datetime.now(timezone.utc).isoformat()
    block_map = [
        (USER_PREFERENCES, "preference"),
        (PENDING_ITEMS, "pending"),
        (SESSION_PATTERNS, "pattern"),
    ]

    for block_name, category in block_map:
        block = agent.state.get_block(block_name)
        if not block or block.is_empty():
            continue

        # Block content is newline-separated entries like:
        #   - [2026-04-20 12:00] some text
        lines = [ln.strip() for ln in block.content.splitlines() if ln.strip()]
        # Take the last 5 items to avoid replaying the entire history
        recent = lines[-5:]

        for line in recent:
            content = f"[{block_name}] {line}"
            conn = get_db_connection()
            existing = conn.execute(
                "SELECT id, activation_count FROM memories "
                "WHERE user_id = ? AND content = ? AND is_ghost = 0 "
                "ORDER BY created_at DESC LIMIT 1",
                (uid, content),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE memories SET activation_count = activation_count + 1, "
                    "updated_at = ? WHERE id = ?",
                    (now, existing["id"]),
                )
                conn.commit()
                conn.close()
                continue

            mid = hashlib.sha256(
                f"{content}{now}".encode()
            ).hexdigest()[:16]
            conn.execute(
                "INSERT OR IGNORE INTO memories "
                "(id, content, scope, retention, category, user_id, bank_id, "
                "created_at, updated_at, tags, emotional_context, metrics, "
                "is_ghost, synthesized_from) "
                "VALUES (?, ?, 'arc', 'long_term', ?, ?, ?, ?, ?, '[]', '{}', '{}', 0, '[]')",
                (mid, content, category, uid, BANK_ID, now, now),
            )
            conn.commit()
            conn.close()
            stored += 1

    return stored


def _bridge_transcript_entities(messages: List[dict], uid: str) -> int:
    """Run entity extraction on transcript content and persist found entities.

    Returns the number of entities stored.
    """
    from .entity_extractor import get_entity_extractor
    from .graph_store import get_graph_store

    # Combine user messages for extraction (skip system/assistant noise)
    user_content = " ".join(
        msg.get("content", "")
        for msg in messages
        if msg.get("role") == "user"
    )[:3000]  # Truncate to avoid excessive extraction

    if not user_content.strip():
        return 0

    extractor = get_entity_extractor()
    result = _run_async(extractor.extract(user_content))

    if not result.entities:
        return 0

    store = get_graph_store()
    store.process_extraction_result(result, uid)

    return len(result.entities)


@mcp.tool()
def process_session_transcript(
    session_id: str,
    messages: List[dict],
    project_path: Optional[str] = None,
    user_id: Optional[str] = None
) -> str:
    """
    Process a session transcript and extract memories.

    Args:
        session_id: Unique session identifier
        messages: List of message dicts with role/content
        project_path: Optional project path for context
        user_id: Optional user ID override

    Returns:
        Confirmation message
    """
    uid = user_id or USER_ID
    agent = get_subconscious_agent(uid)

    _run_async(agent.process_transcript(
        session_id=session_id,
        messages=messages,
        project_path=project_path
    ))

    # Bridge subconscious extraction to memory store
    _bridge_subconscious_to_memories(agent, uid)

    # Run entity extraction on transcript content and store entities
    _bridge_transcript_entities(messages, uid)

    return f"Processed transcript for session {session_id}"


# =============================================================================
# WebSocket Subscription Tools
# =============================================================================

_subscription_manager: Optional[SubscriptionManager] = None


def get_subscription_manager() -> SubscriptionManager:
    """Get or create the global subscription manager."""
    global _subscription_manager
    if _subscription_manager is None:
        _subscription_manager = SubscriptionManager()
    return _subscription_manager


@mcp.tool()
def ws_subscribe(
    subscription_id: str,
    event_types: List[str],
    entity_filter: Optional[str] = None,
    user_id: Optional[str] = None,
) -> str:
    """
    Subscribe to real-time events via WebSocket.

    Args:
        subscription_id: Unique subscription identifier
        event_types: List of event types (e.g., ["memory.stored", "memory.updated"])
        entity_filter: Optional filter (e.g., "memory:*" or "memory:123")
        user_id: Optional user ID

    Returns:
        Subscription confirmation
    """
    manager = get_subscription_manager()
    uid = user_id or USER_ID

    _run_async(
        manager.subscribe(
            subscription_id=subscription_id,
            connection_id=uid,
            event_types=event_types,
            entity_filter=entity_filter,
        )
    )

    return f"Subscribed to {', '.join(event_types)} with filter '{entity_filter or '*'}"


@mcp.tool()
def ws_unsubscribe(subscription_id: str) -> str:
    """
    Unsubscribe from real-time events.

    Args:
        subscription_id: Subscription identifier to remove

    Returns:
        Unsubscription confirmation
    """
    manager = get_subscription_manager()

    if _run_async(manager.unsubscribe(subscription_id)):
        return f"Unsubscribed {subscription_id}"
    return f"Subscription {subscription_id} not found"


@mcp.tool()
def ws_status() -> str:
    """
    Get WebSocket subscription status.

    Returns:
        JSON status of subscriptions
    """
    manager = get_subscription_manager()
    stats = manager.get_stats()
    return json.dumps(stats, indent=2)


@mcp.tool()
def ws_list_subscriptions(user_id: Optional[str] = None) -> str:
    """
    List active subscriptions for a user.

    Args:
        user_id: Optional user ID filter

    Returns:
        List of active subscriptions
    """
    manager = get_subscription_manager()
    uid = user_id or USER_ID

    # Filter subscriptions by connection
    user_subs = [
        sub for sub in manager._subscriptions.values()
        if sub.connection_id == uid
    ]

    if not user_subs:
        return "No active subscriptions"

    lines = ["Active subscriptions:", ""]
    for sub in user_subs:
        lines.append(f"- {sub.id}")
        lines.append(f"  Events: {', '.join(et.value for et in sub.event_types)}")
        if sub.entity_filter:
            lines.append(f"  Filter: {sub.entity_filter}")
        lines.append(f"  Status: {sub.status.value}")
        lines.append("")

    return "\n".join(lines)


# =============================================================================
# Audit Trail Projections Tools
# =============================================================================

_projection_builder: Optional[ProjectionBuilder] = None


def get_projection_builder() -> ProjectionBuilder:
    """Get or create the global projection builder."""
    global _projection_builder
    if _projection_builder is None:
        _projection_builder = ProjectionBuilder()
    return _projection_builder


@mcp.tool()
def audit_build(
    report_name: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    user_filter: Optional[str] = None,
) -> str:
    """
    Build an audit trail projection report.

    Args:
        report_name: One of: memory_timeline, user_activity, block_changes, access_log, anomaly_report
        start_date: Optional ISO date filter
        end_date: Optional ISO date filter
        user_filter: Optional user ID filter

    Returns:
        JSON formatted report data
    """
    builder = get_projection_builder()
    report = builder.get_report(report_name)

    if not report:
        return f"Unknown report: {report_name}. Available: {builder.list_reports()}"

    # Get events from event store
    from .event_bus import get_event_bus
    event_bus = get_event_bus()
    events = event_bus._store.get_all(limit=1000) if event_bus._store else []

    # Build report
    data = report.build(events)

    # Apply filters
    if start_date or end_date:
        from datetime import datetime
        start = datetime.fromisoformat(start_date) if start_date else None
        end = datetime.fromisoformat(end_date) if end_date else None
        data = report.filter_by_date(data, start, end)

    if user_filter:
        data = report.filter_by_user(data, user_filter)

    return json.dumps({
        "report": report_name,
        "record_count": len(data),
        "data": data
    }, indent=2)


@mcp.tool()
def audit_list_reports() -> str:
    """
    List available audit trail reports.

    Returns:
        List of report names
    """
    builder = get_projection_builder()
    reports = builder.list_reports()
    return json.dumps({
        "available_reports": reports,
        "count": len(reports)
    }, indent=2)


@mcp.tool()
def audit_export(
    report_name: str,
    format: str = "json",
    output_path: Optional[str] = None,
) -> str:
    """
    Export an audit trail report to file.

    Args:
        report_name: Report to export
        format: Output format (json or csv)
        output_path: Path to write file (default: ~/.foresight/reports/<report_name>.<format>)

    Returns:
        Path to generated file
    """
    import os
    from datetime import datetime

    builder = get_projection_builder()

    # Get events
    from .event_bus import get_event_bus
    event_bus = get_event_bus()
    events = event_bus._store.get_all(limit=1000) if event_bus._store else []

    # Default output path
    if not output_path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = Path.home() / ".foresight" / "reports"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = str(output_dir / f"{report_name}_{ts}.{format}")
    else:
        output_path = str(Path(output_path).expanduser())

    try:
        if format.lower() == "csv":
            builder.export_csv(report_name, events, output_path)
        else:
            builder.export_json(report_name, events, output_path)
        return f"Exported to: {output_path}"
    except Exception as e:
        return f"Export failed: {e}"


@mcp.tool()
def audit_summary() -> str:
    """
    Get summary of all audit trail projections.

    Returns:
        Summary statistics for all reports
    """
    builder = get_projection_builder()

    # Get events
    from .event_bus import get_event_bus
    event_bus = get_event_bus()
    events = event_bus._store.get_all(limit=1000) if event_bus._store else []

    summary = builder.get_report_summary(events)
    return json.dumps(summary, indent=2)


# =============================================================================
# In-Context Memory Injection
# =============================================================================

_STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "can", "this", "that",
    "these", "those", "i", "you", "he", "she", "it", "we", "they",
    "me", "him", "her", "us", "them", "my", "your", "his", "its",
    "our", "their", "and", "but", "or", "not", "no", "so", "if",
    "then", "than", "too", "very", "just", "about", "also", "with",
    "from", "into", "for", "on", "at", "to", "of", "in", "by", "up",
    "out", "off", "all", "some", "any", "each", "every", "both",
    "few", "more", "most", "other", "such", "only", "own", "same",
    "what", "when", "where", "who", "how", "why", "which", "while",
    "during", "before", "after", "above", "below", "between",
    "under", "again", "further", "once",
})


def _extract_terms(text: str) -> list[str]:
    """Extract key terms from text by splitting, lowering, and filtering stop words and short tokens."""
    words = text.lower().split()
    return [w for w in words if len(w) > 3 and w not in _STOP_WORDS]


def _score_memory_relevance(
    memory: sqlite3.Row,
    terms: list[str],
    now: datetime,
) -> float:
    """Compute a relevance score for a memory row given search terms.

    Score = term_overlap_count + importance_boost + recency_decay

    - term_overlap_count: how many of the search terms appear in the memory content
    - importance_boost: the stored importance value (default 1.0)
    - recency_decay: exponential decay based on age in days (half-life ~7 days)
    """
    content_lower = (memory["content"] or "").lower()

    # Term overlap: count how many distinct search terms appear in content
    overlap = sum(1 for t in terms if t in content_lower)

    # Importance boost: use stored importance, defaulting to 1.0 for older rows
    importance = memory["importance"] if memory["importance"] is not None else 1.0

    # Recency decay: exponential with ~7-day half-life
    created_str = memory["created_at"]
    try:
        created = datetime.fromisoformat(created_str)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_hours = max((now - created).total_seconds() / 3600, 0)
    except (ValueError, TypeError):
        age_hours = 0
    half_life_hours = 168.0  # 7 days
    decay = 0.5 ** (age_hours / half_life_hours)

    return overlap + importance * 0.5 + decay * 0.5


@mcp.tool()
def inject_context(
    conversation_text: str,
    user_id: Optional[str] = None,
    max_memories: int = 5,
    min_relevance: float = 0.3,
) -> str:
    """Surface relevant memories based on conversation context.

    Analyzes conversation text to find and return the most relevant memories
    for grounding the AI's responses in prior context.

    Args:
        conversation_text: The current conversation text to analyze for context
        user_id: Optional user ID override
        max_memories: Maximum number of memories to return (default: 5)
        min_relevance: Minimum relevance score threshold (default: 0.3)

    Returns:
        Structured context block with relevant memories and subconscious patterns
    """
    uid = user_id or USER_ID
    terms = _extract_terms(conversation_text)
    now = datetime.now(timezone.utc)

    # Query candidate memories that match any term via LIKE
    conn = get_db_connection()
    candidates: list[sqlite3.Row] = []

    if terms:
        # Build OR-clause of LIKE conditions with proper ESCAPE
        conditions = []
        params: list[str] = []
        for term in terms:
            escaped = term.replace("!", "!!").replace("%", "!%").replace("_", "!_")
            conditions.append("content LIKE ? ESCAPE '!'")
            params.append(f"%{escaped}%")

        where_clause = " OR ".join(conditions)
        query = (
            f"SELECT * FROM memories "
            f"WHERE user_id = ? AND tenant_id = ? AND is_ghost = 0 "
            f"AND ({where_clause}) "
            f"ORDER BY importance DESC, created_at DESC LIMIT 50"
        )
        candidates = conn.execute(
            query,
            [uid, TENANT_ID] + params,
        ).fetchall()

    # Also fetch high-importance memories as fallback (even without term match)
    fallback = conn.execute(
        "SELECT * FROM memories "
        "WHERE user_id = ? AND tenant_id = ? AND is_ghost = 0 "
        "AND importance >= ? "
        "ORDER BY importance DESC, created_at DESC LIMIT 20",
        (uid, TENANT_ID, min_relevance),
    ).fetchall()

    conn.close()

    # Merge candidates and fallback, deduplicate by id
    seen_ids: set[str] = set()
    all_rows: list[sqlite3.Row] = []
    for row in candidates + fallback:
        if row["id"] not in seen_ids:
            seen_ids.add(row["id"])
            all_rows.append(row)

    # Score and sort
    scored = [
        (row, _score_memory_relevance(row, terms, now))
        for row in all_rows
    ]
    scored.sort(key=lambda pair: pair[1], reverse=True)

    # Filter by minimum relevance
    top = [(row, score) for row, score in scored if score >= min_relevance]
    top = top[:max_memories]

    # Build structured context block
    lines: list[str] = []
    if top:
        lines.append(f"[Relevant Context - {len(top)} memories surfaced]")
        for row, score in top:
            importance_val = row["importance"] if row["importance"] is not None else 1.0
            snippet = (row["content"] or "")[:120]
            if len(row["content"] or "") > 120:
                snippet += "..."
            lines.append(
                f"- [{row['id']}] (importance: {importance_val:.1f}) {snippet}"
            )

    # Check subconscious blocks for relevant preferences/patterns
    sub_lines = _subconscious_context_for_terms(uid, terms)
    if sub_lines:
        if not top:
            lines.append(f"[Relevant Context - 0 memories surfaced]")
        lines.append("")
        lines.append("[Subconscious Patterns]")
        lines.extend(sub_lines)

    if not lines:
        return "[Relevant Context - 0 memories surfaced]\nNo relevant memories found for this conversation."

    return "\n".join(lines)


def _subconscious_context_for_terms(
    uid: str,
    terms: list[str],
) -> list[str]:
    """Check subconscious blocks for content relevant to the search terms.

    Returns a list of formatted lines with matching block content.
    """
    agent = get_subconscious_agent(uid)
    relevant_labels = [USER_PREFERENCES, SESSION_PATTERNS, PENDING_ITEMS]
    lines: list[str] = []

    for label in relevant_labels:
        block = agent.state.get_block(label)
        if not block or block.is_empty():
            continue
        content = block.content
        # Check if any term appears in the block content
        content_lower = content.lower()
        if terms and any(t in content_lower for t in terms):
            # Include relevant lines from the block
            matching = []
            for line in content.splitlines():
                line_lower = line.lower().strip()
                if line_lower and any(t in line_lower for t in terms):
                    matching.append(line.strip())
            if matching:
                lines.append(f"[{label}]")
                for m in matching[:3]:  # Limit to 3 lines per block
                    lines.append(f"  {m}")

    return lines


def main():
    mcp.run()


if __name__ == "__main__":
    main()


# =============================================================================
# Multi-Tenant Isolation Functions
# =============================================================================

@dataclass
class TenantContext:
    """Tenant context for isolation."""
    tenant_id: str
    rate_limit: int = 100
    burst_limit: int = 20


_tenant_context: TenantContext | None = None


def get_tenant_context() -> TenantContext:
    """Get current tenant context."""
    global _tenant_context
    if _tenant_context is None:
        _tenant_context = TenantContext(
            tenant_id=TENANT_ID,
            rate_limit=DEFAULT_RATE_LIMIT,
            burst_limit=DEFAULT_BURST_LIMIT
        )
    return _tenant_context


def set_tenant_context(tenant_id: str) -> None:
    """Set tenant context for current session."""
    global _tenant_context
    _tenant_context = TenantContext(
        tenant_id=tenant_id,
        rate_limit=DEFAULT_RATE_LIMIT,
        burst_limit=DEFAULT_BURST_LIMIT
    )


@mcp.tool()
def create_tenant(tenant_id: str, name: str, rate_limit: int = 100, burst_limit: int = 20) -> str:
    """
    Create a new tenant with isolated rate limits.

    Args:
        tenant_id: Unique tenant identifier
        name: Human-readable tenant name
        rate_limit: Requests per minute limit
        burst_limit: Burst request limit

    Returns:
        Confirmation message
    """
    conn = get_db_connection()
    try:
        conn.execute("""
        INSERT INTO tenants (id, name, rate_limit, burst_limit, created_at)
        VALUES (?, ?, ?, ?, ?)
        """, (
            tenant_id, name, rate_limit, burst_limit,
            datetime.now(timezone.utc).isoformat()
        ))
        conn.commit()
        return f"Created tenant '{name}' ({tenant_id}) with rate_limit={rate_limit}/min, burst={burst_limit}"
    except sqlite3.IntegrityError:
        return f"Tenant '{tenant_id}' already exists"
    finally:
        conn.close()


@mcp.tool()
def get_tenant(tenant_id: str) -> str:
    """
    Get tenant configuration.

    Args:
        tenant_id: Tenant identifier

    Returns:
        Tenant configuration as JSON
    """
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    conn.close()

    if not row:
        return f"Tenant '{tenant_id}' not found"

    return json.dumps({
        "id": row["id"],
        "name": row["name"],
        "rate_limit": row["rate_limit"],
        "burst_limit": row["burst_limit"],
        "created_at": row["created_at"],
        "config": json.loads(row["config"])
    }, indent=2)


@mcp.tool()
def list_tenants() -> str:
    """
    List all tenants.

    Returns:
        List of tenants with their configurations
    """
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM tenants ORDER BY created_at").fetchall()
    conn.close()

    if not rows:
        return "No tenants found"

    result = ["Tenants:", ""]
    for row in rows:
        result.append(f"- {row['id']}: {row['name']} (rate={row['rate_limit']}/min, burst={row['burst_limit']})")
    return "\n".join(result)


@mcp.tool()
def update_tenant_config(tenant_id: str, config: dict) -> str:
    """
    Update tenant configuration.

    Args:
        tenant_id: Tenant identifier
        config: Configuration dictionary to merge

    Returns:
        Updated configuration
    """
    conn = get_db_connection()

    # Get current config
    row = conn.execute("SELECT config FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    if not row:
        conn.close()
        return f"Tenant '{tenant_id}' not found"

    # Merge configs
    current = json.loads(row["config"])
    current.update(config)

    conn.execute("UPDATE tenants SET config = ? WHERE id = ?",
                 (json.dumps(current), tenant_id))
    conn.commit()
    conn.close()

    return f"Updated config for tenant '{tenant_id}'"


@mcp.tool()
def switch_tenant(tenant_id: str) -> str:
    """
    Switch current tenant context.

    Args:
        tenant_id: Tenant to switch to

    Returns:
        Confirmation message
    """
    # Verify tenant exists
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    conn.close()

    if not row:
        return f"Tenant '{tenant_id}' not found"

    # Update context
    set_tenant_context(tenant_id)
    return f"Switched to tenant '{tenant_id}'"


@mcp.tool()
def get_tenant_isolation_status() -> str:
    """
    Get multi-tenant isolation status.

    Returns:
        JSON status of isolation configuration
    """
    conn = get_db_connection()

    # Count tenants
    tenant_count = conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0]

    # Count memories per tenant
    tenant_memories = conn.execute("""
        SELECT tenant_id, COUNT(*) as count
        FROM memories
        GROUP BY tenant_id
    """).fetchall()

    conn.close()

    return json.dumps({
        "current_tenant": TENANT_ID,
        "total_tenants": tenant_count,
        "memories_by_tenant": [{"tenant_id": row[0], "count": row[1]} for row in tenant_memories],
        "isolation": "enabled"
    }, indent=2)


# =============================================================================
# Compliance Export Tools (HIPAA, SOC2, GDPR)
# =============================================================================

from .compliance import ComplianceExporter, ComplianceExport

@mcp.tool()
def compliance_hipaa_access_log(start_date: Optional[str] = None,
                                end_date: Optional[str] = None,
                                user_id: Optional[str] = None,
                                format: str = 'json') -> str:
    """
    Generate HIPAA-compliant access log.
    
    Args:
        start_date: ISO date filter (optional)
        end_date: ISO date filter (optional)
        user_id: Filter by specific user (optional)
        format: Output format (json or csv)
    
    Returns:
        HIPAA access log in specified format
    """
    from .event_bus import get_event_bus
    event_bus = get_event_bus()
    events = event_bus._store.get_all(limit=1000) if event_bus._store else []
    
    exporter = ComplianceExporter(events)
    export = exporter.hipaa_access_log(start_date, end_date, user_id)
    
    if format == 'csv':
        return exporter.to_csv(export)
    return exporter.to_json(export)


@mcp.tool()
def compliance_hipaa_modification_log(start_date: Optional[str] = None,
                                       end_date: Optional[str] = None,
                                       user_id: Optional[str] = None,
                                       format: str = 'json') -> str:
    """
    Generate HIPAA-compliant modification log.
    
    Args:
        start_date: ISO date filter (optional)
        end_date: ISO date filter (optional)
        user_id: Filter by specific user (optional)
        format: Output format (json or csv)
    
    Returns:
        HIPAA modification log in specified format
    """
    from .event_bus import get_event_bus
    event_bus = get_event_bus()
    events = event_bus._store.get_all(limit=1000) if event_bus._store else []
    
    exporter = ComplianceExporter(events)
    export = exporter.hipaa_modification_log(start_date, end_date, user_id)
    
    if format == 'csv':
        return exporter.to_csv(export)
    return exporter.to_json(export)


@mcp.tool()
def compliance_hipaa_user_activity(user_id: str,
                                    start_date: Optional[str] = None,
                                    end_date: Optional[str] = None,
                                    format: str = 'json') -> str:
    """
    Generate HIPAA user activity report.
    
    Args:
        user_id: User ID to report on (required)
        start_date: ISO date filter (optional)
        end_date: ISO date filter (optional)
        format: Output format (json or csv)
    
    Returns:
        HIPAA user activity report
    """
    from .event_bus import get_event_bus
    event_bus = get_event_bus()
    events = event_bus._store.get_all(limit=1000) if event_bus._store else []
    
    exporter = ComplianceExporter(events)
    export = exporter.hipaa_user_activity(user_id, start_date, end_date)
    
    if format == 'csv':
        return exporter.to_csv(export)
    return exporter.to_json(export)


@mcp.tool()
def compliance_soc2_change_history(start_date: Optional[str] = None,
                                    end_date: Optional[str] = None,
                                    format: str = 'json') -> str:
    """
    Generate SOC2 change history report.
    
    Args:
        start_date: ISO date filter (optional)
        end_date: ISO date filter (optional)
        format: Output format (json or csv)
    
    Returns:
        SOC2 change history report
    """
    from .event_bus import get_event_bus
    event_bus = get_event_bus()
    events = event_bus._store.get_all(limit=1000) if event_bus._store else []
    
    exporter = ComplianceExporter(events)
    export = exporter.soc2_change_history(start_date, end_date)
    
    if format == 'csv':
        return exporter.to_csv(export)
    return exporter.to_json(export)


@mcp.tool()
def compliance_soc2_access_review(user_ids: Optional[str] = None,
                                   format: str = 'json') -> str:
    """
    Generate SOC2 access control review report.
    
    Args:
        user_ids: Comma-separated user IDs (optional, all if omitted)
        format: Output format (json or csv)
    
    Returns:
        SOC2 access review report
    """
    from .event_bus import get_event_bus
    event_bus = get_event_bus()
    events = event_bus._store.get_all(limit=1000) if event_bus._store else []
    
    user_id_list = user_ids.split(',') if user_ids else None
    exporter = ComplianceExporter(events)
    export = exporter.soc2_access_review(user_id_list)
    
    if format == 'csv':
        return exporter.to_csv(export)
    return exporter.to_json(export)


@mcp.tool()
def compliance_soc2_monitoring(start_date: Optional[str] = None,
                                end_date: Optional[str] = None,
                                format: str = 'json') -> str:
    """
    Generate SOC2 monitoring report.
    
    Args:
        start_date: ISO date filter (optional)
        end_date: ISO date filter (optional)
        format: Output format (json or csv)
    
    Returns:
        SOC2 monitoring report
    """
    from .event_bus import get_event_bus
    event_bus = get_event_bus()
    events = event_bus._store.get_all(limit=1000) if event_bus._store else []
    
    exporter = ComplianceExporter(events)
    export = exporter.soc2_monitoring_report(start_date, end_date)
    
    if format == 'csv':
        return exporter.to_csv(export)
    return exporter.to_json(export)


@mcp.tool()
def compliance_gdpr_data_export(user_id: str,
                                include_deleted: bool = False,
                                format: str = 'json') -> str:
    """
    Generate GDPR data portability export.
    
    Args:
        user_id: User ID to export data for (required)
        include_deleted: Include deleted items (default: False)
        format: Output format (json or csv)
    
    Returns:
        GDPR data export
    """
    from .event_bus import get_event_bus
    event_bus = get_event_bus()
    events = event_bus._store.get_all(limit=1000) if event_bus._store else []
    
    exporter = ComplianceExporter(events)
    export = exporter.gdpr_data_export(user_id, include_deleted)
    
    if format == 'csv':
        return exporter.to_csv(export)
    return exporter.to_json(export)


@mcp.tool()
def compliance_gdpr_erasure_certification(user_id: str,
                                          deletion_date: Optional[str] = None,
                                          format: str = 'json') -> str:
    """
    Generate GDPR erasure certification.
    
    Args:
        user_id: User ID for erasure certification (required)
        deletion_date: ISO date of deletion (default: now)
        format: Output format (json or csv)
    
    Returns:
        GDPR erasure certification
    """
    from .event_bus import get_event_bus
    event_bus = get_event_bus()
    events = event_bus._store.get_all(limit=1000) if event_bus._store else []
    
    exporter = ComplianceExporter(events)
    export = exporter.gdpr_erasure_certification(user_id, deletion_date)
    
    if format == 'csv':
        return exporter.to_csv(export)
    return exporter.to_json(export)


@mcp.tool()
def compliance_save_report(report_name: str, output_path: str, 
                           format: str = 'json') -> str:
    """
    Save a compliance report to file.
    
    Args:
        report_name: Name of report to save (e.g., 'hipaa_access_log')
        output_path: Path to save the file
        format: Output format (json or csv)
    
    Returns:
        Path to saved file
    """
    from .event_bus import get_event_bus
    event_bus = get_event_bus()
    events = event_bus._store.get_all(limit=1000) if event_bus._store else []
    
    exporter = ComplianceExporter(events)
    
    # Map report names to functions
    report_funcs = {
        'hipaa_access_log': lambda: exporter.hipaa_access_log(),
        'hipaa_modification_log': lambda: exporter.hipaa_modification_log(),
        'soc2_change_history': lambda: exporter.soc2_change_history(),
        'soc2_access_review': lambda: exporter.soc2_access_review(),
        'soc2_monitoring': lambda: exporter.soc2_monitoring_report(),
        'gdpr_data_export': lambda: exporter.gdpr_data_export('default'),
        'gdpr_erasure_certification': lambda: exporter.gdpr_erasure_certification('default'),
    }
    
    if report_name not in report_funcs:
        return f"Unknown report: {report_name}. Available: {list(report_funcs.keys())}"
    
    export = report_funcs[report_name]()
    return exporter.save_to_file(export, output_path, format)


# =============================================================================
# Temporal Memory Tools
# =============================================================================

@mcp.tool()
def get_memories_from_window(
    window: str,
    user_id: Optional[str] = None,
    limit: int = 50,
    min_importance: float = 0.1,
    category: Optional[str] = None
) -> str:
    """
    Get memories from a time window.

    Args:
        window: Time window (today/week/month/year)
        user_id: Optional user ID override
        limit: Max results (default: 50)
        min_importance: Minimum importance threshold (default: 0.1)
        category: Optional category filter

    Returns:
        JSON list of memories with temporal metadata
    """
    from .temporal_queries import get_temporal_query_builder

    valid_windows = ['today', 'week', 'month', 'year']
    if window not in valid_windows:
        return f"Invalid window. Must be one of: {', '.join(valid_windows)}"

    uid = user_id or USER_ID
    builder = get_temporal_query_builder()

    results = builder.get_memories_from_window(
        user_id=uid,
        window=window,  # type: ignore
        limit=limit,
        min_importance=min_importance,
        category=category
    )

    return json.dumps([
        {
            'memory_id': r.memory_id,
            'content': r.content,
            'importance': r.importance,
            'strength_trend': r.strength_trend,
            'activation_count': r.activation_count,
            'created_at': r.created_at,
            'accessed_at': r.accessed_at,
            'category': r.category,
        }
        for r in results
    ], indent=2)


@mcp.tool()
def get_memories_by_trend(
    trend: str,
    user_id: Optional[str] = None,
    limit: int = 50,
    category: Optional[str] = None
) -> str:
    """
    Get memories by freshness trend.

    Args:
        trend: Trend type (stable/strengthening/weakening/stale)
        user_id: Optional user ID override
        limit: Max results (default: 50)
        category: Optional category filter

    Returns:
        JSON list of memories with the specified trend
    """
    from .temporal_queries import get_temporal_query_builder

    valid_trends = ['stable', 'strengthening', 'weakening', 'stale']
    if trend not in valid_trends:
        return f"Invalid trend. Must be one of: {', '.join(valid_trends)}"

    uid = user_id or USER_ID
    builder = get_temporal_query_builder()

    results = builder.get_memories_by_trend(
        user_id=uid,
        trend=trend,  # type: ignore
        limit=limit,
        category=category
    )

    return json.dumps([
        {
            'memory_id': r.memory_id,
            'content': r.content,
            'importance': r.importance,
            'strength_trend': r.strength_trend,
            'activation_count': r.activation_count,
            'created_at': r.created_at,
        }
        for r in results
    ], indent=2)


@mcp.tool()
def analyze_memory_trends(
    user_id: Optional[str] = None,
    timeframe: str = '30 days'
) -> str:
    """
    Analyze memory trends over time.

    Args:
        user_id: Optional user ID override
        timeframe: Timeframe for analysis (e.g., '30 days', '7 days')

    Returns:
        JSON with trend analysis including daily stats and category breakdown
    """
    from .temporal_queries import get_temporal_query_builder
    from .temporal_service import get_temporal_service

    uid = user_id or USER_ID
    builder = get_temporal_query_builder()
    service = get_temporal_service()

    # Get trend analysis
    trend_analysis = builder.analyze_trends(user_id=uid, timeframe=timeframe)

    # Get overall stats
    stats = service.get_memory_stats(user_id=uid)

    result = {
        'timeframe': timeframe,
        'stats': stats,
        'trend_analysis': trend_analysis,
    }

    return json.dumps(result, indent=2)


@mcp.tool()
def update_memory_decay(
    user_id: Optional[str] = None
) -> str:
    """
    Trigger batch decay update for all user memories.

    Should be run periodically (e.g., hourly) to keep importance values current.

    Args:
        user_id: Optional user ID override

    Returns:
        Number of memories updated
    """
    from .temporal_service import get_temporal_service

    uid = user_id or USER_ID
    service = get_temporal_service()

    count = service.batch_update_decay(user_id=uid)
    return f"Updated decay for {count} memories"


@mcp.tool()
def get_memory_stats(
    user_id: Optional[str] = None
) -> str:
    """
    Get temporal statistics for user memories.

    Args:
        user_id: Optional user ID override

    Returns:
        JSON with memory statistics including counts by trend
    """
    from .temporal_service import get_temporal_service

    uid = user_id or USER_ID
    service = get_temporal_service()

    stats = service.get_memory_stats(user_id=uid)
    return json.dumps(stats, indent=2)


@mcp.tool()
def run_temporal_migrations_tool() -> str:
    """
    Run temporal schema migrations.

    Adds temporal fields to memories table for decay tracking and trend analysis.

    Returns:
        Confirmation message
    """
    from .temporal_schema import run_temporal_migrations

    try:
        run_temporal_migrations(DB_PATH)
        return f"Temporal migrations completed successfully on {DB_PATH}"
    except Exception as e:
        return f"Migration failed: {e}"


# =============================================================================
# Entity and Graph Tools
# =============================================================================

@mcp.tool()
def extract_entities(
    content: str,
    user_id: Optional[str] = None
) -> str:
    """
    Extract entities and relationships from text.

    Args:
        content: Text to analyze
        user_id: Optional user ID override

    Returns:
        JSON with extracted entities and relationships
    """
    from .entity_extractor import get_entity_extractor

    uid = user_id or USER_ID
    extractor = get_entity_extractor()

    result = _run_async(extractor.extract(content))

    return json.dumps({
        'user_id': uid,
        'entities': [e.to_dict() for e in result.entities],
        'relationships': [r.to_dict() for r in result.relationships],
    }, indent=2)


@mcp.tool()
def get_entities_by_type(
    entity_type: str,
    user_id: Optional[str] = None,
    limit: int = 50
) -> str:
    """
    Get all entities of a specific type.

    Args:
        entity_type: Entity type (person/place/concept/event/emotion/object)
        user_id: Optional user ID override
        limit: Max results (default: 50)

    Returns:
        JSON list of entities
    """
    from .graph_store import get_graph_store

    valid_types = ['person', 'place', 'concept', 'event', 'emotion', 'object']
    if entity_type not in valid_types:
        return f"Invalid entity_type. Must be one of: {', '.join(valid_types)}"

    uid = user_id or USER_ID
    store = get_graph_store()

    entities = store.get_entities_by_type(uid, entity_type, limit)

    return json.dumps([e.to_dict() for e in entities], indent=2)


@mcp.tool()
def find_entities_by_name(
    name: str,
    user_id: Optional[str] = None,
    limit: int = 10
) -> str:
    """
    Find entities by name (partial match).

    Args:
        name: Name to search for
        user_id: Optional user ID override
        limit: Max results (default: 10)

    Returns:
        JSON list of matching entities
    """
    from .graph_store import get_graph_store

    uid = user_id or USER_ID
    store = get_graph_store()

    entities = store.find_entities_by_name(uid, name, limit)

    return json.dumps([e.to_dict() for e in entities], indent=2)


@mcp.tool()
def get_relationships(
    entity_id: str,
    user_id: Optional[str] = None,
    direction: str = 'both'
) -> str:
    """
    Get relationships for an entity.

    Args:
        entity_id: Entity ID
        user_id: Optional user ID override
        direction: Direction filter (in/out/both)

    Returns:
        JSON list of relationships
    """
    from .graph_store import get_graph_store

    valid_directions = ['in', 'out', 'both']
    if direction not in valid_directions:
        return f"Invalid direction. Must be one of: {', '.join(valid_directions)}"

    uid = user_id or USER_ID
    store = get_graph_store()

    relationships = store.get_relationships(entity_id, uid, direction)

    return json.dumps([r.to_dict() for r in relationships], indent=2)


@mcp.tool()
def traverse_graph(
    start_entity_id: str,
    user_id: Optional[str] = None,
    max_depth: int = 2,
    max_results: int = 50
) -> str:
    """
    Traverse graph from a starting entity.

    Args:
        start_entity_id: Starting entity ID
        user_id: Optional user ID override
        max_depth: Maximum traversal depth (default: 2)
        max_results: Maximum results (default: 50)

    Returns:
        JSON with traversed nodes and edges
    """
    from .graph_store import get_graph_store

    uid = user_id or USER_ID
    store = get_graph_store()

    result = store.traverse_graph(start_entity_id, uid, max_depth, max_results)

    return json.dumps({
        'nodes': [e.to_dict() for e in result.nodes],
        'edges': [r.to_dict() for r in result.edges],
    }, indent=2)


@mcp.tool()
def link_memory_to_entities(
    memory_id: str,
    entity_ids: list,
    user_id: Optional[str] = None
) -> str:
    """
    Link a memory to entities.

    Args:
        memory_id: Memory ID
        entity_ids: List of entity IDs to link
        user_id: Optional user ID override

    Returns:
        Confirmation message
    """
    from .graph_store import get_graph_store

    uid = user_id or USER_ID
    store = get_graph_store()

    store.link_memory_to_entities(memory_id, entity_ids, uid)

    return f"Linked memory {memory_id} to {len(entity_ids)} entities"


# =============================================================================
# Enhanced Synthesis Tools
# =============================================================================

@mcp.tool()
def enhanced_synthesize(
    user_id: Optional[str] = None,
    limit: int = 50,
    min_memories: int = 5
) -> str:
    """
    Perform enhanced synthesis over user memories.

    Extends base synthesis with contradiction detection, temporal trend
    analysis, and evidence-anchored insight generation.

    Args:
        user_id: Optional user ID override
        limit: Max memories to include in synthesis
        min_memories: Minimum memories required (default: 5)

    Returns:
        JSON with synthesis result including contradictions, trends, insights
    """
    from .enhanced_synthesizer import get_enhanced_synthesizer
    from .memory_types import MemoryObject, EmotionalMetadata

    uid = user_id or USER_ID
    conn = get_db_connection()

    rows = conn.execute(
        "SELECT * FROM memories WHERE user_id = ? AND tenant_id = ? AND is_ghost = 0 ORDER BY created_at DESC LIMIT ?",
        (uid, TENANT_ID, limit)
    ).fetchall()
    conn.close()

    if len(rows) < min_memories:
        return f"Need at least {min_memories} memories for synthesis. Insufficient data available."

    # Convert rows to MemoryObjects
    memories = []
    for r in rows:
        memories.append(MemoryObject(
            id=r['id'],
            timestamp=r['created_at'],
            scope=r['scope'],
            retention=r['retention'],
            content=r['content'],
            tags=json.loads(r['tags']),
            emotional_context=EmotionalMetadata(
                intensity=json.loads(r['emotional_context']).get('intensity', 0.5)
            ) if r['emotional_context'] else None,
        ))

    synthesizer = get_enhanced_synthesizer()

    result = _run_async(synthesizer.synthesize(memories, user_id=uid))

    if result is None:
        return "Synthesis could not be completed with available data."

    return json.dumps(result.to_dict(), indent=2)


# =============================================================================
# Hybrid Retrieval Tools
# =============================================================================

@mcp.tool()
def hybrid_search(
    query: str,
    user_id: Optional[str] = None,
    limit: int = 10,
    min_importance: float = 0.1,
    use_keyword: bool = True,
    use_graph: bool = True,
    use_temporal: bool = True,
) -> str:
    """
    Hybrid search combining keyword, graph, and temporal signals.

    Uses Reciprocal Rank Fusion (RRF) to merge results from all three
    retrieval strategies into a single ranked list.

    Args:
        query: Search query string
        user_id: Optional user ID override
        limit: Maximum results (default: 10)
        min_importance: Minimum importance threshold (default: 0.1)
        use_keyword: Enable keyword matching (default: true)
        use_graph: Enable graph traversal (default: true)
        use_temporal: Enable temporal scoring (default: true)

    Returns:
        JSON with merged search results and signal metadata
    """
    from .hybrid_retriever import get_hybrid_retriever

    uid = user_id or USER_ID
    retriever = get_hybrid_retriever()

    result = retriever.search(
        query=query,
        user_id=uid,
        tenant_id=TENANT_ID,
        limit=limit,
        min_importance=min_importance,
        use_keyword=use_keyword,
        use_graph=use_graph,
        use_temporal=use_temporal,
    )

    return json.dumps(result.to_dict(), indent=2)


# =============================================================================
# Reflection Engine Tools
# =============================================================================

@mcp.tool()
def run_reflection(
    user_id: Optional[str] = None,
    period: str = 'weekly',
) -> str:
    """
    Run a reflection analysis over user memories.

    Generates structured insights by analyzing temporal trends,
    entity patterns, and contradictions across a time period.

    Args:
        user_id: Optional user ID override
        period: Analysis period - 'weekly' or 'monthly' (default: weekly)

    Returns:
        JSON reflection report with insights and trend summary
    """
    from .reflection_engine import get_reflection_engine

    if period not in ('weekly', 'monthly'):
        return "Invalid period. Must be 'weekly' or 'monthly'."

    uid = user_id or USER_ID
    engine = get_reflection_engine()

    report = engine.reflect(uid, tenant_id=TENANT_ID, period=period)

    if report is None:
        return "Insufficient memories for reflection analysis."

    return json.dumps(report.to_dict(), indent=2)
