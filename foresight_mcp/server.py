#!/usr/bin/env python3
"""
Foresight MCP Server - Full memory system with psychological safety features.
Restored from src/lib/ai/memory/ architecture.
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware as _Middleware
from fastmcp.tools.base import ToolResult
from mcp.types import TextContent
from pydantic import BaseModel, Field

from .auth import AuthMiddleware
from .block_registry import InjectionPoint, initialize_default_blocks
from .config import (
    BANK_ID,
    DB_PATH,
    DEFAULT_BURST_LIMIT,
    DEFAULT_RATE_LIMIT,
    DEFAULT_TENANT_ID,
    USER_ID,
)
from .connection_pool import get_pool
from .context_blocks import (
    PENDING_ITEMS,
    SESSION_PATTERNS,
    USER_PREFERENCES,
    get_context_block_agent,
)
from .crisis_detection import get_crisis_service
from .decay_model import get_decay_model
from .document_layer import (
    DEFAULT_CHUNK_CHAR_BUDGET as _DOC_CHUNK_BUDGET,
    DocumentLayerError,
    content_hash as _content_hash,
    get_document_store,
)
from .enhanced_synthesizer import get_enhanced_synthesizer
from .entity_extractor import get_entity_extractor
from .event_bus import (
    curation_status_changed,
    get_event_bus,
    memory_deleted,
    memory_retrieved,
    memory_stored,
    memory_updated,
)
from .graph_store import get_graph_store
from .hybrid_retriever import HybridResult, get_hybrid_retriever
from .memory_components import (
    MemoryCrisisTagger,
    MemoryLinker,
    MemorySynthesizer,
    SocraticGate,
)
from .memory_relationships import (
    MemoryRelationshipError,
    get_memory_relationship_store,
)
from .memory_types import (
    EmotionalMetadata,
    EmpathyMetrics,
    MemoryObject,
    MemoryScope,
    RetentionPolicy,
)
from .profile_synthesizer import ProfileConfig, profile_to_prompt, synthesize_profile as _synthesize_profile
from .rate_limiter import RateLimitExceeded, get_rate_limiter
from .reflection_engine import get_reflection_engine
from .semantic_search import (
    DEFAULT_PROVIDER as _SEMANTIC_DEFAULT_PROVIDER,
    SemanticSearchError as _SemanticSearchError,
    get_semantic_search,
)
from .stream_producer import (
    KafkaProducer,
    KinesisProducer,
    StreamPublisher,
    create_stream_producer,
)
from .sync import Operation, OperationQueue, OperationType
from .temporal_queries import get_temporal_query_builder
from .temporal_service import get_temporal_service
from .tenant_context import get_current_tenant_id, set_current_tenant_id
from .tenant_middleware import TenantMiddleware
from .websocket.subscriptions import SubscriptionManager


# Tool argument grouping models
class MemoryOptions(BaseModel):
    category: str = Field(default="fact", description="Category label")
    scope: str = Field(default="session", description="Memory scope: session, arc, trait, or fact")
    retention: str = Field(
        default="short_term", description="Retention policy: ephemeral, short_term, long_term, or permanent"
    )
    importance: float = Field(default=0.5, description="Initial importance score (0.0 to 1.0)")
    emotional_context: dict[str, Any] | None = Field(
        default=None, description="Emotional metadata (valence, arousal, dominance, primary_emotion, intensity)"
    )
    metrics: dict[str, Any] | None = Field(
        default=None, description="Empathy metrics (reciprocity, validation_accuracy, resistance_level)"
    )


class MemoryUpdateOptions(BaseModel):
    content: str | None = Field(default=None, description="New memory content")
    category: str | None = Field(default=None, description="New category label")
    scope: str | None = Field(default=None, description="New memory scope")
    retention: str | None = Field(default=None, description="New retention policy")
    tags: list[str] | None = Field(default=None, description="New list of tags")


class SearchOptions(BaseModel):
    query_type: Literal["id", "keyword", "list"] = Field(default="keyword", description="Type of search/retrieval")
    query: str | None = Field(default=None, description="Search query string")
    memory_id: str | None = Field(default=None, description="Retrieve specific memory by ID")
    limit: int = Field(default=10, description="Maximum results")
    offset: int = Field(default=0, description="Result offset")
    min_importance: float = Field(default=0.1, description="Minimum importance threshold")
    use_hybrid: bool = Field(default=True, description="Enable hybrid search signals")


class ContextBlockAction(BaseModel):
    action: Literal["list", "get", "update", "reset", "clear"] = Field(description="Action to perform")
    label: str | None = Field(default=None, description="Block label (e.g. guidance, preferences)")
    content: str | None = Field(default=None, description="New content for update action")


class SubconsciousAction(ContextBlockAction):
    """Compatibility alias for the older subconscious-named tool contract."""


class CurationRunAction(BaseModel):
    action: Literal["create", "get", "list", "cancel", "archive"] = Field(description="Action to perform")
    run_id: str | None = Field(default=None, description="Curation run ID for get/cancel/archive")
    source_bank_id: str | None = Field(default=None, description="Source bank to curate from")
    output_bank_id: str | None = Field(default=None, description="Optional output bank for reviewable results")
    policy_mode: Literal["preserve", "rebalance", "rebuild"] = Field(
        default="rebalance", description="Curation policy mode"
    )
    tool_access: Literal["disabled", "observe", "operate"] = Field(
        default="observe", description="Curator tool-access policy"
    )
    output_mode: Literal["reviewable_output", "in_place"] = Field(
        default="reviewable_output",
        description="Whether curated results land in a reviewable output bank or in place",
    )
    instructions: str | None = Field(default=None, description="Optional curator instructions")
    transcript_bundle: list[dict[str, Any]] | None = Field(
        default=None, description="Optional transcript bundle to incorporate during curation"
    )
    session_id: str | None = Field(default=None, description="Optional session ID for transcript bundles")
    project_path: str | None = Field(default=None, description="Optional project path for transcript bundles")
    limit: int = Field(default=20, description="Maximum number of runs to return for list")


class EntityQueryType(BaseModel):
    query_type: Literal["by_type", "by_name", "relationships", "traverse"] = Field(description="Type of entity query")
    entity_type: str | None = Field(
        default=None, description="Entity type for 'by_type' (person/place/concept/event/emotion/object)"
    )
    name: str | None = Field(default=None, description="Name for 'by_name' partial match")
    entity_id: str | None = Field(default=None, description="Entity ID for 'relationships' or 'traverse'")
    direction: Literal["in", "out", "both"] = Field(default="both", description="Direction for relationships")
    max_depth: int = Field(default=2, description="Max depth for traversal")


class TemporalWindow(BaseModel):
    window: Literal["today", "week", "month", "year"] = Field(default="week", description="Time window for retrieval")
    trend: str | None = Field(default=None, description="Filter by trend (stable/strengthening/weakening/stale)")
    category: str | None = Field(default=None, description="Category filter")
    limit: int = Field(default=50, description="Max results")


class SystemStatusOptions(BaseModel):
    include_trends: bool = Field(default=False, description="Whether to include temporal trend analysis")
    timeframe: str = Field(default="30 days", description="Timeframe for trend analysis")


class EntityAction(BaseModel):
    action: Literal["extract", "link"] = Field(..., description="Action to perform")
    content: str | None = Field(default=None, description="Text content for extraction")
    memory_id: str | None = Field(default=None, description="Memory ID for linking")
    entity_ids: list[str] | None = Field(default=None, description="Entity IDs for linking")


class EntityQuery(BaseModel):
    query_type: Literal["by_type", "by_name", "relationships", "traverse"] = Field(..., description="Type of query")
    entity_type: str | None = Field(default=None, description="Entity type filter")
    name: str | None = Field(default=None, description="Name for partial match")
    entity_id: str | None = Field(default=None, description="Entity ID for relationships or starting traversal")
    direction: Literal["in", "out", "both"] = Field(default="both", description="Relationship direction")
    max_depth: int = Field(default=2, description="Traversal depth")
    limit: int = Field(default=50, description="Result limit")


class MemoryAction(BaseModel):
    action: Literal["store", "update", "delete", "archive"] = Field(..., description="Action to perform")
    memory_id: str | None = Field(default=None, description="Memory ID for update/delete/archive")
    content: str | None = Field(default=None, description="Content for store/update")
    options: MemoryOptions | None = Field(default=None, description="Options for store")
    updates: MemoryUpdateOptions | None = Field(default=None, description="Updates for update action")


class VersionAction(BaseModel):
    action: Literal["diff", "rollback"] = Field(..., description="Versioning action")
    memory_id: str = Field(..., description="Memory ID")
    version1: int | None = Field(default=None, description="First version for diff")
    version2: int | None = Field(default=None, description="Second version for diff")
    to_version: int | None = Field(default=None, description="Version to rollback to")


class AnalysisAction(BaseModel):
    action: Literal["synthesize", "reflect"] = Field(..., description="Analysis action")
    period: str = Field(default="weekly", description="Period for reflection")
    limit: int = Field(default=50, description="Limit for synthesis")
    enhanced: bool = Field(default=False, description="Whether to use enhanced synthesis")


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
    tid = tenant_id or get_current_tenant_id()
    # Look up tenant-specific limits from DB
    rate_limit = DEFAULT_RATE_LIMIT
    burst_limit = DEFAULT_BURST_LIMIT
    try:
        conn = get_db_connection()
        row = conn.execute("SELECT rate_limit, burst_limit FROM tenants WHERE id = ?", (tid,)).fetchone()
        conn.close()
        if row:
            rate_limit = row["rate_limit"] or DEFAULT_RATE_LIMIT
            burst_limit = row["burst_limit"] or DEFAULT_BURST_LIMIT
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
    return get_pool().acquire()


SCHEMA_VERSION = 9


def _seed_default_tenant(conn) -> None:
    """Insert the default tenant row if it does not already exist."""
    conn.execute(
        """
        INSERT OR IGNORE INTO tenants (id, name, rate_limit, burst_limit, created_at, config)
        VALUES (?, 'Default tenant', ?, ?, ?, '{}')
        """,
        (DEFAULT_TENANT_ID, DEFAULT_RATE_LIMIT, DEFAULT_BURST_LIMIT, datetime.now(timezone.utc).isoformat()),
    )


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
        "CREATE INDEX IF NOT EXISTS idx_memories_tenant ON memories(tenant_id)",
        "CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_memories_content ON memories(content)",
        "CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope)",
        "CREATE INDEX IF NOT EXISTS idx_memories_tags ON memories(tags)",
        "CREATE INDEX IF NOT EXISTS idx_versions_memory ON memory_versions(memory_id)",
        "CREATE INDEX IF NOT EXISTS idx_versions_tenant ON memory_versions(tenant_id)",
        "CREATE INDEX IF NOT EXISTS idx_versions_created ON memory_versions(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_tenants_id ON tenants(id)",
    ],
    2: [
        "ALTER TABLE memories ADD COLUMN accessed_at TEXT DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE memories ADD COLUMN importance REAL DEFAULT 1.0",
        "ALTER TABLE memories ADD COLUMN decay_rate REAL DEFAULT 0.01",
        "ALTER TABLE memories ADD COLUMN activation_count INTEGER DEFAULT 0",
        "ALTER TABLE memories ADD COLUMN retrieval_count INTEGER DEFAULT 0",
        "ALTER TABLE memories ADD COLUMN strength_trend TEXT DEFAULT 'stable'",
        "ALTER TABLE memories ADD COLUMN last_retrieved_at TEXT",
        "ALTER TABLE memories ADD COLUMN category TEXT DEFAULT 'general'",
        "CREATE INDEX IF NOT EXISTS idx_memories_user_created ON memories(user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_memories_user_accessed ON memories(user_id, accessed_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(user_id, importance DESC, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_memories_strength_trend ON memories(user_id, strength_trend, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(user_id, category, created_at DESC)",
        """CREATE TABLE IF NOT EXISTS decay_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            half_life_hours REAL DEFAULT 168.0,
            min_importance REAL DEFAULT 0.1,
            activation_boost REAL DEFAULT 1.2,
            strengthening_threshold INTEGER DEFAULT 5,
            stale_threshold REAL DEFAULT 0.2,
            UNIQUE(tenant_id, user_id, category)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_decay_config_tenant ON decay_config(tenant_id)",
    ],
    3: [
        """CREATE TABLE IF NOT EXISTS curation_runs (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            source_bank_id TEXT NOT NULL,
            output_bank_id TEXT NOT NULL,
            policy_mode TEXT NOT NULL,
            tool_access TEXT NOT NULL,
            output_mode TEXT NOT NULL,
            status TEXT NOT NULL,
            instructions TEXT,
            summary_json TEXT DEFAULT '{}',
            error_json TEXT DEFAULT '{}',
            created_at TEXT NOT NULL,
            started_at TEXT,
            ended_at TEXT,
            archived_at TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_curation_runs_tenant_user ON curation_runs(tenant_id, user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_curation_runs_status ON curation_runs(tenant_id, user_id, status, created_at DESC)",
    ],
    4: [
        """CREATE TABLE IF NOT EXISTS context_blocks (
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            label TEXT NOT NULL,
            content TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id, user_id, label)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_context_blocks_lookup ON context_blocks(tenant_id, user_id, updated_at DESC)",
    ],
    5: [
        "ALTER TABLE curation_runs ADD COLUMN transcript_bundle_json TEXT",
        "ALTER TABLE curation_runs ADD COLUMN session_id TEXT",
        "ALTER TABLE curation_runs ADD COLUMN project_path TEXT",
    ],
    6: [
        """CREATE TABLE IF NOT EXISTS memory_relationships (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            source_memory_id TEXT NOT NULL,
            target_memory_id TEXT NOT NULL,
            relationship_type TEXT NOT NULL
                CHECK(relationship_type IN (
                    'updates', 'extends', 'derives',
                    'contradicts', 'supports', 'related'
                )),
            confidence REAL DEFAULT 1.0 CHECK(confidence >= 0 AND confidence <= 1),
            metadata TEXT DEFAULT '{}',
            created_at TEXT NOT NULL,
            UNIQUE(tenant_id, user_id, source_memory_id, target_memory_id, relationship_type),
            FOREIGN KEY (source_memory_id) REFERENCES memories(id) ON DELETE CASCADE,
            FOREIGN KEY (target_memory_id) REFERENCES memories(id) ON DELETE CASCADE
        )""",
        "CREATE INDEX IF NOT EXISTS idx_memory_relationships_source ON memory_relationships(tenant_id, user_id, source_memory_id)",
        "CREATE INDEX IF NOT EXISTS idx_memory_relationships_target ON memory_relationships(tenant_id, user_id, target_memory_id)",
        "CREATE INDEX IF NOT EXISTS idx_memory_relationships_type ON memory_relationships(tenant_id, user_id, relationship_type)",
        "ALTER TABLE memories ADD COLUMN relation_type TEXT",
        "ALTER TABLE memories ADD COLUMN related_memory_id TEXT",
        "CREATE INDEX IF NOT EXISTS idx_memories_relation ON memories(tenant_id, user_id, relation_type)",
    ],
    7: [
        """CREATE TABLE IF NOT EXISTS memory_embeddings (
            memory_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            dimension INTEGER NOT NULL,
            vector BLOB NOT NULL,
            model_version TEXT DEFAULT '1',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id, user_id, memory_id, provider)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_memory_embeddings_user ON memory_embeddings(tenant_id, user_id, provider)",
    ],
    8: [
        """CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            title TEXT NOT NULL,
            source TEXT NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            char_count INTEGER NOT NULL,
            chunk_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            metadata TEXT DEFAULT '{}',
            UNIQUE(tenant_id, user_id, content_hash)
        )""",
        """CREATE TABLE IF NOT EXISTS document_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            memory_id TEXT,
            chunk_index INTEGER NOT NULL,
            start_offset INTEGER NOT NULL,
            end_offset INTEGER NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(document_id, chunk_index),
            FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
        )""",
        "CREATE INDEX IF NOT EXISTS idx_documents_user ON documents(tenant_id, user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(tenant_id, user_id, content_hash)",
        "CREATE INDEX IF NOT EXISTS idx_document_chunks_doc ON document_chunks(document_id, chunk_index)",
        "CREATE INDEX IF NOT EXISTS idx_document_chunks_memory ON document_chunks(memory_id)",
    ],
    9: [
        "ALTER TABLE memories ADD COLUMN current_strength REAL",
        "ALTER TABLE memories ADD COLUMN last_decay_at TEXT",
        "CREATE INDEX IF NOT EXISTS idx_memories_strength ON memories(tenant_id, user_id, current_strength)",
        """CREATE TABLE IF NOT EXISTS memory_decay_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            memory_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            old_strength REAL,
            new_strength REAL,
            decay_factor REAL,
            reason TEXT,
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_memory_decay_events_lookup ON memory_decay_events (tenant_id, user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_memory_decay_events_memory ON memory_decay_events (memory_id, created_at DESC)",
    ],
    10: [
        "ALTER TABLE memories ADD COLUMN content_hash TEXT",
        "CREATE INDEX IF NOT EXISTS idx_memories_content_hash ON memories(tenant_id, user_id, content_hash)",
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

    applied = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()}

    for version in sorted(_SCHEMA_MIGRATIONS):
        if version in applied:
            continue
        for stmt in _SCHEMA_MIGRATIONS[version]:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as e:
                err = str(e).lower()
                if "duplicate column" in err or "already exists" in err:
                    continue
                raise
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

    # Ensure the built-in default tenant always exists so tenant switches are stable.
    _seed_default_tenant(conn)
    conn.commit()

    # Migrate decay_config: add tenant_id if table exists without it
    try:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(decay_config)").fetchall()]
        if cols and "tenant_id" not in cols:
            conn.execute("ALTER TABLE decay_config ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_decay_config_tenant ON decay_config(tenant_id)")
            conn.commit()
    except sqlite3.OperationalError:
        pass  # Table doesn't exist yet; will be created by migrations

    # Backfill content_hash for existing memories (v10 migration)
    try:
        rows = conn.execute("SELECT id, content FROM memories WHERE content_hash IS NULL").fetchall()
        if rows:
            for row in rows:
                h = _content_hash(row["content"])
                conn.execute("UPDATE memories SET content_hash = ? WHERE id = ?", (h, row["id"]))
            conn.commit()
    except sqlite3.OperationalError:
        pass  # Table doesn't exist yet; will be created by migrations

    conn.close()


