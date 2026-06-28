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
import warnings as _warnings
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
from .backend import RedisCompanion, create_backend
from .block_registry import InjectionPoint, initialize_default_blocks
from .capture import get_capture_pipeline
from .clustering import ClusterResult, cluster_memories
from .config import (
    BANK_ID,
    DB_PATH,
    DEFAULT_BURST_LIMIT,
    DEFAULT_RATE_LIMIT,
    DEFAULT_TENANT_ID,
    REDIS_URL,
    USER_ID,
)
from .capture import get_capture_pipeline
from .backend import create_backend
from .connection_pool import get_pool
from .context_blocks import (
    PENDING_ITEMS,
    SESSION_PATTERNS,
    USER_PREFERENCES,
    get_context_block_agent,
)
from .crisis_detection import get_crisis_service
from .decay_model import DecayConfigOptions, get_decay_model
from .document_layer import (
    DEFAULT_CHUNK_CHAR_BUDGET as _DOC_CHUNK_BUDGET,
    DocumentCreateOptions,
    DocumentLayerError,
    content_hash as _content_hash,
    get_document_store,
)
from .enhanced_synthesizer import get_enhanced_synthesizer
from .entity_extractor import Entity, get_entity_extractor
from .event_bus import (
    curation_status_changed,
    get_event_bus,
    memory_deleted,
    memory_retrieved,
    memory_stored,
    memory_updated,
)
from .graph_store import get_graph_store
from .hooks import (
    MemoryHookContext,
    MemoryHookType,
    _audit_hook,
    _cache_invalidation_hook,
    get_memory_hook_registry,
)
from .hybrid_retriever import HybridResult, HybridSearchOptions, HybridSearchResult, get_hybrid_retriever
from .injection_budget import (
    DEFAULT_LANE_WEIGHTS,
    MIN_LANE_CHARS,
    BudgetResult,
    InjectionBudget,
    Lane,
    LaneItem,
    format_budgeted_payload,
)
from .memory_components import (
    MemoryCrisisTagger,
    MemoryLinker,
    MemorySynthesizer,
    SocraticGate,
)
from .memory_maintenance import MaintenanceConfig, MemoryMaintenanceJob
from .memory_relationships import (
    LinkMemoriesOptions,
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
from .narrative_cache import NarrativeCache
from .phrase_triggers import DEFAULT_TRIGGERS, extract_triggered_memories
from .profile_synthesizer import ProfileConfig, profile_to_prompt, synthesize_profile as _synthesize_profile
from .rate_limiter import RateLimitExceededError, get_rate_limiter
from .reflection_engine import get_reflection_engine
from .reflection_narrative import _default_cache as _reflection_narrative_cache
from .semantic_search import (
    DEFAULT_PROVIDER as _SEMANTIC_DEFAULT_PROVIDER,
    SemanticSearchError as _SemanticSearchError,
    SemanticSearchOptions,
    get_semantic_search,
)
from .sensitivity import resolve_is_sensitive
from .stream_producer import (
    KafkaProducer,
    KinesisProducer,
    StreamPublisher,
    create_stream_producer,
)
from .sync import Operation, OperationQueue, OperationType
from .temporal_queries import get_temporal_query_builder
from .temporal_service import get_temporal_service
from .tenant_context import get_current_tenant_id, get_current_user_id, set_current_tenant_id
from .tenant_middleware import TenantMiddleware

# WebSocket imports
from .websocket.server import WebSocketServer
from .websocket.subscriptions import SubscriptionManager

DEFAULT_MAX_MEMORY_PER_TENANT = 100_000
DEFAULT_MAX_CACHE_ENTRIES_PER_TENANT = 50_000
DEFAULT_MAX_TFIDF_CACHE_SIZE = 10_000

# Last injection tracking for system status visibility (PIX-3955)
_last_injection_stats: dict[str, Any] = {}

# Global database backend (set during main() startup, PIX-3994)
_global_backend: Any = None

# Global Redis companion (set during main() startup, PIX-3995)
_redis_companion: RedisCompanion | None = None


# Global narrative cache instance for metrics (lazy initialization)
class _NarrativeCacheSingleton:
    """Module-level singleton for NarrativeCache."""

    _instance: NarrativeCache | None = None

    @classmethod
    def get_instance(cls) -> NarrativeCache:
        """Get or create global narrative cache instance."""
        if cls._instance is None:
            cache_path = Path(DB_PATH).parent / "narrative_cache.sqlite"
            cls._instance = NarrativeCache(cache_path)
        return cls._instance


def get_narrative_cache() -> NarrativeCache:
    """Get or create global narrative cache instance."""
    return _NarrativeCacheSingleton.get_instance()


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
    relation_type: str | None = Field(
        default=None,
        description="Optional typed relationship to another memory. Allowed: updates, extends, derives, contradicts, supports, related",
    )
    related_memory_id: str | None = Field(
        default=None, description="ID of the memory this one relates to (paired with relation_type)"
    )
    is_sensitive: bool | None = Field(
        default=None,
        description=(
            "PIX-3956 sensitivity override. None defers to the detector on memory.content;"
            " True forces the row is_sensitive=1; False forces is_sensitive=0."
        ),
    )


class MemoryUpdateOptions(BaseModel):
    content: str | None = Field(default=None, description="New memory content")
    category: str | None = Field(default=None, description="New category label")
    scope: str | None = Field(default=None, description="New memory scope")
    retention: str | None = Field(default=None, description="New retention policy")
    tags: list[str] | None = Field(default=None, description="New list of tags")
    is_sensitive: bool | None = Field(
        default=None,
        description=(
            "Sensitivity override on update. None defers to the detector on memory.content;"
            " True forces is_sensitive=1; False forces is_sensitive=0."
        ),
    )
    relation_type: str | None = Field(
        default=None,
        description="Optional typed relationship to another memory. Allowed: updates, extends, derives, contradicts, supports, related",
    )
    related_memory_id: str | None = Field(
        default=None, description="ID of the memory this one relates to (paired with relation_type)"
    )


class SearchOptions(BaseModel):
    query_type: Literal["id", "keyword", "list"] = Field(default="keyword", description="Type of search/retrieval")
    query: str | None = Field(default=None, description="Search query string")
    memory_id: str | None = Field(default=None, description="Retrieve specific memory by ID")
    limit: int = Field(default=10, description="Maximum results")
    offset: int = Field(default=0, description="Result offset")
    min_importance: float = Field(default=0.1, description="Minimum importance threshold")
    use_hybrid: bool = Field(default=True, description="Enable hybrid search signals")
    use_cascade: bool = Field(default=False, description="Enable cascade search across related entities")
    cascade_depth: int = Field(default=2, description="Maximum depth for cascade traversal")
    cascade_limit: int = Field(default=100, description="Maximum results per cascade level")
    debug: bool = Field(
        default=False,
        description="Return structured trace with timing and signal metadata instead of formatted results",
    )


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
    run_clustering: bool = Field(
        default=False,
        description="If True, run memory clustering after curation completes",
    )
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
    include_cache_metrics: bool = Field(default=False, description="Whether to include cache and budget metrics")
    enforce_hard_caps: bool = Field(
        default=False, description="Whether to enforce hard caps on memory and cache counts"
    )


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


class MaintenanceAction(BaseModel):
    modes: list[str] = Field(
        default=["consolidate", "contradict", "archive_stale", "synthesize"],
        description="Maintenance modes to run",
    )
    duplicate_threshold: float = Field(default=0.25, description="Minimum Jaccard similarity to consider duplicate")
    stale_strength_threshold: float = Field(default=0.2, description="Archive memories below this strength")
    stale_importance_threshold: float = Field(default=0.1, description="Archive memories below this importance")
    batch_size: int = Field(default=200, description="Max memories to process per mode")
    max_runtime_seconds: float = Field(default=300, description="Wall-clock budget in seconds")


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
    """Check rate limit for tenant, raising RateLimitExceededError if exceeded."""
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
        raise RateLimitExceededError(remaining=remaining, reset_time=reset_time)


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
        # PIX-3956 clinical/safety/privacy gating. ALTER is idempotent on
        # re-runs because the runner catches "duplicate column" errors and
        # skips them. sensitivity_reason is nullable so legacy rows can
        # stay at NULL.
        "ALTER TABLE memories ADD COLUMN is_sensitive INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE memories ADD COLUMN sensitivity_reason TEXT",
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


def init_db(backend=None):
    """Initialize the database schema with idempotent versioned migrations.

    Args:
        backend: Optional DatabaseBackend. None → create via create_backend().
    """
    if backend is None:
        db_path = Path(DB_PATH)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        backend = create_backend()
    backend.connect()

    try:
        backend.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
        """)

        applied = {row["version"] for row in backend.fetch("SELECT version FROM schema_migrations")}

        for version in sorted(_SCHEMA_MIGRATIONS):
            if version in applied:
                continue
            for stmt in _SCHEMA_MIGRATIONS[version]:
                try:
                    backend.execute(stmt)
                except Exception as e:
                    err = str(e).lower()
                    if "duplicate column" in err or "already exists" in err:
                        continue
                    raise
            backend.set_version(version, datetime.now(timezone.utc).isoformat())

        # Ensure the built-in default tenant always exists so tenant switches are stable.
        # Use ON CONFLICT DO NOTHING (works on both SQLite 3.24+ and PostgreSQL)
        # instead of _seed_default_tenant(conn) which uses SQLite-specific INSERT OR IGNORE.
        backend.execute(
            """
            INSERT INTO tenants (id, name, rate_limit, burst_limit, created_at, config)
            VALUES (?, 'Default tenant', ?, ?, ?, '{}')
            ON CONFLICT (id) DO NOTHING
            """,
            (
                DEFAULT_TENANT_ID,
                DEFAULT_RATE_LIMIT,
                DEFAULT_BURST_LIMIT,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

        # Migrate decay_config: add tenant_id if table exists without it
        if backend.table_exists("decay_config") and not backend.column_exists("decay_config", "tenant_id"):
            backend.execute("ALTER TABLE decay_config ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'")
            backend.execute("CREATE INDEX IF NOT EXISTS idx_decay_config_tenant ON decay_config(tenant_id)")

        # Backfill content_hash for existing memories (v10 migration)
        try:
            rows = backend.fetch("SELECT id, content FROM memories WHERE content_hash IS NULL")
            if rows:
                for row in rows:
                    h = _content_hash(row["content"])
                    backend.execute("UPDATE memories SET content_hash = ? WHERE id = ?", (h, row["id"]))
        except Exception:
            pass  # Table doesn't exist yet; will be created by migrations
    finally:
        backend.close()


def _initialize_backend() -> None:
    """Create and connect the global database backend (PIX-3994).

    Reads ``FORESIGHT_DB_URL`` via ``create_backend()``.  The backend is stored
    as ``_global_backend`` and passed to service initializers.

    Fail-fast: if ``FORESIGHT_DB_URL`` is explicitly set but the backend
    fails to connect, the server aborts with a clear error message.
    """
    global _global_backend  # noqa: PLW0603
    try:
        backend = create_backend()
        backend.connect()
        _global_backend = backend
        logger.info("Database backend initialised (type=%s)", type(backend).__name__)
    except Exception:
        if os.environ.get("FORESIGHT_DB_URL"):
            logger.exception(
                "FATAL: FORESIGHT_DB_URL is set (%s) but the backend failed to connect. "
                "Aborting startup. Fix the connection string or unset FORESIGHT_DB_URL.",
                os.environ.get("FORESIGHT_DB_URL"),
            )
            raise
        logger.debug("No FORESIGHT_DB_URL set; skipping backend initialisation (SQLite pool will be used).")


def _initialize_redis() -> None:
    """Create and connect the global RedisCompanion (PIX-3995).

    If ``REDIS_URL`` is set, creates a ``RedisCompanion`` and eagerly
    connects.  The companion is stored as ``_redis_companion`` and
    gracefully degrades if Redis is unreachable.
    """
    global _redis_companion  # noqa: PLW0603
    if not REDIS_URL:
        logger.debug("No REDIS_URL set; RedisCompanion disabled")
        return
    companion = RedisCompanion(REDIS_URL)
    _run_async(companion._ensure_connected())  # eager connect
    _redis_companion = companion


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


    get_hybrid_retriever().invalidate_tfidf_cache(uid, tenant_id)

    # ── POST_STORE hook ────────────────────────────────────────────────
    hook_ctx.memory_id = memory_id
    get_memory_hook_registry().emit_post(MemoryHookType.POST_STORE, hook_ctx)

    return f"Stored memory {memory_id}. Gate: {gate_result.decision}. Reason: {gate_result.reason}"


def _handle_memory_update(uid: str, tenant_id: str, options: MemoryAction) -> str:
    """Helper to handle memory updates."""
    if not options.memory_id or not options.updates:
        return "Error: memory_id and updates required for 'update' action"

    # ── PRE_UPDATE hook ────────────────────────────────────────────────
    hook_ctx = MemoryHookContext(
        action="update",
        memory_id=options.memory_id,
        user_id=uid,
        tenant_id=tenant_id,
        content=options.updates.content,
        category=options.updates.category,
        scope=options.updates.scope,
        retention=options.updates.retention,
    )
    for r in get_memory_hook_registry().emit_pre(MemoryHookType.PRE_UPDATE, hook_ctx):
        if r.abort:
            return f"Hook aborted update: {r.message}"

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
        is_sensitive_bit, sensitivity_reason = resolve_is_sensitive(
            options.updates.is_sensitive, options.updates.content.strip()
        )
        updates_list.extend(["is_sensitive = ?", "sensitivity_reason = ?"])
        values.extend([1 if is_sensitive_bit else 0, sensitivity_reason])
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
    was_sensitive = bool(dict(row).get("is_sensitive", 0))
    old_evt = "[REDACTED - sensitive]" if was_sensitive else (row["content"] or "")
    new_evt = "[REDACTED - sensitive]" if was_sensitive else (options.updates.content or row["content"] or "")
    get_event_bus_with_stream().publish(
        memory_updated(
            memory_id=options.memory_id,
            old_content=old_evt,
            new_content=new_evt,
            actor=uid,
        )
    )
    get_hybrid_retriever().invalidate_tfidf_cache(uid, tenant_id)

    # ── POST_UPDATE hook ───────────────────────────────────────────────
    hook_ctx.old_content = row["content"]
    hook_ctx.content = options.updates.content or row["content"]
    get_memory_hook_registry().emit_post(MemoryHookType.POST_UPDATE, hook_ctx)
    # Create memory relationship if specified
    if options.updates.relation_type and options.updates.related_memory_id:
        store = get_memory_relationship_store()
        try:
            store.link_memories(
                source_memory_id=options.memory_id,
                target_memory_id=options.updates.related_memory_id,
                relationship_type=options.updates.relation_type,
                user_id=uid,
                tenant_id=tenant_id,
            )
        except MemoryRelationshipError as exc:
            logger.warning(f"Failed to create memory relationship: {exc}")
    return f"Updated memory {options.memory_id}"


def _handle_memory_delete(uid: str, tenant_id: str, memory_id: str | None) -> str:
    """Helper to handle memory deletion."""
    if not memory_id:
        return "Error: memory_id required for 'delete' action"

    # ── PRE_DELETE hook ────────────────────────────────────────────────
    hook_ctx = MemoryHookContext(
        action="delete",
        memory_id=memory_id,
        user_id=uid,
        tenant_id=tenant_id,
    )
    for r in get_memory_hook_registry().emit_pre(MemoryHookType.PRE_DELETE, hook_ctx):
        if r.abort:
            return f"Hook aborted delete: {r.message}"

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

    # ── POST_DELETE hook ───────────────────────────────────────────────
    get_memory_hook_registry().emit_post(MemoryHookType.POST_DELETE, hook_ctx)

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

    if options.action == "store" and tenant_id == "default" and os.environ.get("PYTEST_CURRENT_TEST"):
        return (
            "Error: refusing to store to the 'default' tenant under pytest; set FORESIGHT_DB_PATH or use a test tenant"
        )

    if options.action == "store":
        return _handle_memory_store(uid, tenant_id, options)

    if options.action == "update":
        return _handle_memory_update(uid, tenant_id, options)

    if options.action == "delete":
        return _handle_memory_delete(uid, tenant_id, options.memory_id)

    if options.action == "archive":
        return _handle_memory_archive(uid, tenant_id, options.memory_id)

    return f"Unknown action: {options.action}"


@dataclass
class SearchTrace:
    """Structured trace of a retrieval operation for performance observability."""

    query: str
    latency_ms: float
    result_count: int
    total_candidates: int
    signal_counts: dict[str, Any]
    fast_path: int | str | None
    response_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "latency_ms": self.latency_ms,
            "result_count": self.result_count,
            "total_candidates": self.total_candidates,
            "signal_counts": self.signal_counts,
            "fast_path": self.fast_path,
            "response_bytes": self.response_bytes,
        }


def _trace_retrieval(
    query: str,
    uid: str,
    tenant_id: str,
    options: HybridSearchOptions,
) -> tuple[HybridSearchResult, SearchTrace]:
    """Run hybrid search and return (result, trace) pair."""
    t0 = time.perf_counter()
    retriever = get_hybrid_retriever()
    result = retriever.search(query, uid, options)
    latency_ms = (time.perf_counter() - t0) * 1000
    fast_path = result.signal_counts.get("fast_path")
    trace = SearchTrace(
        query=query,
        latency_ms=round(latency_ms, 2),
        result_count=len(result.results),
        total_candidates=result.total_candidates,
        signal_counts=result.signal_counts,
        fast_path=fast_path,
        response_bytes=0,
    )
    logger.debug(
        "search_memories trace: query=%r user=%s tenant=%s latency_ms=%.2f results=%d candidates=%d fast_path=%s signals=%s",
        query,
        uid,
        tenant_id,
        trace.latency_ms,
        trace.result_count,
        trace.total_candidates,
        fast_path,
        result.signal_counts,
    )
    return result, trace


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

    # ── PRE_RETRIEVE hook ──────────────────────────────────────────────
    hook_ctx = MemoryHookContext(
        action="retrieve",
        user_id=uid,
        tenant_id=tenant_id,
        query=options.query,
    )
    for r in get_memory_hook_registry().emit_pre(MemoryHookType.PRE_RETRIEVE, hook_ctx):
        if r.abort:
            return f"Hook aborted retrieval: {r.message}"
        if r.modified_context:
            query_override = r.modified_context.get("query", options.query)
            if query_override:
                options.query = query_override

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
            # ── POST_RETRIEVE hook (no results) ────────────────────────
            get_memory_hook_registry().emit_post(MemoryHookType.POST_RETRIEVE, hook_ctx)
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

        # ── POST_RETRIEVE hook (ID lookup) ─────────────────────────────
        hook_ctx.memory_id = mid
        get_memory_hook_registry().emit_post(MemoryHookType.POST_RETRIEVE, hook_ctx)
        return result

    # 2. Hybrid search if enabled and query provided
    if options.use_hybrid and options.query:
        try:
            hybrid_result, trace = _trace_retrieval(
                options.query,
                uid,
                tenant_id,
                HybridSearchOptions(
                    tenant_id=tenant_id,
                    limit=options.limit,
                    min_importance=options.min_importance,
                    use_keyword=True,
                    use_tfidf_cosine=True,
                    use_semantic=None,
                    use_graph=True,
                    use_temporal=True,
                ),
            )
            if options.debug:
                return json.dumps(trace.to_dict(), indent=2)
            if hybrid_result.results:
                results = []
                for r in hybrid_result.results:
                    signals = ", ".join(r.source_signals) if r.source_signals else "hybrid"
                    results.append(
                        f"- [{r.memory_id}] {r.content[:100]}... (score={r.combined_score:.3f}, signals={signals})"
                    )
                # ── POST_RETRIEVE hook (hybrid search) ─────────────────
                get_memory_hook_registry().emit_post(MemoryHookType.POST_RETRIEVE, hook_ctx)
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
        # ── POST_RETRIEVE hook (no results) ────────────────────────────
        get_memory_hook_registry().emit_post(MemoryHookType.POST_RETRIEVE, hook_ctx)
        return "No memories found."

    results = [f"- [{r['id']}] ({r['scope']}/{r['retention']}) {r['content'][:80]}..." for r in rows]
    # ── POST_RETRIEVE hook (fallback) ─────────────────────────────────
    get_memory_hook_registry().emit_post(MemoryHookType.POST_RETRIEVE, hook_ctx)
    return f"Memories ({len(results)} found):\n" + "\n".join(results)


