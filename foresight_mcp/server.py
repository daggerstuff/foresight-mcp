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
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, cast

from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware as _Middleware
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, Field

from .auth import AuthMiddleware
from .config import (
    BANK_ID,
    DB_PATH,
    DEFAULT_BURST_LIMIT,
    DEFAULT_RATE_LIMIT,
    USER_ID,
)
from .connection_pool import get_pool
from .crisis_detection import get_crisis_service
from .enhanced_synthesizer import get_enhanced_synthesizer
from .entity_extractor import get_entity_extractor
from .event_bus import (
    get_event_bus,
    memory_deleted,
    memory_retrieved,
    memory_stored,
    memory_updated,
)
from .graph_store import get_graph_store
from .hybrid_retriever import get_hybrid_retriever
from .memory_components import (
    MemoryCrisisTagger,
    MemoryLinker,
    MemorySynthesizer,
    SocraticGate,
)
from .memory_types import (
    EmotionalMetadata,
    EmpathyMetrics,
    MemoryObject,
    MemoryScope,
    RetentionPolicy,
)
from .rate_limiter import RateLimitExceeded, get_rate_limiter
from .reflection_engine import get_reflection_engine
from .stream_producer import (
    KafkaProducer,
    KinesisProducer,
    StreamPublisher,
    create_stream_producer,
)
from .subconscious import (
    PENDING_ITEMS,
    SESSION_PATTERNS,
    USER_PREFERENCES,
    get_subconscious_agent,
)
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