# Initialize database on module load - deferred to runtime in main()
# init_db()  # Deferred initialization

# Initialize memory system components
_SERVER_STATE: dict[str, Any] = {
    "memory_system_initialized": False,
    "safe_path_prefixes": None,
    "stream_publisher": None,
    "subscription_manager": None,
    "tenant_context": None,
}

_CURATION_WORKERS: dict[str, threading.Thread] = {}
_CURATION_WORKERS_LOCK = threading.Lock()
_CURATION_CANCEL_SIGNALS: dict[str, threading.Event] = {}


class CurationError(RuntimeError):
    """Raised when a curation run is canceled before publication completes."""


# =============================================================================
# Version Management Functions
# =============================================================================


def get_memory_versions(memory_id: str, user_id: str | None = None) -> str:
    """Get all versions of a memory."""
    uid = user_id or USER_ID
    conn = get_db_connection()

    # Verify memory exists
    row = conn.execute(
        "SELECT * FROM memories WHERE id = ? AND user_id = ? AND tenant_id = ?",
        (memory_id, uid, get_current_tenant_id()),
    ).fetchone()

    if not row:
        conn.close()
        return f"Memory {memory_id} not found."

    # Get current version
    current_version = row["version"] if row else 1

    # Get version history
    versions = conn.execute(
        "SELECT * FROM memory_versions WHERE memory_id = ? AND tenant_id = ? ORDER BY version DESC",
        (memory_id, get_current_tenant_id()),
    ).fetchall()
    conn.close()

    if not versions:
        return f"Memory {memory_id} (version {current_version}): No version history found."

    result = [f"Memory {memory_id} - {len(versions)} versions:", ""]
    for v in versions:
        result.append(f"  v{v['version']}: {v['content'][:50]}...")
        result.append(f"    Created: {v['created_at']}")
        if v["rollback_of"]:
            result.append(f"    Rollback of: {v['rollback_of']}")

    return "\n".join(result)


def create_version_snapshot(memory_id: str, data: dict) -> str:
    """Create a new version snapshot for a memory."""
    version = data.get("version", 1)
    version_id = str(hashlib.sha256(f"{memory_id}:{version}".encode()).hexdigest())[:16]

    # Handle stringified inputs from DB rows
    tags = data.get("tags", "[]")
    emo = data.get("emotional_context")
    met = data.get("metrics")

    tags_json = tags if isinstance(tags, str) else json.dumps(tags)
    emo_json = emo if isinstance(emo, str) else json.dumps(emo)
    met_json = met if isinstance(met, str) else json.dumps(met)

    tenant_id = data.get("tenant_id") or get_current_tenant_id()

    conn = get_db_connection()
    conn.execute(
        """
    INSERT INTO memory_versions (
        id, memory_id, tenant_id, content, version, created_at, tags, emotional_context, metrics, rollback_of
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            version_id,
            memory_id,
            tenant_id,
            data["content"],
            version,
            datetime.now(timezone.utc).isoformat(),
            tags_json,
            emo_json,
            met_json,
            data.get("rollback_of"),
        ),
    )
    conn.commit()
    conn.close()
    return version_id


def rollback_to_version(memory_id: str, target_version: int, user_id: str | None = None) -> str:
    """Rollback a memory to a specific version."""
    uid = user_id or USER_ID
    conn = get_db_connection()

    # Verify memory ownership first
    current = conn.execute(
        "SELECT * FROM memories WHERE id = ? AND user_id = ? AND tenant_id = ?",
        (memory_id, uid, get_current_tenant_id()),
    ).fetchone()

    if not current:
        conn.close()
        return f"Memory {memory_id} not found"

    # Get the version content (tenant enforced via memory ownership above)
    version_row = conn.execute(
        "SELECT * FROM memory_versions WHERE memory_id = ? AND version = ? AND tenant_id = ?",
        (memory_id, target_version, get_current_tenant_id()),
    ).fetchone()

    if not version_row:
        conn.close()
        return f"Version {target_version} not found for memory {memory_id}"

    # Snapshot current state before rollback
    create_version_snapshot(
        memory_id=memory_id,
        data={
            "content": current["content"],
            "tags": current["tags"],
            "emotional_context": current["emotional_context"],
            "metrics": current["metrics"],
            "version": current["version"] or 1,
            "rollback_of": None,
            "tenant_id": current["tenant_id"],
        },
    )

    # Update to target version content
    new_version = target_version + 1
    conn.execute(
        """
    UPDATE memories SET
        content = ?, tags = ?, emotional_context = ?, metrics = ?,
        version = ?, updated_at = ?
    WHERE id = ? AND user_id = ?
    """,
        (
            version_row["content"],
            version_row["tags"],
            version_row["emotional_context"],
            version_row["metrics"],
            new_version,
            datetime.now(timezone.utc).isoformat(),
            memory_id,
            uid,
            get_current_tenant_id(),
        ),
    )
    conn.commit()
    conn.close()

    # Emit rollback event
    event_bus = get_event_bus_with_stream()
    event_bus.publish(
        memory_updated(
            memory_id=memory_id, old_content=current["content"], new_content=version_row["content"], actor=uid
        )
    )

    return f"Rolled back memory {memory_id} to version {target_version}"


def get_memory_diff(memory_id: str, version1: int, version2: int, _user_id: str | None = None) -> dict[str, Any]:
    """Get diff between two versions of a memory."""
    conn = get_db_connection()
    tenant_id = get_current_tenant_id()

    v1 = conn.execute(
        "SELECT * FROM memory_versions WHERE memory_id = ? AND version = ? AND tenant_id = ?",
        (memory_id, version1, tenant_id),
    ).fetchone()

    v2 = conn.execute(
        "SELECT * FROM memory_versions WHERE memory_id = ? AND version = ? AND tenant_id = ?",
        (memory_id, version2, tenant_id),
    ).fetchone()

    conn.close()

    if not v1 or not v2:
        return {"error": "One or both versions not found"}

    return {
        "memory_id": memory_id,
        "version1": {"version": version1, "content": v1["content"]},
        "version2": {"version": version2, "content": v2["content"]},
        "changed_fields": ["content"],
    }


# =============================================================================
# Memory System Components
# =============================================================================


def get_memory_system():
    """Get or initialize the memory system components."""
    if not _SERVER_STATE["memory_system_initialized"]:
        _SERVER_STATE["memory_system_initialized"] = True
    return {
        "tagger": MemoryCrisisTagger(get_crisis_service("high")),
        "gate": None,
        "synthesizer": MemorySynthesizer(),
        "linker": MemoryLinker(),
    }


class RateLimitMiddleware(_Middleware):
    """FastMCP middleware that enforces per-tenant rate limiting on tool calls."""

    async def on_call_tool(self, context, call_next):
        try:
            _check_rate_limit()
        except RateLimitExceeded as e:
            return ToolResult(
                content=[TextContent(type="text", text=str(e))],
                meta={"isError": True},
            )
        return await call_next(context)


_MAX_CONTENT_LENGTH = 100_000
_MAX_QUERY_LENGTH = 10_000
_MAX_LIMIT = 1000
_MAX_TENANT_ID_LENGTH = 64
_MAX_USER_ID_LENGTH = 128


def _validate_lengths(arguments: dict) -> str | None:
    for key in ("content", "conversation_text", "transcript"):
        val = arguments.get(key)
        if isinstance(val, str) and len(val) > _MAX_CONTENT_LENGTH:
            return f"{key} exceeds maximum length of {_MAX_CONTENT_LENGTH} characters"

    for key in ("query",):
        val = arguments.get(key)
        if isinstance(val, str) and len(val) > _MAX_QUERY_LENGTH:
            return f"{key} exceeds maximum length of {_MAX_QUERY_LENGTH} characters"
    return None


def _validate_numeric(arguments: dict) -> str | None:
    for key in ("limit",):
        val = arguments.get(key)
        if val is not None:
            try:
                limit_val = int(val)
                if limit_val > _MAX_LIMIT:
                    return f"limit cannot exceed {_MAX_LIMIT}"
                if limit_val < 0:
                    return "limit cannot be negative"
            except (ValueError, TypeError):
                return f"{key} must be an integer"
    for key in ("offset",):
        val = arguments.get(key)
        if val is not None:
            try:
                if int(val) < 0:
                    return "offset cannot be negative"
            except (ValueError, TypeError):
                return f"{key} must be a non-negative integer"
    return None


def _validate_ids(arguments: dict) -> str | None:
    for key in ("user_id", "tenant_id"):
        val = arguments.get(key)
        if val is not None and isinstance(val, str):
            if key == "tenant_id" and len(val) > _MAX_TENANT_ID_LENGTH:
                return f"{key} exceeds maximum length of {_MAX_TENANT_ID_LENGTH}"
            if key == "user_id" and len(val) > _MAX_USER_ID_LENGTH:
                return f"{key} exceeds maximum length of {_MAX_USER_ID_LENGTH}"
    return None


def _validate_paths(arguments: dict) -> str | None:
    if _SERVER_STATE["safe_path_prefixes"] is None:
        _SERVER_STATE["safe_path_prefixes"] = [
            str(Path.home()),
            os.getcwd(),
            "/tmp",
        ]

    for key in ("output_path", "path", "file_path"):
        val = arguments.get(key)
        if val is not None and isinstance(val, str):
            if ".." in val:
                return "Path traversal not allowed"
            if not any(val.startswith(p) for p in _SERVER_STATE["safe_path_prefixes"]):
                return f"Access to {val} is restricted"
    return None


def _validate_tool_inputs(_name: str, arguments: dict) -> str | None:
    """Validate tool inputs."""
    for validator in (_validate_lengths, _validate_numeric, _validate_ids, _validate_paths):
        error = validator(arguments)
        if error:
            return error
    return None


class InputValidationMiddleware(_Middleware):
    """FastMCP middleware that validates tool inputs."""

    async def on_call_tool(self, context, call_next):
        try:
            name = getattr(context, "name", None) or getattr(context, "tool_name", None) or ""
            arguments = getattr(context, "arguments", {}) or {}
            error = _validate_tool_inputs(name, arguments)
            if error:
                return ToolResult(
                    content=[TextContent(type="text", text=f"Validation error: {error}")],
                    meta={"isError": True},
                )
        except Exception:
            pass
        return await call_next(context)


mcp = FastMCP(
    "Foresight", middleware=[AuthMiddleware(), TenantMiddleware(), InputValidationMiddleware(), RateLimitMiddleware()]
)

logger = logging.getLogger("foresight_server")


def initialize_stream_producer():
    """Initialize stream producer for Kafka/Kinesis event publishing."""
    try:
        producer = create_stream_producer(environment=get_current_tenant_id() or "dev")
        publisher = StreamPublisher(producer, environment=get_current_tenant_id() or "dev")
        _SERVER_STATE["stream_publisher"] = publisher
        logger.info("Stream publisher initialized successfully")
        if isinstance(producer, KafkaProducer):
            logger.info(f"Using Kafka stream producer: {producer.bootstrap_servers}")
        elif isinstance(producer, KinesisProducer):
            logger.info("Using Kinesis stream producer")
        else:
            logger.info("Using mock stream producer")
        return publisher
    except Exception as e:
        logger.warning(f"Failed to initialize stream producer: {e}")
        return None


def get_stream_publisher():
    return _SERVER_STATE["stream_publisher"]


def cleanup_stream_producer():
    """Clean up stream producer."""
    publisher = _SERVER_STATE["stream_publisher"]
    if publisher:
        try:
            if hasattr(publisher.producer, "close"):
                publisher.producer.close()
            _SERVER_STATE["stream_publisher"] = None
            logger.info("Stream publisher closed")
        except Exception as e:
            logger.error(f"Error closing stream producer: {e}")
        finally:
            _SERVER_STATE["stream_publisher"] = None


atexit.register(cleanup_stream_producer)


def get_event_bus_with_stream():
    return get_event_bus(stream_publisher=_SERVER_STATE["stream_publisher"])


def _handle_memory_store(uid: str, tenant_id: str, options: MemoryAction) -> str:
    """Helper to handle memory storage."""
    if not options.content:
        return "Error: Content is required for 'store' action"

    opts = options.options or MemoryOptions()
    memory_id = hashlib.sha256(f"{options.content}{datetime.now(timezone.utc).isoformat()}".encode()).hexdigest()[:16]
    content_h = _content_hash(options.content.strip())

    conn = get_db_connection()
    existing = conn.execute(
        "SELECT id, activation_count FROM memories "
        "WHERE user_id = ? AND tenant_id = ? AND content_hash = ? AND is_ghost = 0 "
        "ORDER BY created_at DESC LIMIT 1",
        (uid, tenant_id, content_h),
    ).fetchone()

    if existing:
        conn.execute(
            "UPDATE memories SET activation_count = activation_count + 1, updated_at = ? "
            "WHERE id = ? AND user_id = ? AND tenant_id = ?",
            (datetime.now(timezone.utc).isoformat(), existing["id"], uid, tenant_id),
        )
        conn.commit()
        conn.close()
        return f"Duplicate detected - bumped activation for existing memory {existing['id']}"

    # Parse emotional context and metrics
    emo_ctx = EmotionalMetadata.from_dict(opts.emotional_context) if opts.emotional_context else None
    met = EmpathyMetrics.from_dict(opts.metrics) if opts.metrics else None

    memory = MemoryObject.create(
        content=options.content,
        scope=cast(MemoryScope, opts.scope),
        retention=cast(RetentionPolicy, opts.retention),
        emotional_context=emo_ctx,
        metrics=met,
    )
    memory.id = memory_id

    # Socratic Gate
    ms = get_memory_system()
    gate_result = _run_async(SocraticGate(ms["tagger"]).evaluate(memory, uid))
    memory.tags = gate_result.suggested_tags
    if opts.category and opts.category not in memory.tags:
        memory.tags.append(opts.category)

    # Store
    conn.execute(
        "INSERT INTO memories (id, user_id, tenant_id, category, scope, retention, "
        "content, content_hash, emotional_context, metrics, importance, activation_count, "
        "created_at, updated_at, tags) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            memory_id,
            uid,
            tenant_id,
            opts.category,
            memory.scope,
            memory.retention,
            options.content.strip(),
            content_h,
            json.dumps(opts.emotional_context) if opts.emotional_context else None,
            json.dumps(opts.metrics) if opts.metrics else None,
            opts.importance,
            1,
            datetime.now(timezone.utc).isoformat(),
            datetime.now(timezone.utc).isoformat(),
            json.dumps(memory.tags),
        ),
    )
    conn.commit()
    conn.close()

    get_event_bus_with_stream().publish(memory_stored(memory_id=memory_id, content=options.content, actor=uid))
    get_hybrid_retriever().invalidate_tfidf_cache(uid, tenant_id)
    return f"Stored memory {memory_id}. Gate: {gate_result.decision}. Reason: {gate_result.reason}"


def _handle_memory_update(uid: str, tenant_id: str, options: MemoryAction) -> str:
    """Helper to handle memory updates."""
    if not options.memory_id or not options.updates:
        return "Error: memory_id and updates required for 'update' action"

    conn = get_db_connection()
    row = conn.execute(
        "SELECT * FROM memories WHERE id = ? AND user_id = ? AND tenant_id = ?", (options.memory_id, uid, tenant_id)
    ).fetchone()

    if not row:
        conn.close()
        return f"Memory {options.memory_id} not found."

    updates_list = []
    values = []
    if options.updates.content:
        create_version_snapshot(
            options.memory_id,
            {
                "content": row["content"],
                "tags": row["tags"],
                "emotional_context": row["emotional_context"],
                "metrics": row["metrics"],
                "version": row["version"] or 1,
                "tenant_id": row["tenant_id"],
            },
        )
        updates_list.extend(["content = ?", "version = ?"])
        values.extend([options.updates.content.strip(), (row["version"] or 1) + 1])

    if options.updates.category:
        updates_list.append("category = ?")
        values.append(options.updates.category)
    if options.updates.scope:
        updates_list.append("scope = ?")
        values.append(options.updates.scope)
    if options.updates.retention:
        updates_list.append("retention = ?")
        values.append(options.updates.retention)
    if options.updates.tags:
        updates_list.append("tags = ?")
        values.append(json.dumps(options.updates.tags))

    if not updates_list:
        conn.close()
        return "No updates provided."

    updates_list.append("updated_at = ?")
    values.append(datetime.now(timezone.utc).isoformat())
    values.extend([options.memory_id, uid, tenant_id])
    conn.execute(
        f"UPDATE memories SET {', '.join(updates_list)} WHERE id = ? AND user_id = ? AND tenant_id = ?", values
    )
    conn.commit()
    conn.close()
    get_event_bus_with_stream().publish(
        memory_updated(
            memory_id=options.memory_id,
            old_content=row["content"],
            new_content=options.updates.content or row["content"],
            actor=uid,
        )
    )
    get_hybrid_retriever().invalidate_tfidf_cache(uid, tenant_id)
    return f"Updated memory {options.memory_id}"


def _handle_memory_delete(uid: str, tenant_id: str, memory_id: str | None) -> str:
    """Helper to handle memory deletion."""
    if not memory_id:
        return "Error: memory_id required for 'delete' action"

    conn = get_db_connection()
    if not conn.execute(
        "SELECT id FROM memories WHERE id = ? AND user_id = ? AND tenant_id = ?", (memory_id, uid, tenant_id)
    ).fetchone():
        conn.close()
        return f"Memory {memory_id} not found."

    get_event_bus_with_stream().publish(memory_deleted(memory_id=memory_id, actor=uid))
    conn.execute("DELETE FROM memories WHERE id = ? AND user_id = ? AND tenant_id = ?", (memory_id, uid, tenant_id))
    conn.commit()
    conn.close()
    get_hybrid_retriever().invalidate_tfidf_cache(uid, tenant_id)
    return f"Deleted memory {memory_id}"


def _handle_memory_archive(uid: str, tenant_id: str, memory_id: str | None) -> str:
    """Helper to handle memory archiving."""
    if not memory_id:
        return "Error: memory_id required for 'archive' action"

    conn = get_db_connection()
    row = conn.execute(
        "SELECT * FROM memories WHERE id = ? AND user_id = ? AND tenant_id = ?", (memory_id, uid, tenant_id)
    ).fetchone()

    if not row:
        conn.close()
        return f"Memory {memory_id} not found."
    row_dict = dict(row)
    if not row_dict.get("vector_id"):
        conn.close()
        return "Cannot archive memory without vector_id. Embed first."

    ms = get_memory_system()
    ghost = ms["linker"].to_ghost(
        MemoryObject(
            id=row_dict["id"],
            timestamp=row_dict["created_at"],
            scope=row_dict["scope"],
            retention=row_dict["retention"],
            content=row_dict["content"],
            tags=json.loads(row_dict["tags"]) or [],
            synthesized_from=json.loads(row_dict["synthesized_from"]) or [],
            is_ghost=bool(row_dict.get("is_ghost", 0)),
            vector_id=row_dict["vector_id"],
            gist=row_dict.get("gist"),
        )
    )
    conn.execute(
        "UPDATE memories SET content = ?, is_ghost = 1, gist = ? WHERE id = ? AND user_id = ? AND tenant_id = ?",
        (ghost.content, ghost.gist, memory_id, uid, tenant_id),
    )
    conn.commit()
    conn.close()
    return f"Archived memory {memory_id} to ghost node. Gist: {ghost.gist}"


@mcp.tool(output_schema=None)
def manage_memories(
    options: MemoryAction,
    user_id: str | None = None,
) -> str:
    """
    Manage memory lifecycle: store, update, delete, or archive.

    Args:
        options: Action and parameters
        user_id: Optional user ID override
    """
    uid = user_id or USER_ID
    tenant_id = get_current_tenant_id()

    if options.action == "store":
        return _handle_memory_store(uid, tenant_id, options)

    if options.action == "update":
        return _handle_memory_update(uid, tenant_id, options)

    if options.action == "delete":
        return _handle_memory_delete(uid, tenant_id, options.memory_id)

    if options.action == "archive":
        return _handle_memory_archive(uid, tenant_id, options.memory_id)

    return f"Unknown action: {options.action}"

    return f"Unknown action: {options.action}"


@mcp.tool(output_schema=None)
def search_memories(
    options: SearchOptions,
    user_id: str | None = None,
) -> str:
    """
    Unified search and retrieval for memories.
    Supports ID lookup, content keyword search, and hybrid retrieval.

    Args:
        options: Search parameters (query_type, query, memory_id, limit, etc.)
        user_id: Optional user ID override
    """
    uid = user_id or USER_ID
    tenant_id = get_current_tenant_id()

    # 1. Direct ID lookup
    if options.query_type == "id" or options.memory_id:
        mid = options.memory_id or options.query
        if not mid:
            return "Error: memory_id or query (as ID) required for id lookup."
        conn = get_db_connection()
        row = conn.execute(
            "SELECT * FROM memories WHERE id = ? AND user_id = ? AND tenant_id = ?", (mid, uid, tenant_id)
        ).fetchone()
        conn.close()

        if not row:
            return f"Memory {mid} not found."

        # Emit event
        event_bus = get_event_bus_with_stream()
        event_bus.publish(memory_retrieved(memory_id=mid, query_context="", actor=uid))

        tags = json.loads(row["tags"])
        result = f"[{row['id']}] ({row['scope']}/{row['retention']})\n"
        result += f"Content: {row['content']}\n"
        result += f"Tags: {', '.join(tags) if tags else 'none'}\n"
        if row["is_ghost"]:
            result += "[GHOST NODE - Content archived]"
        return result

    # 2. Hybrid search if enabled and query provided
    if options.use_hybrid and options.query:
        try:
            retriever = get_hybrid_retriever()
            hybrid_result = retriever.search(
                options.query, uid, tenant_id=tenant_id, limit=options.limit, min_importance=options.min_importance
            )
            if hybrid_result.results:
                results = []
                for r in hybrid_result.results:
                    signals = ", ".join(r.source_signals) if r.source_signals else "hybrid"
                    results.append(
                        f"- [{r.memory_id}] {r.content[:100]}... (score={r.combined_score:.3f}, signals={signals})"
                    )
                return f"Found {len(results)} memories (hybrid search):\n" + "\n".join(results)
        except Exception as e:
            logger.debug(f"Hybrid search failed: {e}")

    # 3. Fallback to basic list/keyword search
    conn = get_db_connection()
    if options.query:
        escaped = options.query.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        query_sql = (
            "SELECT * FROM memories WHERE user_id = ? AND tenant_id = ? AND content LIKE ? ESCAPE '!' LIMIT ? OFFSET ?"
        )
        params = (uid, tenant_id, f"%{escaped}%", options.limit, options.offset)
    else:
        query_sql = (
            "SELECT * FROM memories WHERE user_id = ? AND tenant_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?"
        )
        params = (uid, tenant_id, options.limit, options.offset)

    rows = conn.execute(query_sql, params).fetchall()
    conn.close()

    if not rows:
        return "No memories found."

    results = [f"- [{r['id']}] ({r['scope']}/{r['retention']}) {r['content'][:80]}..." for r in rows]
    return f"Memories ({len(results)} found):\n" + "\n".join(results)


# =============================================================================
# Memory Versioning Tools
# =============================================================================


def _handle_version_rollback(uid: str, tenant_id: str, options: VersionAction) -> str:
    """Helper to handle memory version rollback."""
    if options.to_version is None:
        return "Error: to_version required for rollback"

    conn = get_db_connection()
    row = conn.execute(
        "SELECT * FROM memories WHERE id = ? AND user_id = ? AND tenant_id = ?", (options.memory_id, uid, tenant_id)
    ).fetchone()
    if not row:
        conn.close()
        return f"Memory {options.memory_id} not found."

    version_row = conn.execute(
        "SELECT * FROM memory_versions WHERE memory_id = ? AND version = ? AND tenant_id = ?",
        (options.memory_id, options.to_version, tenant_id),
    ).fetchone()
    if not version_row:
        conn.close()
        return f"Version {options.to_version} not found for memory {options.memory_id}."

    version_id = hashlib.sha256(
        f"{options.memory_id}{row['version']}{datetime.now(timezone.utc).isoformat()}".encode()
    ).hexdigest()[:16]
    conn.execute(
        "INSERT INTO memory_versions (id, memory_id, tenant_id, content, version, created_at, tags, emotional_context, metrics, rollback_of) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            version_id,
            options.memory_id,
            tenant_id,
            row["content"],
            row["version"] or 1,
            datetime.now(timezone.utc).isoformat(),
            row["tags"],
            row["emotional_context"],
            row["metrics"],
            None,
        ),
    )
    new_version = options.to_version + 1
    conn.execute(
        "UPDATE memories SET content = ?, tags = ?, emotional_context = ?, metrics = ?, version = ?, updated_at = ? WHERE id = ? AND user_id = ? AND tenant_id = ?",
        (
            version_row["content"],
            version_row["tags"],
            version_row["emotional_context"],
            version_row["metrics"],
            new_version,
            datetime.now(timezone.utc).isoformat(),
            options.memory_id,
            uid,
            tenant_id,
        ),
    )
    conn.commit()
    conn.close()
    get_event_bus_with_stream().publish(
        memory_updated(
            memory_id=options.memory_id, old_content=row["content"], new_content=version_row["content"], actor=uid
        )
    )
    return f"Rolled back memory {options.memory_id} to version {options.to_version} (now at {new_version})"


def _handle_version_diff(uid: str, tenant_id: str, options: VersionAction) -> str:
    """Helper to handle memory version diff."""
    if options.version1 is None or options.version2 is None:
        return "Error: version1 and version2 required for diff"

    conn = get_db_connection()
    if not conn.execute(
        "SELECT id FROM memories WHERE id = ? AND user_id = ? AND tenant_id = ?", (options.memory_id, uid, tenant_id)
    ).fetchone():
        conn.close()
        return f"Memory {options.memory_id} not found."

    v1 = conn.execute(
        "SELECT content, created_at FROM memory_versions WHERE memory_id = ? AND version = ? AND tenant_id = ?",
        (options.memory_id, options.version1, tenant_id),
    ).fetchone()
    v2 = conn.execute(
        "SELECT content, created_at FROM memory_versions WHERE memory_id = ? AND version = ? AND tenant_id = ?",
        (options.memory_id, options.version2, tenant_id),
    ).fetchone()
    conn.close()

    if not v1:
        return f"Version {options.version1} not found."
    if not v2:
        return f"Version {options.version2} not found."

    res = [
        f"Diff for {options.memory_id}:",
        f"V{options.version1}: {v1['content'][:50]}...",
        f"V{options.version2}: {v2['content'][:50]}...",
    ]
    res.append("Changed." if v1["content"] != v2["content"] else "Identical.")
    return "\n".join(res)


@mcp.tool(output_schema=None)
def manage_memory_versions(options: VersionAction, user_id: str | None = None) -> str:
    """
    Manage memory versioning: diff or rollback.

    Args:
        options: Action and parameters
        user_id: Optional user ID override
    """
    uid = user_id or USER_ID
    tenant_id = get_current_tenant_id()

    if options.action == "rollback":
        return _handle_version_rollback(uid, tenant_id, options)

    if options.action == "diff":
        return _handle_version_diff(uid, tenant_id, options)

    return f"Unknown action: {options.action}"


# =============================================================================
# Context Block Tools
# =============================================================================


def _tool_response(*, ok: bool, action: str, **payload: Any) -> str:
    """Return a stable JSON envelope for tool responses."""
    body: dict[str, Any] = {"ok": ok, "action": action}
    body.update(payload)
    return json.dumps(body, indent=2)


def _tool_error(action: str, message: str, **payload: Any) -> str:
    """Return a stable JSON envelope for tool failures."""
    return _tool_response(ok=False, action=action, error={"message": message}, **payload)


def _handle_context_block_list(agent) -> str:
    """Helper for context block list action."""
    blocks = agent.get_all_blocks()
    return _tool_response(ok=True, action="list", blocks=blocks)


def _handle_context_block_get(agent, label: str) -> str:
    """Helper for context block get action."""
    content = agent.get_block(label)
    if content is not None:
        return _tool_response(ok=True, action="get", label=label, content=content)
    return _tool_error("get", f"Block '{label}' not found.", label=label)


def _handle_context_block_update(agent, label: str, content: str | None) -> str:
    """Helper for context block update action."""
    if content is None:
        return _tool_error("update", "'content' is required for update.", label=label)
    try:
        agent.update_block(label, content)
    except ValueError as exc:
        return _tool_error("update", str(exc), label=label)
    return _tool_response(ok=True, action="update", label=label, message=f"Updated block '{label}'")


@mcp.tool(output_schema=None)
def manage_context_blocks(options: ContextBlockAction, user_id: str | None = None) -> str:
    """
    Manage Foresight context blocks (guidance, preferences, context).

    Args:
        options: Action and parameters (list, get, update, reset, clear)
        user_id: Optional user ID override
    """
    uid = user_id or USER_ID
    tenant_id = get_current_tenant_id()
    agent = get_context_block_agent(uid, tenant_id)

    if options.action == "list":
        res = _handle_context_block_list(agent)
    elif not options.label:
        res = _tool_error(options.action, "'label' is required for this action.")
    elif options.action == "get":
        res = _handle_context_block_get(agent, options.label)
    elif options.action == "update":
        res = _handle_context_block_update(agent, options.label, options.content)
    elif options.action in ("reset", "clear"):
        try:
            if options.action == "reset":
                agent.reset_block(options.label)
            else:
                agent.clear_block(options.label)
            message = (
                f"Reset block '{options.label}' to default"
                if options.action == "reset"
                else f"Cleared block '{options.label}'"
            )
            res = _tool_response(
                ok=True,
                action=options.action,
                label=options.label,
                message=message,
            )
        except ValueError as exc:
            res = _tool_error(options.action, str(exc), label=options.label)
    else:
        res = _tool_error(options.action, f"Unsupported action: {options.action}")

    return res


@mcp.tool(output_schema=None)
def manage_subconscious(options: SubconsciousAction, user_id: str | None = None) -> str:
    """Legacy alias for manage_context_blocks()."""
    return manage_context_blocks(ContextBlockAction(**options.model_dump()), user_id=user_id)


def _bridge_context_blocks_to_memories(agent, uid: str) -> int:
    """Bridge extracted context block items into the memory store.

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
            tenant_id = get_current_tenant_id()
            content_h = _content_hash(content)
            conn = get_db_connection()
            existing = conn.execute(
                "SELECT id, activation_count FROM memories "
                "WHERE user_id = ? AND tenant_id = ? AND content_hash = ? AND is_ghost = 0 "
                "ORDER BY created_at DESC LIMIT 1",
                (uid, tenant_id, content_h),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE memories SET activation_count = activation_count + 1, updated_at = ? "
                    "WHERE id = ? AND user_id = ? AND tenant_id = ?",
                    (now, existing["id"], uid, tenant_id),
                )
                conn.commit()
                conn.close()
                continue

            mid = hashlib.sha256(f"{content}{now}".encode()).hexdigest()[:16]
            conn.execute(
                "INSERT OR IGNORE INTO memories "
                "(id, content, content_hash, scope, retention, category, user_id, bank_id, tenant_id, "
                "created_at, updated_at, tags, emotional_context, metrics, "
                "is_ghost, synthesized_from) "
                "VALUES (?, ?, ?, 'arc', 'long_term', ?, ?, ?, ?, ?, ?, '[]', '{}', '{}', 0, '[]')",
                (mid, content, content_h, category, uid, BANK_ID, tenant_id, now, now),
            )
            conn.commit()
            conn.close()
            stored += 1

    return stored


def _bridge_subconscious_to_memories(agent, uid: str) -> int:
    """Compatibility alias for the older helper name."""
    return _bridge_context_blocks_to_memories(agent, uid)


def _bridge_transcript_entities(messages: list[dict], uid: str) -> int:
    """Run entity extraction on transcript content and persist found entities.

    Returns the number of entities stored.
    """

    user_content = " ".join(msg.get("content", "") for msg in messages if msg.get("role") == "user")[:3000]

    if not user_content.strip():
        return 0

    extractor = get_entity_extractor()
    result = _run_async(extractor.extract(user_content))

    if not result.entities:
        return 0

    store = get_graph_store()
    store.process_extraction_result(result, uid)

    return len(result.entities)


@mcp.tool(output_schema=None)
def process_session_transcript(
    session_id: str, messages: list[dict], project_path: str | None = None, user_id: str | None = None
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
    tenant_id = get_current_tenant_id()
    agent = get_context_block_agent(uid, tenant_id)

    _run_async(agent.process_transcript(session_id=session_id, messages=messages, project_path=project_path))

    _bridge_context_blocks_to_memories(agent, uid)
    _bridge_transcript_entities(messages, uid)

    return f"Processed transcript for session {session_id}"


# =============================================================================
# Curation Run Tools
# =============================================================================


def _row_to_curation_run(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """Convert a curation run row into a JSON-friendly dict."""
    if row is None:
        return None
    summary = row["summary_json"] or "{}"
    error = row["error_json"] or "{}"
    return {
        "id": row["id"],
        "tenant_id": row["tenant_id"],
        "user_id": row["user_id"],
        "source_bank_id": row["source_bank_id"],
        "output_bank_id": row["output_bank_id"],
        "policy_mode": row["policy_mode"],
        "tool_access": row["tool_access"],
        "output_mode": row["output_mode"],
        "status": row["status"],
        "instructions": row["instructions"],
        "summary": json.loads(summary),
        "error": json.loads(error),
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "archived_at": row["archived_at"],
    }


def _curation_run_output_bank(
    run_id: str, _source_bank_id: str, output_mode: str, requested_output_bank: str | None
) -> str:
    """Resolve the effective output bank for a curation run."""
    if output_mode == "in_place":
        return f"curation:stage:{run_id}"
    return requested_output_bank or f"curation:{run_id}"


def _curation_archive_bank(run_id: str, source_bank_id: str) -> str:
    """Return the archive bank used when an in-place run replaces source rows."""
    return f"{source_bank_id}:archived:{run_id}"


def _fetch_curation_run(uid: str, tenant_id: str, run_id: str) -> sqlite3.Row | None:
    """Load a curation run row for a user."""
    conn = get_db_connection()
    row = conn.execute(
        "SELECT * FROM curation_runs WHERE id = ? AND user_id = ? AND tenant_id = ?",
        (run_id, uid, tenant_id),
    ).fetchone()
    conn.close()
    return row


def _curation_payload_from_row(row: sqlite3.Row) -> dict[str, Any]:
    """Rebuild a worker payload from a curation run row."""
    row_dict = dict(row)
    transcript_bundle_json = row_dict.get("transcript_bundle_json")
    return {
        "tenant_id": row_dict["tenant_id"],
        "user_id": row_dict["user_id"],
        "source_bank_id": row_dict["source_bank_id"],
        "output_bank_id": row_dict["output_bank_id"],
        "policy_mode": row_dict["policy_mode"],
        "tool_access": row_dict["tool_access"],
        "output_mode": row_dict["output_mode"],
        "instructions": row_dict["instructions"],
        "transcript_bundle": json.loads(transcript_bundle_json) if transcript_bundle_json else None,
        "session_id": row_dict.get("session_id"),
        "project_path": row_dict.get("project_path"),
    }


def _update_curation_run(
    run_id: str,
    tenant_id: str,
    *,
    status: str | None = None,
    summary: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
    archived_at: str | None = None,
) -> None:
    """Update mutable curation run fields."""
    updates: list[str] = []
    values: list[Any] = []
    if status is not None:
        updates.append("status = ?")
        values.append(status)
    if summary is not None:
        updates.append("summary_json = ?")
        values.append(json.dumps(summary))
    if error is not None:
        updates.append("error_json = ?")
        values.append(json.dumps(error))
    if started_at is not None:
        updates.append("started_at = ?")
        values.append(started_at)
    if ended_at is not None:
        updates.append("ended_at = ?")
        values.append(ended_at)
    if archived_at is not None:
        updates.append("archived_at = ?")
        values.append(archived_at)
    if not updates:
        return
    conn = get_db_connection()
    conn.execute(
        f"UPDATE curation_runs SET {', '.join(updates)} WHERE id = ? AND tenant_id = ?",
        (*values, run_id, tenant_id),
    )
    conn.commit()
    conn.close()


def _claim_curation_run(run_id: str, tenant_id: str, started_at: str) -> bool:
    """Atomically claim a pending curation run for execution."""
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            """UPDATE curation_runs
            SET status = 'running', started_at = ?, ended_at = NULL, error_json = '{}'
            WHERE id = ? AND tenant_id = ? AND status = 'pending'""",
            (started_at, run_id, tenant_id),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def _publish_curation_status(
    run_id: str,
    status: str,
    actor: str,
    **payload: Any,
) -> None:
    """Publish a curation lifecycle event."""
    get_event_bus_with_stream().publish(curation_status_changed(run_id, status, payload=payload, actor=actor))


def _get_curation_cancel_event(run_id: str) -> threading.Event:
    """Get or create the cancellation event for a curation run."""
    with _CURATION_WORKERS_LOCK:
        event = _CURATION_CANCEL_SIGNALS.get(run_id)
        if event is None:
            event = threading.Event()
            _CURATION_CANCEL_SIGNALS[run_id] = event
        return event


def _is_run_canceled(uid: str, tenant_id: str, run_id: str) -> bool:
    """Check whether a run has been canceled while background work is in progress."""
    if _get_curation_cancel_event(run_id).is_set():
        return True
    row = _fetch_curation_run(uid, tenant_id, run_id)
    return bool(row and row["status"] == "canceled")


def _raise_if_run_canceled(uid: str, tenant_id: str, run_id: str) -> None:
    """Abort background work when the run has been canceled."""
    if _is_run_canceled(uid, tenant_id, run_id):
        raise CurationError(f"Curation run {run_id} was canceled")


def _delete_existing_curation_outputs(uid: str, tenant_id: str, bank_id: str, run_id: str) -> None:
    """Remove stale outputs for a rerun or resumed run before writing fresh results."""
    conn = get_db_connection()
    try:
        conn.execute(
            "DELETE FROM memories WHERE user_id = ? AND tenant_id = ? AND bank_id = ? AND tags LIKE ?",
            (uid, tenant_id, bank_id, f'%"curation_run:{run_id}"%'),
        )
        conn.commit()
    finally:
        conn.close()


def _promote_in_place_curation(
    uid: str,
    tenant_id: str,
    run_id: str,
    source_bank_id: str,
    staging_bank_id: str,
    source_rows: list[sqlite3.Row],
    staged_ids: list[str],
    *,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Atomically archive original source rows and promote staged rows into the source bank."""
    archive_bank_id = _curation_archive_bank(run_id, source_bank_id)
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db_connection()
    try:
        if cancel_event and cancel_event.is_set():
            raise CurationError("Curation canceled before promotion started")
        if _is_run_canceled(uid, tenant_id, run_id):
            raise CurationError("Curation canceled before promotion started")
        conn.execute("BEGIN")
        if cancel_event and cancel_event.is_set():
            raise CurationError("Curation canceled before promotion committed")
        if source_rows:
            placeholders = ",".join("?" for _ in source_rows)
            conn.execute(
                f"UPDATE memories SET bank_id = ?, updated_at = ? WHERE user_id = ? AND tenant_id = ? AND id IN ({placeholders})",
                (archive_bank_id, now, uid, tenant_id, *(row["id"] for row in source_rows)),
            )
        if staged_ids:
            placeholders = ",".join("?" for _ in staged_ids)
            conn.execute(
                f"UPDATE memories SET bank_id = ?, updated_at = ? WHERE user_id = ? AND tenant_id = ? AND id IN ({placeholders})",
                (source_bank_id, now, uid, tenant_id, *staged_ids),
            )
        if cancel_event and cancel_event.is_set():
            raise CurationError("Curation canceled before promotion committed")
        if _is_run_canceled(uid, tenant_id, run_id):
            raise CurationError("Curation canceled before promotion committed")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "archive_bank_id": archive_bank_id,
        "promoted_memory_count": len(staged_ids),
        "archived_memory_count": len(source_rows),
        "staging_bank_id": staging_bank_id,
    }


def _restore_in_place_curation(
    uid: str,
    tenant_id: str,
    source_bank_id: str,
    staging_bank_id: str,
    source_rows: list[sqlite3.Row],
    staged_ids: list[str],
) -> None:
    """Restore source/staging banks if cancellation lands after promotion."""
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db_connection()
    try:
        conn.execute("BEGIN")
        if source_rows:
            placeholders = ",".join("?" for _ in source_rows)
            conn.execute(
                f"UPDATE memories SET bank_id = ?, updated_at = ? WHERE user_id = ? AND tenant_id = ? AND id IN ({placeholders})",
                (source_bank_id, now, uid, tenant_id, *(row["id"] for row in source_rows)),
            )
        if staged_ids:
            placeholders = ",".join("?" for _ in staged_ids)
            conn.execute(
                f"UPDATE memories SET bank_id = ?, updated_at = ? WHERE user_id = ? AND tenant_id = ? AND id IN ({placeholders})",
                (staging_bank_id, now, uid, tenant_id, *staged_ids),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _load_source_bank_rows(uid: str, tenant_id: str, bank_id: str, limit: int = 100) -> list[sqlite3.Row]:
    """Load source-bank memories for curation."""
    conn = get_db_connection()
    rows = conn.execute(
        """SELECT id, content, category, importance, strength_trend,
        activation_count, tags, emotional_context, created_at, scope, retention
        FROM memories
        WHERE user_id = ? AND tenant_id = ? AND bank_id = ? AND is_ghost = 0
        ORDER BY importance DESC, created_at DESC
        LIMIT ?""",
        (uid, tenant_id, bank_id, limit),
    ).fetchall()
    conn.close()
    return rows


def _build_synthesis_snapshot(rows: list[sqlite3.Row], uid: str) -> dict[str, Any] | None:
    """Run the enhanced synthesizer when enough source memories exist."""
    if len(rows) < 5:
        return None
    memories: list[MemoryObject] = []
    for row in rows:
        emo = json.loads(row["emotional_context"]) if row["emotional_context"] else {}
        memories.append(
            MemoryObject(
                id=row["id"],
                timestamp=row["created_at"],
                scope=row["scope"] or "arc",
                retention=row["retention"] or "long_term",
                content=row["content"],
                tags=json.loads(row["tags"]) if row["tags"] else [],
                emotional_context=EmotionalMetadata(intensity=emo.get("intensity", 0.5)) if emo else None,
            )
        )
    result = _run_async(get_enhanced_synthesizer().synthesize(memories, user_id=uid))
    return result.to_dict() if result else None


def _build_reflection_snapshot(rows: list[sqlite3.Row], uid: str, tenant_id: str) -> dict[str, Any] | None:
    """Build a non-persisting reflection summary from existing reflection primitives."""
    if not rows:
        return None
    engine = get_reflection_engine()
    conn = get_db_connection()
    try:
        trend_summary = engine._build_trend_summary(rows)
        entity_summary = engine._build_entity_summary(conn, uid, tenant_id)
        insights = [insight.to_dict() for insight in engine._generate_insights(rows, trend_summary, entity_summary)[:5]]
        return {
            "trend_summary": trend_summary,
            "entity_summary": entity_summary,
            "insights": insights,
        }
    finally:
        conn.close()


def _context_block_snapshot(uid: str, tenant_id: str) -> list[dict[str, Any]]:
    """Return non-empty context blocks for summary generation."""
    return cast(list[dict[str, Any]], get_context_block_agent(uid, tenant_id).get_all_blocks())


def _make_curated_entries(
    run: dict[str, Any],
    source_rows: list[sqlite3.Row],
    block_snapshot: list[dict[str, Any]],
    synthesis: dict[str, Any] | None,
    reflection: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Build curated output entries based on the configured policy mode."""
    tags = [
        f"curation_run:{run['id']}",
        f"source_bank:{run['source_bank_id']}",
        f"policy:{run['policy_mode']}",
    ]
    entries: list[dict[str, Any]] = []

    context_labels = [block["label"] for block in block_snapshot if block.get("content")]

    summary_lines = [
        f"Foresight Curator run {run['id']} completed in {run['output_mode']} mode.",
        f"Source bank: {run['source_bank_id']}",
        f"Output bank: {run['output_bank_id']}",
        f"Policy: {run['policy_mode']}",
        f"Tool access: {run['tool_access']}",
        f"Source memories considered: {len(source_rows)}",
    ]
    if run.get("instructions"):
        summary_lines.append(f"Instructions: {run['instructions']}")
    if context_labels:
        summary_lines.append(f"Context blocks considered: {', '.join(context_labels[:5])}")
    if synthesis:
        summary_lines.append(f"Synthesis contradictions: {len(synthesis.get('contradictions', []))}")
        summary_lines.append(f"Synthesis insights: {len(synthesis.get('insights', []))}")
    if reflection:
        summary_lines.append(f"Reflection overall trend: {reflection['trend_summary'].get('overall', 'stable')}")
        summary_lines.append(f"Reflection insights: {len(reflection.get('insights', []))}")

    entries.append(
        {
            "content": "\n".join(summary_lines),
            "category": "curation_summary",
            "scope": "arc",
            "retention": "long_term",
            "tags": [*tags, "summary"],
        }
    )

    if run["policy_mode"] == "preserve":
        for row in source_rows[:10]:
            entries.append(
                {
                    "content": row["content"],
                    "category": row["category"] or "curated_memory",
                    "scope": row["scope"] or "arc",
                    "retention": row["retention"] or "long_term",
                    "tags": [*tags, "preserved"],
                }
            )
    elif run["policy_mode"] == "rebalance":
        for row in source_rows[:5]:
            entries.append(
                {
                    "content": f"[Rebalanced] {row['content']}",
                    "category": row["category"] or "curated_memory",
                    "scope": row["scope"] or "arc",
                    "retention": row["retention"] or "long_term",
                    "tags": [*tags, "rebalanced"],
                }
            )
        if synthesis and synthesis.get("insights"):
            entries.append(
                {
                    "content": "\n".join(f"- {insight['summary']}" for insight in synthesis["insights"][:5]),
                    "category": "curation_insight",
                    "scope": "arc",
                    "retention": "long_term",
                    "tags": [*tags, "synthesis"],
                }
            )
    else:
        rebuilt_lines = []
        if synthesis and synthesis.get("insights"):
            rebuilt_lines.extend(f"- {insight['summary']}" for insight in synthesis["insights"][:5])
        elif reflection and reflection.get("insights"):
            rebuilt_lines.extend(f"- {insight['summary']}" for insight in reflection["insights"][:5])
        else:
            rebuilt_lines.extend(f"- {row['content']}" for row in source_rows[:5])
        entries.append(
            {
                "content": "Rebuilt memory bank:\n" + "\n".join(rebuilt_lines),
                "category": "curation_rebuild",
                "scope": "arc",
                "retention": "long_term",
                "tags": [*tags, "rebuilt"],
            }
        )

    return entries


def _insert_curation_entries(
    uid: str,
    tenant_id: str,
    bank_id: str,
    entries: list[dict[str, Any]],
    *,
    cancel_event: threading.Event | None = None,
) -> list[str]:
    """Persist curated output entries into the chosen bank."""
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db_connection()
    created_ids: list[str] = []
    try:
        conn.execute("BEGIN")
        for entry in entries:
            if cancel_event and cancel_event.is_set():
                raise CurationError("Curation canceled before staged output committed")
            memory_id = hashlib.sha256(f"{bank_id}:{entry['content']}:{uuid.uuid4().hex}".encode()).hexdigest()[:16]
            conn.execute(
                "INSERT INTO memories "
                "(id, content, scope, retention, category, user_id, bank_id, tenant_id, "
                "created_at, updated_at, tags, emotional_context, metrics, is_ghost, synthesized_from, importance) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', '{}', 0, '[]', ?)",
                (
                    memory_id,
                    entry["content"],
                    entry.get("scope", "arc"),
                    entry.get("retention", "long_term"),
                    entry.get("category", "curation"),
                    uid,
                    bank_id,
                    tenant_id,
                    now,
                    now,
                    json.dumps(entry.get("tags", [])),
                    entry.get("importance", 0.75),
                ),
            )
            created_ids.append(memory_id)
        if cancel_event and cancel_event.is_set():
            raise CurationError("Curation canceled before staged output committed")
        conn.commit()
        return created_ids
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _execute_curation_run(run_id: str, payload: dict[str, Any]) -> None:
    """Execute a queued curation run."""
    tenant_id = payload["tenant_id"]
    uid = payload["user_id"]
    cancel_event = _get_curation_cancel_event(run_id)
    set_current_tenant_id(tenant_id)
    queue = OperationQueue(DB_PATH)
    now = datetime.now(timezone.utc).isoformat()
    if not _claim_curation_run(run_id, tenant_id, now):
        return
    _publish_curation_status(run_id, "running", actor=uid, source_bank_id=payload["source_bank_id"])

    try:
        _raise_if_run_canceled(uid, tenant_id, run_id)
        if payload.get("transcript_bundle"):
            agent = get_context_block_agent(uid, tenant_id)
            session_id = payload.get("session_id") or f"curation-{run_id}"
            _run_async(
                agent.process_transcript(
                    session_id=session_id,
                    messages=payload["transcript_bundle"],
                    project_path=payload.get("project_path"),
                )
            )
            _bridge_context_blocks_to_memories(agent, uid)
            _bridge_transcript_entities(payload["transcript_bundle"], uid)

        _raise_if_run_canceled(uid, tenant_id, run_id)
        source_rows = _load_source_bank_rows(uid, tenant_id, payload["source_bank_id"])
        block_snapshot = _context_block_snapshot(uid, tenant_id)
        synthesis = None if payload["tool_access"] == "disabled" else _build_synthesis_snapshot(source_rows, uid)
        reflection = (
            None if payload["tool_access"] == "disabled" else _build_reflection_snapshot(source_rows, uid, tenant_id)
        )
        run = {
            "id": run_id,
            "source_bank_id": payload["source_bank_id"],
            "output_bank_id": payload["output_bank_id"],
            "policy_mode": payload["policy_mode"],
            "tool_access": payload["tool_access"],
            "output_mode": payload["output_mode"],
            "instructions": payload.get("instructions"),
        }
        entries = _make_curated_entries(run, source_rows, block_snapshot, synthesis, reflection)
        _delete_existing_curation_outputs(uid, tenant_id, payload["output_bank_id"], run_id)
        created_ids = _insert_curation_entries(
            uid,
            tenant_id,
            payload["output_bank_id"],
            entries,
            cancel_event=cancel_event,
        )
        _raise_if_run_canceled(uid, tenant_id, run_id)

        promotion_summary: dict[str, Any] = {}
        if payload["output_mode"] == "in_place":
            promotion_summary = _promote_in_place_curation(
                uid,
                tenant_id,
                run_id,
                payload["source_bank_id"],
                payload["output_bank_id"],
                source_rows,
                created_ids,
                cancel_event=cancel_event,
            )
            if cancel_event.is_set() or _is_run_canceled(uid, tenant_id, run_id):
                _restore_in_place_curation(
                    uid,
                    tenant_id,
                    payload["source_bank_id"],
                    payload["output_bank_id"],
                    source_rows,
                    created_ids,
                )
                raise CurationError("Curation canceled after promotion; restored source bank")

        summary = {
            "source_memory_count": len(source_rows),
            "output_memory_count": len(created_ids),
            "output_memory_ids": created_ids,
            "context_blocks_considered": [block["label"] for block in block_snapshot],
            "synthesis": synthesis,
            "reflection": reflection,
            "transcript_processed": bool(payload.get("transcript_bundle")),
        }
        summary.update(promotion_summary)
        _raise_if_run_canceled(uid, tenant_id, run_id)
        completed_at = datetime.now(timezone.utc).isoformat()
        _update_curation_run(run_id, tenant_id, status="completed", summary=summary, ended_at=completed_at, error={})
        queue.remove(run_id, tenant_id=tenant_id)
        _publish_curation_status(
            run_id,
            "completed",
            actor=uid,
            output_bank_id=payload["output_bank_id"],
            source_bank_id=payload["source_bank_id"],
            output_mode=payload["output_mode"],
        )
    except CurationError:
        ended_at = datetime.now(timezone.utc).isoformat()
        row = _fetch_curation_run(uid, tenant_id, run_id)
        if row is None or row["status"] != "canceled":
            _update_curation_run(run_id, tenant_id, status="canceled", ended_at=ended_at)
            _publish_curation_status(run_id, "canceled", actor=uid)
        else:
            _update_curation_run(run_id, tenant_id, ended_at=ended_at)
        queue.remove(run_id, tenant_id=tenant_id)
    except Exception as exc:
        failed_at = datetime.now(timezone.utc).isoformat()
        error = {
            "type": exc.__class__.__name__,
            "message": str(exc),
        }
        _update_curation_run(run_id, tenant_id, status="failed", error=error, ended_at=failed_at)
        queue.remove(run_id, tenant_id=tenant_id)
        _publish_curation_status(run_id, "failed", actor=uid, error=error)
    finally:
        with _CURATION_WORKERS_LOCK:
            _CURATION_WORKERS.pop(run_id, None)
            _CURATION_CANCEL_SIGNALS.pop(run_id, None)


def _start_curation_worker(run_id: str, payload: dict[str, Any]) -> None:
    """Start a daemon thread to process a curation run."""
    worker = threading.Thread(
        target=_execute_curation_run,
        args=(run_id, payload),
        daemon=True,
        name=f"foresight-curation-{run_id[:8]}",
    )
    with _CURATION_WORKERS_LOCK:
        existing = _CURATION_WORKERS.get(run_id)
        if existing and existing.is_alive():
            return
        _CURATION_WORKERS[run_id] = worker
    worker.start()


@mcp.tool(output_schema=None)
def manage_curation_runs(options: CurationRunAction, user_id: str | None = None) -> str:
    """
    Manage async Foresight curation runs.

    Actions:
    - create: queue a new run
    - get: fetch a single run
    - list: list recent runs
    - cancel: cancel a pending/running run
    - archive: archive a terminal run
    """
    init_db()
    uid = user_id or USER_ID
    tenant_id = get_current_tenant_id()

    if options.action == "create":
        source_bank_id = options.source_bank_id or BANK_ID
        if options.output_mode == "in_place" and options.tool_access != "operate":
            res = _tool_error("create", "output_mode=in_place requires tool_access=operate")
        elif options.output_mode == "in_place" and options.output_bank_id is not None:
            res = _tool_error("create", "output_mode=in_place does not allow output_bank_id override")
        elif options.transcript_bundle and options.tool_access != "operate":
            res = _tool_error("create", "transcript_bundle requires tool_access=operate")
        else:
            run_id = f"cur_{uuid.uuid4().hex[:12]}"
            output_bank_id = _curation_run_output_bank(
                run_id, source_bank_id, options.output_mode, options.output_bank_id
            )
            created_at = datetime.now(timezone.utc).isoformat()
            conn = get_db_connection()
            conn.execute(
                """INSERT INTO curation_runs
                (id, tenant_id, user_id, source_bank_id, output_bank_id, policy_mode, tool_access,
                 output_mode, status, instructions, transcript_bundle_json, session_id, project_path,
                 summary_json, error_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, '{}', '{}', ?)""",
                (
                    run_id,
                    tenant_id,
                    uid,
                    source_bank_id,
                    output_bank_id,
                    options.policy_mode,
                    options.tool_access,
                    options.output_mode,
                    options.instructions,
                    json.dumps(options.transcript_bundle) if options.transcript_bundle else None,
                    options.session_id,
                    options.project_path,
                    created_at,
                ),
            )
            conn.commit()
            conn.close()

            payload = {
                "tenant_id": tenant_id,
                "user_id": uid,
                "source_bank_id": source_bank_id,
                "output_bank_id": output_bank_id,
                "policy_mode": options.policy_mode,
                "tool_access": options.tool_access,
                "output_mode": options.output_mode,
                "instructions": options.instructions,
                "transcript_bundle": options.transcript_bundle,
                "session_id": options.session_id,
                "project_path": options.project_path,
            }
            queue = OperationQueue(DB_PATH)
            queue.enqueue(
                Operation(
                    id=run_id,
                    type=OperationType.CREATE,
                    entity_type="curation_run",
                    entity_id=run_id,
                    payload=payload,
                ),
                tenant_id=tenant_id,
            )
            _publish_curation_status(run_id, "pending", actor=uid, output_bank_id=output_bank_id)
            _start_curation_worker(run_id, payload)
            run = _row_to_curation_run(_fetch_curation_run(uid, tenant_id, run_id))
            res = _tool_response(ok=True, action="create", run=run)

    elif options.action == "list":
        conn = get_db_connection()
        rows = conn.execute(
            "SELECT * FROM curation_runs WHERE user_id = ? AND tenant_id = ? ORDER BY created_at DESC LIMIT ?",
            (uid, tenant_id, options.limit),
        ).fetchall()
        conn.close()
        res = _tool_response(ok=True, action="list", runs=[_row_to_curation_run(row) for row in rows])

    elif not options.run_id:
        res = _tool_error(options.action, "run_id is required for this action")

    else:
        row = _fetch_curation_run(uid, tenant_id, options.run_id)
        if row is None:
            res = _tool_error(options.action, f"Curation run {options.run_id} not found.", run_id=options.run_id)
        elif options.action == "get":
            res = _tool_response(ok=True, action="get", run=_row_to_curation_run(row))
        elif options.action == "cancel":
            if row["status"] not in {"pending", "running"}:
                res = _tool_error(
                    "cancel",
                    f"Run {options.run_id} is already {row['status']} and cannot be canceled.",
                    run=_row_to_curation_run(row),
                )
            else:
                ended_at = datetime.now(timezone.utc).isoformat()
                _get_curation_cancel_event(options.run_id).set()
                _update_curation_run(options.run_id, tenant_id, status="canceled", ended_at=ended_at)
                OperationQueue(DB_PATH).remove(options.run_id, tenant_id=tenant_id)
                _publish_curation_status(options.run_id, "canceled", actor=uid)
                res = _tool_response(
                    ok=True,
                    action="cancel",
                    run=_row_to_curation_run(_fetch_curation_run(uid, tenant_id, options.run_id)),
                )
        elif options.action == "archive":
            if row["status"] not in {"completed", "failed", "canceled"}:
                res = _tool_error(
                    "archive",
                    f"Run {options.run_id} must be terminal before it can be archived.",
                    run=_row_to_curation_run(row),
                )
            else:
                archived_at = datetime.now(timezone.utc).isoformat()
                _update_curation_run(options.run_id, tenant_id, archived_at=archived_at)
                res = _tool_response(
                    ok=True,
                    action="archive",
                    run=_row_to_curation_run(_fetch_curation_run(uid, tenant_id, options.run_id)),
                )
        else:
            res = _tool_error(options.action, f"Unsupported action: {options.action}")

    return res


# =============================================================================
# WebSocket Subscription Tools
# =============================================================================


def get_subscription_manager() -> SubscriptionManager:
    """Get or create the global subscription manager."""
    if _SERVER_STATE["subscription_manager"] is None:
        _SERVER_STATE["subscription_manager"] = SubscriptionManager()
    return _SERVER_STATE["subscription_manager"]


# =============================================================================
# In-Context Memory Injection
# =============================================================================

_STOP_WORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "can",
        "this",
        "that",
        "these",
        "those",
        "i",
        "you",
        "he",
        "she",
        "it",
        "we",
        "they",
        "me",
        "him",
        "her",
        "us",
        "them",
        "my",
        "your",
        "his",
        "its",
        "our",
        "their",
        "and",
        "but",
        "or",
        "not",
        "no",
        "so",
        "if",
        "then",
        "than",
        "too",
        "very",
        "just",
        "about",
        "also",
        "with",
        "from",
        "into",
        "for",
        "on",
        "at",
        "to",
        "of",
        "in",
        "by",
        "up",
        "out",
        "off",
        "all",
        "some",
        "any",
        "each",
        "every",
        "both",
        "few",
        "more",
        "most",
        "other",
        "such",
        "only",
        "own",
        "same",
        "what",
        "when",
        "where",
        "who",
        "how",
        "why",
        "which",
        "while",
        "during",
        "before",
        "after",
        "above",
        "below",
        "between",
        "under",
        "again",
        "further",
        "once",
    }
)


def _extract_terms(text: str) -> list[str]:
    """Extract key terms from text by splitting, lowering, and filtering stop words and short tokens."""
    words = text.lower().split()
    return [w for w in words if len(w) > 3 and w not in _STOP_WORDS]


class _MemoryLike(Protocol):
    """Minimal row protocol for relevance scoring."""

    def __getitem__(self, key: str, /) -> Any: ...


def _score_memory_relevance(
    memory: _MemoryLike | Mapping[str, Any],
    terms: list[str],
    now: datetime,
) -> float:
    """Compute a relevance score for a memory row given search terms.

    Score = term-overlap-count + importance-boost + recency-decay

    - term-overlap-count: how many of the search terms appear in the memory content
    - importance-boost: the stored importance value (default 1.0)
    - recency-decay: exponential decay based on age in days (half-life ~7 days)
    """
    content_lower = (memory["content"] or "").lower()

    overlap = sum(1 for t in terms if re.search(rf"\b{re.escape(t)}\b", content_lower))

    overlap_score = overlap / max(len(terms), 1)

    importance = memory["importance"] if memory["importance"] is not None else 0.5

    created_str = memory["created_at"]
    try:
        created = datetime.fromisoformat(created_str)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_hours = max((now - created).total_seconds() / 3600, 0)
    except (ValueError, TypeError):
        age_hours = 0
    half_life_hours = 168.0
    decay = 0.5 ** (age_hours / half_life_hours)

    return overlap_score + importance * 0.5 + decay * 0.5


@mcp.tool(output_schema=None)
def inject_context(
    conversation_text: str,
    user_id: str | None = None,
    max_memories: int = 5,
    min_relevance: float = 0.3,
    include_details: bool = False,
) -> str:
    """Surface relevant memories based on conversation context.

    Analyzes conversation text to find and return the most relevant memories
    for grounding the AI's responses in prior context. Uses HybridRetriever
    (keyword + TF-IDF + graph + temporal signals via RRF) for ranking.

    Args:
        conversation_text: The current conversation text to analyze for context
        user_id: Optional user ID override
        max_memories: Maximum number of memories to return (default: 5)
        min_relevance: Minimum relevance score threshold (default: 0.3)
        include_details: If True, return JSON with formatted text plus structured
            memories and context blocks grouped by InjectionPoint (default: False)

    Returns:
        Formatted string with relevant memories and context block signals.
        If include_details=True, returns a JSON string with keys:
        - formatted: the formatted text
        - memories: list of dicts with memory_id, content, score, signals
        - context_blocks: dict grouped by InjectionPoint
    """
    uid = user_id or USER_ID
    tenant_id = get_current_tenant_id()
    terms = _extract_terms(conversation_text)
    retriever = get_hybrid_retriever()
    query_text = conversation_text if conversation_text else " ".join(terms)

    hybrid_result = retriever.search(
        query=query_text,
        user_id=uid,
        tenant_id=tenant_id,
        limit=max(50, max_memories * 3),
        min_importance=0.1,
    )

    memories: list[HybridResult] = [r for r in hybrid_result.results if r.combined_score >= min_relevance][
        :max_memories
    ]

    formatted = _format_injection_output(memories, uid, tenant_id, terms)

    if not include_details:
        return formatted

    blocks_by_point = _format_context_blocks_by_injection_point(uid, tenant_id, terms)
    payload = {
        "formatted": formatted,
        "memories": [m.to_dict() for m in memories],
        "context_blocks": blocks_by_point,
    }
    return json.dumps(payload, indent=2)


def _format_injection_output(
    memories: list[HybridResult],
    uid: str,
    _tenant_id: str,
    terms: list[str],
) -> str:
    """Format memories and context block signals into a human-readable string."""
    lines: list[str] = []
    if memories:
        lines.append(f"[Relevant Context - {len(memories)} memories surfaced]")
        for mem in memories:
            snippet = (mem.content or "")[:120]
            if len(mem.content or "") > 120:
                snippet += "..."
            lines.append(f"- [{mem.memory_id}] (score: {mem.combined_score:.2f}) {snippet}")

    sub_lines = _context_block_notes_for_terms(uid, terms)
    if sub_lines:
        lines.append("")
        lines.append("[Subconscious/Block Signals]")
        lines.extend(sub_lines)

    if not lines:
        return "[Relevant Context - 0 memories surfaced]\nNo relevant memories found for this conversation."

    return "\n".join(lines)


def _format_context_blocks_by_injection_point(
    uid: str,
    tenant_id: str,
    terms: list[str],
) -> dict[str, list[dict]]:
    """Group matching context block entries by their schema's InjectionPoint.

    Returns a dict mapping InjectionPoint value to matching block entries:
    {"pre_prompt": [...], "post_prompt": [...], "whisper_only": [...]}

    Each entry contains: label, content, matched_terms.
    """
    agent = get_context_block_agent(uid, tenant_id)
    registry = initialize_default_blocks()
    relevant_labels = [USER_PREFERENCES, SESSION_PATTERNS, PENDING_ITEMS]

    grouped: dict[str, list[dict]] = {
        InjectionPoint.PRE_PROMPT.value: [],
        InjectionPoint.POST_PROMPT.value: [],
        InjectionPoint.WHISPER_ONLY.value: [],
    }

    for label in relevant_labels:
        schema = registry.get_schema(label)
        if schema is None:
            continue
        block = agent.state.get_block(label)
        if not block or block.is_empty():
            continue
        content = block.content
        content_lower = content.lower()
        if not terms or not any(re.search(rf"\b{re.escape(t)}\b", content_lower) for t in terms):
            continue
        for line in content.splitlines():
            line_lower = line.lower().strip()
            if not line_lower:
                continue
            matched = [t for t in terms if re.search(rf"\b{re.escape(t)}\b", line_lower)]
            if matched:
                grouped[schema.injection_point.value].append(
                    {"label": label, "content": line.strip(), "matched_terms": matched}
                )

    return grouped


def _context_block_notes_for_terms(
    uid: str,
    terms: list[str],
) -> list[str]:
    """Check context blocks for content relevant to the search terms.

    Returns a list of formatted lines with matching block content,
    grouped by InjectionPoint (PRE_PROMPT first, then POST_PROMPT, then WHISPER_ONLY).
    """
    tenant_id = get_current_tenant_id()
    grouped = _format_context_blocks_by_injection_point(uid, tenant_id, terms)

    lines: list[str] = []
    for point_value, entries in grouped.items():
        matching_entries = [
            e for e in entries if any(re.search(rf"\b{re.escape(t)}\b", e["content"].lower()) for t in terms)
        ]
        if not matching_entries:
            continue
        for entry in matching_entries[:3]:
            lines.append(f"  [{entry['label']} / {point_value}] {entry['content']}")

    return lines


def _subconscious_context_for_terms(uid: str, terms: list[str]) -> list[str]:
    """Compatibility alias for the older helper name."""
    return _context_block_notes_for_terms(uid, terms)


@mcp.tool(output_schema=None)
def get_relevant_memories(
    query: str,
    user_id: str | None = None,
    limit: int = 5,
    min_relevance: float = 0.1,
) -> str:
    """Return structured list of relevant memories for a query.

    Clean memories-only API (no context blocks). Uses HybridRetriever
    (keyword + TF-IDF + graph + temporal signals via RRF) for ranking.

    Args:
        query: Search query string
        user_id: Optional user ID override
        limit: Maximum number of memories to return (default: 5)
        min_relevance: Minimum combined_score threshold (default: 0.1)

    Returns:
        JSON string with:
        - memories: list of dicts with memory_id, content, category, importance,
          created_at, keyword_score, tfidf_cosine_score, semantic_score,
          graph_score, temporal_score, combined_score, source_signals
        - total_candidates: number of candidates considered
        - signal_counts: dict of how many results each signal contributed
    """
    uid = user_id or USER_ID
    tenant_id = get_current_tenant_id()
    retriever = get_hybrid_retriever()

    result = retriever.search(
        query=query,
        user_id=uid,
        tenant_id=tenant_id,
        limit=max(limit * 2, 20),
        min_importance=0.0,
    )

    memories = [m.to_dict() for m in result.results if m.combined_score >= min_relevance][:limit]
    payload = {
        "memories": memories,
        "total_candidates": result.total_candidates,
        "signal_counts": result.signal_counts,
    }
    return json.dumps(payload, indent=2)


def _resume_pending_curation_runs() -> None:
    """Resume pending or interrupted curation runs on server startup."""
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM curation_runs WHERE status IN ('pending', 'running') ORDER BY created_at ASC"
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        payload = _curation_payload_from_row(row)
        if row["status"] == "running":
            _update_curation_run(row["id"], row["tenant_id"], status="pending")
        OperationQueue(DB_PATH).enqueue(
            Operation(
                id=row["id"],
                type=OperationType.CREATE,
                entity_type="curation_run",
                entity_id=row["id"],
                payload=payload,
            ),
            tenant_id=row["tenant_id"],
        )
        _start_curation_worker(row["id"], payload)


def main():
    init_db()
    initialize_stream_producer()
    _resume_pending_curation_runs()
    mcp.run(show_banner=False)


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


def get_tenant_context() -> dict:
    """Get current tenant context."""
    if _SERVER_STATE["tenant_context"] is None:
        _SERVER_STATE["tenant_context"] = {"id": get_current_tenant_id()}
    return _SERVER_STATE["tenant_context"]


def set_tenant_context(tenant_id: str) -> None:
    """Set tenant context for current request.

    Deprecated: Use set_current_tenant_id() from tenant_module instead.
    The TenantMiddleware handles per-request tenant isolation automatically.
    """
    warnings.warn(
        "set_tenant_context() is deprecated; use set_current_tenant_id() instead",
        DeprecationWarning,
        stacklevel=2,
    )
    set_current_tenant_id(tenant_id)


@mcp.tool(output_schema=None)
def switch_tenant(tenant_id: str) -> str:
    """
    Switch current tenant context.

    Args:
        tenant_id: Tenant to switch to

    Returns:
        Confirmation message
    """
    conn = get_db_connection()
    try:
        if tenant_id == DEFAULT_TENANT_ID:
            _seed_default_tenant(conn)
            conn.commit()
            set_current_tenant_id(tenant_id)
            return f"Switched to tenant '{tenant_id}'"

        row = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
        if not row:
            return f"Tenant '{tenant_id}' not found"
    finally:
        conn.close()

    set_current_tenant_id(tenant_id)
    return f"Switched to tenant '{tenant_id}'"


# =============================================================================
# Temporal Memory Tools
# =============================================================================

# =============================================================================
# Temporal and Status Tools
# =============================================================================


@mcp.tool(output_schema=None)
def query_memories_temporal(options: TemporalWindow, user_id: str | None = None) -> str:
    """
    Query memories based on temporal signals (window, trend).

    Args:
        options: Temporal query parameters (window, trend, category, limit)
        user_id: Optional user ID override
    """
    uid = user_id or USER_ID
    builder = get_temporal_query_builder()

    if options.trend:
        results = builder.get_memories_by_trend(
            user_id=uid, trend=options.trend, limit=options.limit, category=options.category
        )
    else:
        results = builder.get_memories_from_window(
            user_id=uid, window=options.window, limit=options.limit, min_importance=0.1, category=options.category
        )

    return json.dumps(
        [
            {
                "memory_id": r.memory_id,
                "content": r.content,
                "importance": r.importance,
                "strength_trend": r.strength_trend,
                "activation_count": r.activation_count,
                "created_at": r.created_at,
                "category": r.category,
            }
            for r in results
        ],
        indent=2,
    )


@mcp.tool(output_schema=None)
def get_system_status(options: SystemStatusOptions | None = None, user_id: str | None = None) -> str:
    """
    Get system health, memory statistics, and temporal trends.

    Args:
        options: Optional status and trend parameters
        user_id: Optional user ID override
    """
    uid = user_id or USER_ID
    opts = options or SystemStatusOptions()
    conn = get_db_connection()

    # Basic stats
    count = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE user_id = ? AND tenant_id = ?", (uid, get_current_tenant_id())
    ).fetchone()[0]

    scope_counts = conn.execute(
        "SELECT scope, COUNT(*) FROM memories WHERE user_id = ? AND tenant_id = ? GROUP BY scope",
        (uid, get_current_tenant_id()),
    ).fetchall()
    conn.close()

    result = {
        "status": "healthy",
        "memory_count": count,
        "by_scope": {r[0]: r[1] for r in scope_counts},
        "tenant_id": get_current_tenant_id(),
    }

    # Add temporal stats/trends if requested
    if opts.include_trends:
        builder = get_temporal_query_builder()
        service = get_temporal_service()
        result["temporal_stats"] = service.get_memory_stats(user_id=uid)
        result["trend_analysis"] = builder.analyze_trends(user_id=uid, timeframe=opts.timeframe)

    return json.dumps(result, indent=2)


@mcp.tool(output_schema=None)
def memory_status(
    user_id: str | None = None,
    include_trends: bool = False,
    timeframe: str = "30 days",
) -> str:
    """Legacy alias for get_system_status() used by CLI health checks."""
    return get_system_status(
        options=SystemStatusOptions(include_trends=include_trends, timeframe=timeframe),
        user_id=user_id,
    )


@mcp.tool(output_schema=None)
def store_memory(
    content: str,
    user_id: str | None = None,
    category: str = "fact",
    scope: str = "session",
    retention: str = "short_term",
    importance: float = 0.5,
    emotional_context: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
) -> str:
    """Legacy alias for manage_memories(action="store") used by callers and tests."""
    options = MemoryAction(
        action="store",
        content=content,
        options=MemoryOptions(
            category=category,
            scope=scope,
            retention=retention,
            importance=importance,
            emotional_context=emotional_context,
            metrics=metrics,
        ),
    )
    return manage_memories(options, user_id=user_id)


@mcp.tool(output_schema=None)
def list_memories(limit: int = 10, offset: int = 0, user_id: str | None = None) -> str:
    """Legacy alias for search_memories(query_type="list")."""
    options = SearchOptions(query_type="list", limit=limit, offset=offset)
    return search_memories(options, user_id=user_id)


@mcp.tool(output_schema=None)
def query_memories(
    query: str,
    user_id: str | None = None,
    limit: int = 10,
    use_hybrid: bool = True,
    min_importance: float = 0.1,
    offset: int = 0,
) -> str:
    """Legacy alias for search_memories(query_type="keyword")."""
    options = SearchOptions(
        query_type="keyword",
        query=query,
        limit=limit,
        use_hybrid=use_hybrid,
        min_importance=min_importance,
        offset=offset,
    )
    return search_memories(options, user_id=user_id)


@mcp.tool(output_schema=None)
def get_memory(memory_id: str, user_id: str | None = None, min_importance: float = 0.1) -> str:
    """Legacy alias for search_memories(query_type="id")."""
    options = SearchOptions(query_type="id", memory_id=memory_id, min_importance=min_importance)
    return search_memories(options, user_id=user_id)


@mcp.tool(output_schema=None)
def update_memory(
    memory_id: str,
    user_id: str | None = None,
    content: str | None = None,
    category: str | None = None,
    scope: str | None = None,
    retention: str | None = None,
    tags: list[str] | None = None,
) -> str:
    """Legacy alias for manage_memories(action="update")."""
    updates = MemoryUpdateOptions(content=content, category=category, scope=scope, retention=retention, tags=tags)
    options = MemoryAction(action="update", memory_id=memory_id, updates=updates)
    return manage_memories(options, user_id=user_id)


@mcp.tool(output_schema=None)
def delete_memory(memory_id: str, user_id: str | None = None) -> str:
    """Legacy alias for manage_memories(action="delete")."""
    options = MemoryAction(action="delete", memory_id=memory_id)
    return manage_memories(options, user_id=user_id)


@mcp.tool(output_schema=None)
def archive_memory(memory_id: str, user_id: str | None = None) -> str:
    """Legacy alias for manage_memories(action="archive")."""
    options = MemoryAction(action="archive", memory_id=memory_id)
    return manage_memories(options, user_id=user_id)


# =============================================================================
# Profile Synthesis Tool
# =============================================================================


@mcp.tool(output_schema=None)
def synthesize_profile(
    user_id: str | None = None,
    max_static_memories: int = 20,
    max_dynamic_memories: int = 10,
    include_synthesis: bool = True,
    format_prompt: bool = False,
) -> str:
    """
    Build a user profile with static (stable facts) and dynamic (recent context) layers.

    Profile = compact summary of who the user is (static) and what they are
    currently working on (dynamic). Directly injectable into system prompts.

    Args:
        user_id: Optional user ID override.
        max_static_memories: Max trait/fact memories to consider.
        max_dynamic_memories: Max session/arc memories to consider.
        include_synthesis: Run enhanced synthesis for contradiction detection.
        format_prompt: Return as a formatted prompt block instead of JSON.

    Returns:
        JSON:  ``{"static": [...], "dynamic": [...]}``
        Prompt block when ``format_prompt=True``.
    """
    uid = user_id or USER_ID
    cfg = ProfileConfig(
        max_static_memories=max_static_memories,
        max_dynamic_memories=max_dynamic_memories,
        include_synthesis=include_synthesis,
    )
    profile = _run_async(_synthesize_profile(uid, get_current_tenant_id(), cfg))

    if format_prompt:
        return profile_to_prompt(profile)

    return json.dumps(profile.to_dict(), indent=2, ensure_ascii=False)


# =============================================================================
# Entity and Graph Tools
# =============================================================================


@mcp.tool(output_schema=None)
def manage_entities(action: EntityAction, user_id: str | None = None) -> str:
    """
    Manage entities and relationships (extract, link).

    Args:
        action: Action details (extract, link)
        user_id: Optional user ID override
    """
    uid = user_id or USER_ID
    store = get_graph_store()

    if action.action == "extract":
        if not action.content:
            return "Content is required for entity extraction"
        extractor = get_entity_extractor()
        result = _run_async(extractor.extract(action.content))
        return json.dumps(
            {
                "user_id": uid,
                "entities": [e.to_dict() for e in result.entities],
                "relationships": [r.to_dict() for r in result.relationships],
            },
            indent=2,
        )

    if action.action == "link":
        if not action.memory_id or not action.entity_ids:
            return "memory_id and entity_ids are required for linking"
        store.link_memory_to_entities(action.memory_id, action.entity_ids, uid)
        return f"Linked memory {action.memory_id} to {len(action.entity_ids)} entities"

    return "Invalid action"


def _handle_entity_query_by_type(uid: str, store, query: EntityQuery) -> str:
    """Helper for entity query by type."""
    if not query.entity_type:
        return "entity_type is required"
    entities = store.get_entities_by_type(uid, query.entity_type, query.limit)
    return json.dumps([e.to_dict() for e in entities], indent=2)


def _handle_entity_query_by_name(uid: str, store, query: EntityQuery) -> str:
    """Helper for entity query by name."""
    if not query.name:
        return "name is required"
    entities = store.find_entities_by_name(uid, query.name, query.limit)
    return json.dumps([e.to_dict() for e in entities], indent=2)


def _handle_entity_query_relationships(uid: str, store, query: EntityQuery) -> str:
    """Helper for entity relationships query."""
    if not query.entity_id:
        return "entity_id is required"
    relationships = store.get_relationships(query.entity_id, uid, query.direction)
    return json.dumps([r.to_dict() for r in relationships], indent=2)


@mcp.tool(output_schema=None)
def query_entities(query: EntityQuery, user_id: str | None = None) -> str:
    """
    Query entities and graph relationships.

    Args:
        query: Query parameters (by_type, by_name, relationships, traverse)
        user_id: Optional user ID override
    """
    uid = user_id or USER_ID
    store = get_graph_store()

    if query.query_type == "by_type":
        return _handle_entity_query_by_type(uid, store, query)

    if query.query_type == "by_name":
        return _handle_entity_query_by_name(uid, store, query)

    if query.query_type == "relationships":
        return _handle_entity_query_relationships(uid, store, query)

    if query.query_type == "traverse":
        if not query.entity_id:
            return "entity_id is required for traversal"
        result = store.traverse_graph(query.entity_id, uid, query.max_depth, query.limit)
        return json.dumps(
            {
                "nodes": [e.to_dict() for e in result.nodes],
                "edges": [r.to_dict() for r in result.edges],
            },
            indent=2,
        )

    return "Invalid query_type"


# =============================================================================
# Document Layer Tools (MEM-7)
# =============================================================================


@mcp.tool(output_schema=None)
def create_document(
    title: str,
    content: str,
    user_id: str | None = None,
    source: str = "note",
    metadata: dict[str, Any] | None = None,
    char_budget: int = _DOC_CHUNK_BUDGET,
    memory_id_for_chunk: str | None = None,
) -> str:
    """
    Persist a raw source document and its extracted chunks.

    Separates source content from derived memories so that extraction
    can be re-run without losing the original. Chunks are produced
    synchronously with paragraph-based splitting (see chunk_text).

    Args:
        title: Human-readable document title.
        content: Raw source text.
        user_id: Optional user ID override.
        source: One of transcript/article/journal/note/email/other.
        metadata: Optional JSON-serializable metadata.
        char_budget: Soft max chars per chunk (100-8000).
        memory_id_for_chunk: If set, applied to every chunk produced.
    """
    uid = user_id or USER_ID
    store = get_document_store()
    try:
        doc, chunks = store.create_document(
            title=title,
            content=content,
            user_id=uid,
            source=source,
            metadata=metadata,
            char_budget=char_budget,
            memory_id_for_chunk=memory_id_for_chunk,
        )
    except DocumentLayerError as exc:
        return f"Error: {exc}"
    return json.dumps(
        {
            "document": doc.to_dict(),
            "chunks": [c.to_dict() for c in chunks],
        },
        indent=2,
    )


@mcp.tool(output_schema=None)
def get_document(
    document_id: str,
    user_id: str | None = None,
) -> str:
    """Fetch a stored document by ID."""
    uid = user_id or USER_ID
    store = get_document_store()
    try:
        doc = store.get_document(document_id=document_id, user_id=uid)
    except DocumentLayerError as exc:
        return f"Error: {exc}"
    if doc is None:
        return f"Document {document_id} not found."
    return json.dumps(doc.to_dict(), indent=2)


@mcp.tool(output_schema=None)
def list_document_chunks(
    document_id: str,
    user_id: str | None = None,
) -> str:
    """List all chunks produced from a document, in order."""
    uid = user_id or USER_ID
    store = get_document_store()
    try:
        chunks = store.list_chunks(document_id=document_id, user_id=uid)
    except DocumentLayerError as exc:
        return f"Error: {exc}"
    return json.dumps([c.to_dict() for c in chunks], indent=2)


@mcp.tool(output_schema=None)
def get_memory_source(
    memory_id: str,
    user_id: str | None = None,
) -> str:
    """Reverse-lookup: given a memory_id, return its source document + chunk."""
    uid = user_id or USER_ID
    store = get_document_store()
    try:
        result = store.get_memory_source(memory_id=memory_id, user_id=uid)
    except DocumentLayerError as exc:
        return f"Error: {exc}"
    if result is None:
        return f"No source document found for memory {memory_id}."
    doc, chunk = result
    return json.dumps(
        {"document": doc.to_dict(), "chunk": chunk.to_dict()},
        indent=2,
    )


@mcp.tool(output_schema=None)
def delete_document(
    document_id: str,
    user_id: str | None = None,
) -> str:
    """Delete a document and all of its chunks (CASCADE)."""
    uid = user_id or USER_ID
    store = get_document_store()
    try:
        deleted = store.delete_document(document_id=document_id, user_id=uid)
    except DocumentLayerError as exc:
        return f"Error: {exc}"
    return json.dumps(
        {"document_id": document_id, "deleted": deleted},
        indent=2,
    )


# =============================================================================
# Enhanced Synthesis Tools
# =============================================================================


@mcp.tool(output_schema=None)
def link_memories(
    source_memory_id: str,
    target_memory_id: str,
    relationship_type: str,
    user_id: str | None = None,
    confidence: float = 1.0,
    metadata: dict[str, Any] | None = None,
) -> str:
    """
    Create or update a typed relationship between two memories.

    Relationship types: 'updates', 'extends', 'derives', 'contradicts',
    'supports', 'related'.

    Args:
        source_memory_id: ID of the source memory (the "from" side).
        target_memory_id: ID of the target memory (the "to" side).
        relationship_type: One of the supported relationship types.
        user_id: Optional user ID override.
        confidence: Confidence score in [0.0, 1.0].
        metadata: Optional JSON-serializable metadata.
    """
    uid = user_id or USER_ID
    store = get_memory_relationship_store()
    try:
        rel = store.link_memories(
            source_memory_id=source_memory_id,
            target_memory_id=target_memory_id,
            relationship_type=relationship_type,
            user_id=uid,
            confidence=confidence,
            metadata=metadata,
        )
    except MemoryRelationshipError as exc:
        return f"Error: {exc}"
    return json.dumps(rel.to_dict(), indent=2)


@mcp.tool(output_schema=None)
def get_memory_relationships(
    memory_id: str,
    user_id: str | None = None,
    direction: str = "both",
    relationship_type: str | None = None,
) -> str:
    """
    Return relationships touching a memory.

    Args:
        memory_id: The memory to query.
        user_id: Optional user ID override.
        direction: 'out' (source=memory), 'in' (target=memory), or 'both'.
        relationship_type: Optional filter, e.g. 'updates' or 'extends'.
    """
    uid = user_id or USER_ID
    store = get_memory_relationship_store()
    try:
        rels = store.get_relationships_for_memory(
            memory_id=memory_id,
            user_id=uid,
            direction=direction,
            relationship_type=relationship_type,
        )
    except MemoryRelationshipError as exc:
        return f"Error: {exc}"
    return json.dumps([r.to_dict() for r in rels], indent=2)


@mcp.tool(output_schema=None)
def traverse_memory_graph(
    root_memory_id: str,
    user_id: str | None = None,
    max_depth: int = 2,
    limit: int = 100,
) -> str:
    """
    BFS-traverse the memory relationship graph from a root memory.

    Walks edges in both directions up to max_depth and returns the set of
    reachable memory IDs plus the edges traversed.

    Args:
        root_memory_id: Starting memory ID.
        user_id: Optional user ID override.
        max_depth: Maximum traversal depth (0-5).
        limit: Maximum number of nodes to return (1-1000).
    """
    uid = user_id or USER_ID
    store = get_memory_relationship_store()
    try:
        result = store.traverse_memory_graph(
            root_memory_id=root_memory_id,
            user_id=uid,
            max_depth=max_depth,
            limit=limit,
        )
    except MemoryRelationshipError as exc:
        return f"Error: {exc}"
    return json.dumps(
        {
            "root_memory_id": result.root_memory_id,
            "depth": result.depth,
            "nodes": result.nodes,
            "edges": result.edges,
        },
        indent=2,
    )


# =============================================================================
# Semantic Vector Search Tools (MEM-5)
# =============================================================================


@mcp.tool(output_schema=None)
def index_memory_embedding(
    memory_id: str,
    text: str,
    user_id: str | None = None,
    provider: str | None = None,
) -> str:
    """
    Compute and store an embedding vector for a memory's text.

    Args:
        memory_id: The memory ID to index.
        text: The text content to embed.
        user_id: Optional user ID override.
        provider: Embedder provider name (default 'local-hash').
    """
    uid = user_id or USER_ID
    prov = provider or _SEMANTIC_DEFAULT_PROVIDER
    try:
        store = get_semantic_search(provider=prov)
        dim = store.index_memory(memory_id=memory_id, text=text, user_id=uid)
    except _SemanticSearchError as exc:
        return f"Error: {exc}"
    return json.dumps(
        {
            "memory_id": memory_id,
            "user_id": uid,
            "provider": prov,
            "dimension": dim,
            "indexed": True,
        },
        indent=2,
    )


@mcp.tool(output_schema=None)
def delete_memory_embedding(
    memory_id: str,
    user_id: str | None = None,
    provider: str | None = None,
) -> str:
    """Remove a memory's stored embedding vector."""
    uid = user_id or USER_ID
    prov = provider or _SEMANTIC_DEFAULT_PROVIDER
    try:
        store = get_semantic_search(provider=prov)
        deleted = store.delete_memory_embedding(memory_id=memory_id, user_id=uid)
    except _SemanticSearchError as exc:
        return f"Error: {exc}"
    return json.dumps(
        {
            "memory_id": memory_id,
            "user_id": uid,
            "provider": prov,
            "deleted": deleted,
        },
        indent=2,
    )


@mcp.tool(output_schema=None)
def semantic_search_memories(
    query: str,
    user_id: str | None = None,
    limit: int = 10,
    min_score: float = 0.0,
    provider: str | None = None,
) -> str:
    """
    Semantic vector search over stored memory embeddings.

    Args:
        query: Free-text query to embed and match against stored vectors.
        user_id: Optional user ID override.
        limit: Maximum matches to return (1-1000).
        min_score: Minimum cosine similarity threshold in [-1.0, 1.0].
        provider: Embedder provider name (default 'local-hash').
    """
    uid = user_id or USER_ID
    prov = provider or _SEMANTIC_DEFAULT_PROVIDER
    try:
        store = get_semantic_search(provider=prov)
        result = store.search(query=query, user_id=uid, limit=limit, min_score=min_score)
    except _SemanticSearchError as exc:
        return f"Error: {exc}"
    return json.dumps(result.to_dict(), indent=2)


# =============================================================================
# Enhanced Synthesis Tools
# =============================================================================


def _handle_analyze_synthesize(uid: str, tenant_id: str, options: AnalysisAction) -> str:
    """Helper for analyze synthesize action."""
    synthesizer = get_enhanced_synthesizer() if options.enhanced else get_memory_system()["synthesizer"]
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT * FROM memories WHERE user_id = ? AND tenant_id = ? AND is_ghost = 0 ORDER BY created_at DESC LIMIT ?",
        (uid, tenant_id, options.limit),
    ).fetchall()
    conn.close()

    if len(rows) < 5:
        return "Need at least 5 memories for synthesis."

    memories = []
    for r in rows:
        emo = json.loads(r["emotional_context"]) if r["emotional_context"] else {}
        memories.append(
            MemoryObject(
                id=r["id"],
                timestamp=r["created_at"],
                scope=r["scope"],
                retention=r["retention"],
                content=r["content"],
                tags=json.loads(r["tags"]),
                emotional_context=EmotionalMetadata(intensity=emo.get("intensity", 0.5)) if emo else None,
            )
        )

    if options.enhanced:
        result = _run_async(synthesizer.synthesize(memories, user_id=uid))
        return json.dumps(result.to_dict(), indent=2) if result else "No results."

    result = _run_async(synthesizer.synthesize(memories))
    if not result:
        return "No results."

    return json.dumps(
        {
            "merged_ids": result.merged_ids,
            "new_memory_id": result.new_memory_id,
            "compression": result.compression_ratio,
            "stance_shifts": len(result.stance_shifts),
        },
        indent=2,
    )