class SubconsciousAction(BaseModel):
    action: Literal["list", "get", "update", "reset", "clear"] = Field(description="Action to perform")
    label: str | None = Field(default=None, description="Block label (e.g. guidance, preferences)")
    content: str | None = Field(default=None, description="New content for update action")


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

    # Migrate decay_config: add tenant_id if table exists without it
    try:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(decay_config)").fetchall()]
        if cols and "tenant_id" not in cols:
            conn.execute("ALTER TABLE decay_config ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_decay_config_tenant ON decay_config(tenant_id)")
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

    conn = get_db_connection()
    conn.execute(
        """
    INSERT INTO memory_versions (
        id, memory_id, content, version, created_at, tags, emotional_context, metrics, rollback_of
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            version_id,
            memory_id,
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

    v1 = conn.execute(
        "SELECT * FROM memory_versions WHERE memory_id = ? AND version = ?", (memory_id, version1)
    ).fetchone()

    v2 = conn.execute(
        "SELECT * FROM memory_versions WHERE memory_id = ? AND version = ?", (memory_id, version2)
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
            return CallToolResult.model_validate(
                {
                    "content": [TextContent(type="text", text=str(e))],
                    "isError": True,
                }
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
                return CallToolResult.model_validate(
                    {
                        "content": [TextContent(type="text", text=f"Validation error: {error}")],
                        "isError": True,
                    }
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

    # Deduplication
    conn = get_db_connection()
    existing = conn.execute(
        "SELECT id, activation_count FROM memories "
        "WHERE user_id = ? AND tenant_id = ? AND content = ? AND is_ghost = 0 "
        "ORDER BY created_at DESC LIMIT 1",
        (uid, tenant_id, options.content.strip()),
    ).fetchone()

    if existing:
        conn.execute(
            "UPDATE memories SET activation_count = activation_count + 1, updated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), existing["id"]),
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
        "content, emotional_context, metrics, importance, activation_count, "
        "created_at, updated_at, tags) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            memory_id,
            uid,
            tenant_id,
            opts.category,
            memory.scope,
            memory.retention,
            options.content.strip(),
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
    if not row.get("vector_id"):
        conn.close()
        return "Cannot archive memory without vector_id. Embed first."

    ms = get_memory_system()
    ghost = ms["linker"].to_ghost(
        MemoryObject(
            id=row["id"],
            timestamp=row["created_at"],
            scope=row["scope"],
            retention=row["retention"],
            content=row["content"],
            tags=json.loads(row["tags"]) or [],
            synthesized_from=json.loads(row["synthesized_from"]) or [],
            is_ghost=bool(row.get("is_ghost", 0)),
            vector_id=row["vector_id"],
            gist=row.get("gist"),
        )
    )
    conn.execute(
        "UPDATE memories SET content = ?, is_ghost = 1, gist = ? WHERE id = ? AND user_id = ?",
        (ghost.content, ghost.gist, memory_id, uid),
    )
    conn.commit()
    conn.close()
    return f"Archived memory {memory_id} to ghost node. Gist: {ghost.gist}"


@mcp.tool()
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


@mcp.tool()
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
        "INSERT INTO memory_versions (id, memory_id, content, version, created_at, tags, emotional_context, metrics, rollback_of) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            version_id,
            options.memory_id,
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
        "UPDATE memories SET content = ?, tags = ?, emotional_context = ?, metrics = ?, version = ?, updated_at = ? WHERE id = ? AND user_id = ?",
        (
            version_row["content"],
            version_row["tags"],
            version_row["emotional_context"],
            version_row["metrics"],
            new_version,
            datetime.now(timezone.utc).isoformat(),
            options.memory_id,
            uid,
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
# Subconscious Memory Block Tools
# =============================================================================


def _handle_subconscious_list(agent) -> str:
    """Helper for subconscious list action."""
    blocks = agent.get_all_blocks()
    return json.dumps(blocks, indent=2)


def _handle_subconscious_get(agent, label: str) -> str:
    """Helper for subconscious get action."""
    content = agent.get_block(label)
    if content:
        return f"[{label}]\n{content}"
    return f"Block '{label}' not found."


def _handle_subconscious_update(agent, label: str, content: str | None) -> str:
    """Helper for subconscious update action."""
    if content is None:
        return "Error: 'content' is required for update."
    if label == "guidance":
        agent.update_guidance(content)
    else:
        agent.state.update_block(label, content)
    return f"Updated block '{label}'"


@mcp.tool()
def manage_subconscious(options: SubconsciousAction, user_id: str | None = None) -> str:
    """
    Manage subconscious memory blocks (guidance, preferences, context).

    Args:
        options: Action and parameters (list, get, update, reset, clear)
        user_id: Optional user ID override
    """
    uid = user_id or USER_ID
    agent = get_subconscious_agent(uid)

    if options.action == "list":
        return _handle_subconscious_list(agent)

    if not options.label:
        return "Error: 'label' is required for this action."

    if options.action == "get":
        return _handle_subconscious_get(agent, options.label)

    if options.action == "update":
        return _handle_subconscious_update(agent, options.label, options.content)

    if options.action in ("reset", "clear"):
        if options.action == "reset":
            agent.reset_block(options.label)
        else:
            agent.clear_block(options.label)
        suffix = " to default" if options.action == "reset" else ""
        return f"{options.action.capitalize()}ed block '{options.label}'{suffix}"

    return f"Unsupported action: {options.action}"


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
            tenant_id = get_current_tenant_id()
            conn = get_db_connection()
            existing = conn.execute(
                "SELECT id, activation_count FROM memories "
                "WHERE user_id = ? AND tenant_id = ? AND content = ? AND is_ghost = 0 "
                "ORDER BY created_at DESC LIMIT 1",
                (uid, tenant_id, content),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE memories SET activation_count = activation_count + 1, updated_at = ? WHERE id = ?",
                    (now, existing["id"]),
                )
                conn.commit()
                conn.close()
                continue

            mid = hashlib.sha256(f"{content}{now}".encode()).hexdigest()[:16]
            conn.execute(
                "INSERT OR IGNORE INTO memories "
                "(id, content, scope, retention, category, user_id, bank_id, tenant_id, "
                "created_at, updated_at, tags, emotional_context, metrics, "
                "is_ghost, synthesized_from) "
                "VALUES (?, ?, 'arc', 'long_term', ?, ?, ?, ?, ?, ?, '[]', '{}', '{}', 0, '[]')",
                (mid, content, category, uid, BANK_ID, tenant_id, now, now),
            )
            conn.commit()
            conn.close()
            stored += 1

    return stored


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


@mcp.tool()
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
    agent = get_subconscious_agent(uid)

    _run_async(agent.process_transcript(session_id=session_id, messages=messages, project_path=project_path))

    _bridge_subconscious_to_memories(agent, uid)
    _bridge_transcript_entities(messages, uid)

    return f"Processed transcript for session {session_id}"


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


def _score_memory_relevance(
    memory: sqlite3.Row,
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


@mcp.tool()
def inject_context(
    conversation_text: str,
    user_id: str | None = None,
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

    conn = get_db_connection()
    candidates: list[sqlite3.Row] = []

    if terms:
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
            [uid, get_current_tenant_id(), *params],
        ).fetchall()

    fallback = conn.execute(
        "SELECT * FROM memories "
        "WHERE user_id = ? AND tenant_id = ? AND is_ghost = 0 "
        "AND importance >= ? "
        "ORDER BY importance DESC, created_at DESC LIMIT 20",
        (uid, get_current_tenant_id(), min_relevance),
    ).fetchall()

    conn.close()

    seen_ids: set[str] = set()
    all_rows: list[sqlite3.Row] = []
    for row in candidates + fallback:
        if row["id"] not in seen_ids:
            seen_ids.add(row["id"])
            all_rows.append(row)

    scored = [(row, _score_memory_relevance(row, terms, now)) for row in all_rows]
    scored.sort(key=lambda pair: pair[1], reverse=True)

    top = [(row, score) for row, score in scored if score >= min_relevance]
    top = top[:max_memories]

    lines: list[str] = []
    if top:
        lines.append(f"[Relevant Context - {len(top)} memories surfaced]")
        for row, _ in top:
            importance_val = row["importance"] if row["importance"] is not None else 1.0
            snippet = (row["content"] or "")[:120]
            if len(row["content"] or "") > 120:
                snippet += "..."
            lines.append(f"- [{row['id']}] (importance: {importance_val:.1f}) {snippet}")

    sub_lines = _subconscious_context_for_terms(uid, terms)
    if sub_lines:
        if not top:
            lines.append("[Relevant Context - 0 memories surfaced]")
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
        content_lower = content.lower()
        if terms and any(re.search(rf"\b{re.escape(t)}\b", content_lower) for t in terms):
            matching = []
            for line in content.splitlines():
                line_lower = line.lower().strip()
                if line_lower and any(re.search(rf"\b{re.escape(t)}\b", line_lower) for t in terms):
                    matching.append(line.strip())
            if matching:
                lines.append(f"[{label}]")
                for m in matching[:3]:
                    lines.append(f"  {m}")

    return lines


def main():
    init_db()
    initialize_stream_producer()
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


@mcp.tool()
def switch_tenant(tenant_id: str) -> str:
    """
    Switch current tenant context.

    Args:
        tenant_id: Tenant to switch to

    Returns:
        Confirmation message
    """
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    conn.close()

    if not row:
        return f"Tenant '{tenant_id}' not found"

    set_current_tenant_id(tenant_id)
    return f"Switched to tenant '{tenant_id}'"


# =============================================================================
# Temporal Memory Tools
# =============================================================================

# =============================================================================
# Temporal and Status Tools
# =============================================================================


@mcp.tool()
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


@mcp.tool()
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


def memory_status() -> str:
    """Legacy alias for get_system_status() used by CLI health checks."""
    return get_system_status()


def store_memory(content: str, user_id: str | None = None, **kwargs) -> str:
    """Legacy alias for manage_memories(action="store") used by tests."""
    options = MemoryAction(action="store", content=content, options=MemoryOptions(**kwargs))
    return manage_memories(options, user_id=user_id)


def list_memories(limit: int = 10, offset: int = 0, user_id: str | None = None) -> str:
    """Legacy alias for search_memories(query_type="list")."""
    options = SearchOptions(query_type="list", limit=limit, offset=offset)
    return search_memories(options, user_id=user_id)


def query_memories(query: str, limit: int = 10, user_id: str | None = None) -> str:
    """Legacy alias for search_memories(query_type="keyword")."""
    options = SearchOptions(query_type="keyword", query=query, limit=limit)
    return search_memories(options, user_id=user_id)


def get_memory(memory_id: str, user_id: str | None = None) -> str:
    """Legacy alias for search_memories(query_type="id")."""
    options = SearchOptions(query_type="id", memory_id=memory_id)
    return search_memories(options, user_id=user_id)


def update_memory(memory_id: str, **kwargs) -> str:
    """Legacy alias for manage_memories(action="update")."""
    user_id = kwargs.pop("user_id", None)
    updates = MemoryUpdateOptions(**kwargs)
    options = MemoryAction(action="update", memory_id=memory_id, updates=updates)
    return manage_memories(options, user_id=user_id)


def delete_memory(memory_id: str, user_id: str | None = None) -> str:
    """Legacy alias for manage_memories(action="delete")."""
    options = MemoryAction(action="delete", memory_id=memory_id)
    return manage_memories(options, user_id=user_id)


def archive_memory(memory_id: str, user_id: str | None = None) -> str:
    """Legacy alias for manage_memories(action="archive")."""
    options = MemoryAction(action="archive", memory_id=memory_id)
    return manage_memories(options, user_id=user_id)


# =============================================================================
# Entity and Graph Tools
# =============================================================================


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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