@mcp.tool(output_schema=None)
def analyze_memories(options: AnalysisAction, user_id: str | None = None) -> str:
    """
    Perform analysis on memories: synthesis or reflection.

    Args:
        options: Action and parameters
        user_id: Optional user ID override
    """
    uid = user_id or USER_ID
    tenant_id = get_current_tenant_id()

    if options.action == "synthesize":
        return _handle_analyze_synthesize(uid, tenant_id, options)

    if options.action == "reflect":
        engine = get_reflection_engine()
        report = engine.reflect(uid, tenant_id=tenant_id, period=options.period)
        return json.dumps(report.to_dict(), indent=2) if report else "No results."

    return f"Unknown action: {options.action}"


# =============================================================================
# Hybrid Retrieval Tools
# =============================================================================


# =============================================================================
# Reflection Engine Tools
# =============================================================================


# =============================================================================
# Decay Model Tools (MEM-8: Memory Strength Decay Model)
# =============================================================================


@mcp.tool(output_schema=None)
def get_memory_strength(
    memory_id: str,
    user_id: str | None = None,
    tenant_id: str | None = None,
) -> str:
    """Read the dynamic strength and trend for a memory.

    Returns the importance (creator-set), the current_strength (decayed),
    the strength_trend, activation_count, last_decay_at, and timestamps.
    The dynamic strength decays over time per the user's
    decay_config; the static importance is preserved.

    Args:
        memory_id: Memory ID to look up.
        user_id: Optional user ID override; defaults to the active user.
        tenant_id: Optional tenant ID override; defaults to the active tenant.
    """
    uid = user_id or USER_ID
    tid = tenant_id or get_current_tenant_id()
    result = get_decay_model().get_memory_strength(memory_id=memory_id, user_id=uid, tenant_id=tid)
    if result is None:
        return f"Memory {memory_id} not found for user {uid} in tenant {tid}."
    return json.dumps(result, indent=2)


@mcp.tool(output_schema=None)
def apply_memory_decay(
    user_id: str | None = None,
    tenant_id: str | None = None,
    batch_size: int = 500,
) -> str:
    """Run a decay batch for a user's memories.

    Applies the Ebbinghaus-based decay to current_strength for every
    memory in (tenant, user), updates the trend, and records a
    memory_decay_events audit-log row per affected memory.

    Args:
        user_id: Optional user ID override; defaults to the active user.
        tenant_id: Optional tenant ID override; defaults to the active tenant.
        batch_size: Pagination size for the underlying query (default 500).
    """
    uid = user_id or USER_ID
    tid = tenant_id or get_current_tenant_id()
    stats = get_decay_model().apply_decay_batch(user_id=uid, tenant_id=tid, batch_size=batch_size)
    return json.dumps(
        {
            "ok": True,
            "action": "apply_memory_decay",
            "user_id": uid,
            "tenant_id": tid,
            **stats.to_dict(),
        },
        indent=2,
    )


@mcp.tool(output_schema=None)
def reinforce_memory(
    memory_id: str,
    user_id: str | None = None,
    tenant_id: str | None = None,
    activation_boost: float | None = None,
) -> str:
    """Boost a memory's strength on access.

    Increments activation_count, multiplies current_strength by
    activation_boost (capped at 1.0), updates accessed_at /
    last_retrieved_at / last_decay_at, sets strength_trend, and writes
    a 'reinforce' event to memory_decay_events.

    Args:
        memory_id: Memory ID to reinforce.
        user_id: Optional user ID override.
        tenant_id: Optional tenant ID override.
        activation_boost: Optional override; defaults to the user's
            decay_config.activation_boost.
    """
    uid = user_id or USER_ID
    tid = tenant_id or get_current_tenant_id()
    result = get_decay_model().reinforce_memory(
        memory_id=memory_id,
        user_id=uid,
        tenant_id=tid,
        activation_boost=activation_boost,
    )
    if result is None:
        return f"Memory {memory_id} not found for user {uid} in tenant {tid}."
    return json.dumps({"ok": True, "action": "reinforce_memory", **result}, indent=2)


@mcp.tool(output_schema=None)
def get_decay_config(
    user_id: str | None = None,
    tenant_id: str | None = None,
    category: str = "general",
) -> str:
    """Return the decay config for a (tenant, user, category) triple.

    Falls back to system defaults when no row exists in decay_config:
    half_life_hours=168, min_importance=0.1, activation_boost=1.2,
    strengthening_threshold=5, stale_threshold=0.2.

    Args:
        user_id: Optional user ID override.
        tenant_id: Optional tenant ID override.
        category: Memory category (default 'general').
    """
    uid = user_id or USER_ID
    tid = tenant_id or get_current_tenant_id()
    cfg = get_decay_model().get_decay_config(user_id=uid, tenant_id=tid, category=category)
    return json.dumps({"ok": True, "action": "get_decay_config", **cfg.to_dict()}, indent=2)


@mcp.tool(output_schema=None)
def set_decay_config(
    user_id: str | None = None,
    tenant_id: str | None = None,
    category: str = "general",
    half_life_hours: float | None = None,
    min_importance: float | None = None,
    activation_boost: float | None = None,
    strengthening_threshold: int | None = None,
    stale_threshold: float | None = None,
) -> str:
    """Upsert a decay config row for (tenant, user, category).

    None values for individual fields keep whatever the existing row
    holds (or the system default if no row exists). Validation:
    half_life_hours > 0, all thresholds and importance bounds in [0, 1],
    activation_boost in [0, 10].

    Args:
        user_id: Optional user ID override.
        tenant_id: Optional tenant ID override.
        category: Memory category (default 'general').
        half_life_hours: New Ebbinghaus half-life in hours.
        min_importance: Floor for current_strength.
        activation_boost: Multiplier applied on each access.
        strengthening_threshold: Activation count to mark 'strengthening'.
        stale_threshold: Below this strength, trend becomes 'stale'.
    """
    uid = user_id or USER_ID
    tid = tenant_id or get_current_tenant_id()
    cfg = get_decay_model().set_decay_config(
        user_id=uid,
        tenant_id=tid,
        category=category,
        half_life_hours=half_life_hours,
        min_importance=min_importance,
        activation_boost=activation_boost,
        strengthening_threshold=strengthening_threshold,
        stale_threshold=stale_threshold,
    )
    return json.dumps({"ok": True, "action": "set_decay_config", **cfg.to_dict()}, indent=2)


@mcp.tool(output_schema=None)
def get_decay_events(
    user_id: str | None = None,
    tenant_id: str | None = None,
    memory_id: str | None = None,
    limit: int = 50,
) -> str:
    """Read recent memory_decay_events audit-log rows for a user.

    Each row records a single 'decay' or 'reinforce' event with the
    old/new strength, the decay_factor or boost, a human-readable
    reason, and an ISO timestamp. Useful for compliance review and
    debugging unexpected strength changes.

    Args:
        user_id: Optional user ID override.
        tenant_id: Optional tenant ID override.
        memory_id: Optional filter to a single memory.
        limit: Maximum number of events to return (default 50).
    """
    uid = user_id or USER_ID
    tid = tenant_id or get_current_tenant_id()
    events = get_decay_model().get_decay_events(user_id=uid, tenant_id=tid, memory_id=memory_id, limit=limit)
    return json.dumps(
        {
            "ok": True,
            "action": "get_decay_events",
            "count": len(events),
            "events": [e.to_dict() for e in events],
        },
        indent=2,
    )
