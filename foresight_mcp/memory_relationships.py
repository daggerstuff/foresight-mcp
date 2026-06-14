"""
Memory Relationship Store for MEM-4.

Typed, tenant-isolated graph of relationships between memories.
Backed by the `memory_relationships` table created in schema migration v6.

Relationship types:
- updates:    source supersedes target (newer/better version)
- extends:    source adds detail to target
- derives:    source was derived from target (e.g. synthesis, summary)
- contradicts: source conflicts with target
- supports:   source backs up target
- related:    generic typed association
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import DB_PATH
from .connection_pool import get_pool
from .tenant_context import get_current_tenant_id

logger = logging.getLogger("foresight_memory_relationships")

VALID_RELATIONSHIP_TYPES: frozenset[str] = frozenset(
    {
        "updates",
        "extends",
        "derives",
        "contradicts",
        "supports",
        "related",
    }
)

MAX_USER_ID_LENGTH = 128
MAX_TENANT_ID_LENGTH = 64
MAX_MEMORY_ID_LENGTH = 128
MAX_METADATA_BYTES = 16_384


class MemoryRelationshipError(ValueError):
    """Raised on invalid relationship input or constraint violations."""


@dataclass
class MemoryRelationship:
    """A typed directed edge between two memories."""

    id: str
    tenant_id: str
    user_id: str
    source_memory_id: str
    target_memory_id: str
    relationship_type: str
    confidence: float
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MemoryGraphTraversal:
    """Result of traversing the memory relationship graph."""

    root_memory_id: str
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    depth: int


def _validate_user_tenant(user_id: str, tenant_id: str) -> None:
    if not user_id or len(user_id) > MAX_USER_ID_LENGTH:
        raise MemoryRelationshipError(f"user_id must be 1-{MAX_USER_ID_LENGTH} chars")
    if not tenant_id or len(tenant_id) > MAX_TENANT_ID_LENGTH:
        raise MemoryRelationshipError(f"tenant_id must be 1-{MAX_TENANT_ID_LENGTH} chars")


def _validate_memory_id(value: str, field_name: str) -> None:
    if not value or len(value) > MAX_MEMORY_ID_LENGTH:
        raise MemoryRelationshipError(f"{field_name} must be 1-{MAX_MEMORY_ID_LENGTH} chars")


def _validate_relationship_type(rel_type: str) -> None:
    if rel_type not in VALID_RELATIONSHIP_TYPES:
        raise MemoryRelationshipError(
            f"relationship_type must be one of {sorted(VALID_RELATIONSHIP_TYPES)}, got {rel_type!r}"
        )


def _validate_confidence(confidence: float) -> None:
    if not isinstance(confidence, (int, float)):
        raise MemoryRelationshipError("confidence must be a number")
    if confidence < 0.0 or confidence > 1.0:
        raise MemoryRelationshipError("confidence must be in [0.0, 1.0]")


def _validate_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    metadata = metadata or {}
    serialized = json.dumps(metadata, ensure_ascii=False)
    if len(serialized.encode("utf-8")) > MAX_METADATA_BYTES:
        raise MemoryRelationshipError(f"metadata exceeds {MAX_METADATA_BYTES} bytes")
    return metadata


class MemoryRelationshipStore:
    """SQLite-backed store for typed memory-to-memory relationships."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._ensure_table()

    def _connect(self) -> sqlite3.Connection:
        pool = get_pool(self.db_path)
        conn = pool.acquire()
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_table(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_relationships (
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
                    UNIQUE(tenant_id, user_id, source_memory_id, target_memory_id, relationship_type)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_relationships_source "
                "ON memory_relationships(tenant_id, user_id, source_memory_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_relationships_target "
                "ON memory_relationships(tenant_id, user_id, target_memory_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_relationships_type "
                "ON memory_relationships(tenant_id, user_id, relationship_type)"
            )
            conn.commit()
        finally:
            pool = getattr(conn, "_pool", None)
            if pool is not None:
                pool.release(conn)
            else:
                conn.close()

    def link_memories(  # noqa: PLR0913
        self,
        source_memory_id: str,
        target_memory_id: str,
        relationship_type: str,
        user_id: str,
        tenant_id: str | None = None,
        confidence: float = 1.0,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRelationship:
        """Create or update a relationship edge between two memories."""
        _validate_memory_id(source_memory_id, "source_memory_id")
        _validate_memory_id(target_memory_id, "target_memory_id")
        if source_memory_id == target_memory_id:
            raise MemoryRelationshipError("source_memory_id and target_memory_id must differ")
        _validate_relationship_type(relationship_type)
        _validate_confidence(confidence)
        tid = tenant_id or get_current_tenant_id()
        _validate_user_tenant(user_id, tid)
        meta = _validate_metadata(metadata)

        rel_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO memory_relationships (
                    id, tenant_id, user_id,
                    source_memory_id, target_memory_id,
                    relationship_type, confidence,
                    metadata, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, user_id, source_memory_id, target_memory_id, relationship_type)
                DO UPDATE SET
                    confidence = excluded.confidence,
                    metadata = excluded.metadata
                """,
                (
                    rel_id,
                    tid,
                    user_id,
                    source_memory_id,
                    target_memory_id,
                    relationship_type,
                    float(confidence),
                    json.dumps(meta, ensure_ascii=False),
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT id, created_at FROM memory_relationships
                WHERE tenant_id = ? AND user_id = ?
                  AND source_memory_id = ? AND target_memory_id = ?
                  AND relationship_type = ?
                """,
                (tid, user_id, source_memory_id, target_memory_id, relationship_type),
            ).fetchone()
            conn.commit()
        finally:
            pool = getattr(conn, "_pool", None)
            if pool is not None:
                pool.release(conn)
            else:
                conn.close()

        actual_id = row["id"] if row else rel_id
        actual_created_at = row["created_at"] if row else now

        return MemoryRelationship(
            id=actual_id,
            tenant_id=tid,
            user_id=user_id,
            source_memory_id=source_memory_id,
            target_memory_id=target_memory_id,
            relationship_type=relationship_type,
            confidence=float(confidence),
            metadata=meta,
            created_at=actual_created_at,
        )

    def get_relationships_for_memory(
        self,
        memory_id: str,
        user_id: str,
        tenant_id: str | None = None,
        direction: str = "both",
        relationship_type: str | None = None,
    ) -> list[MemoryRelationship]:
        """Return relationships touching a memory (direction: out, in, both)."""
        _validate_memory_id(memory_id, "memory_id")
        if direction not in {"out", "in", "both"}:
            raise MemoryRelationshipError("direction must be one of 'out', 'in', 'both'")
        if relationship_type is not None:
            _validate_relationship_type(relationship_type)
        tid = tenant_id or get_current_tenant_id()
        _validate_user_tenant(user_id, tid)

        clauses = ["tenant_id = ?", "user_id = ?"]
        params: list[Any] = [tid, user_id]

        if direction == "out":
            clauses.append("source_memory_id = ?")
            params.append(memory_id)
        elif direction == "in":
            clauses.append("target_memory_id = ?")
            params.append(memory_id)
        else:
            clauses.append("(source_memory_id = ? OR target_memory_id = ?)")
            params.extend([memory_id, memory_id])

        if relationship_type is not None:
            clauses.append("relationship_type = ?")
            params.append(relationship_type)

        sql = (
            "SELECT id, tenant_id, user_id, source_memory_id, target_memory_id, "
            "relationship_type, confidence, metadata, created_at "
            "FROM memory_relationships WHERE " + " AND ".join(clauses) + " "
            "ORDER BY created_at DESC"
        )

        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            pool = getattr(conn, "_pool", None)
            if pool is not None:
                pool.release(conn)
            else:
                conn.close()

        results: list[MemoryRelationship] = []
        for r in rows:
            meta_raw = r["metadata"]
            try:
                meta = json.loads(meta_raw) if meta_raw else {}
            except (json.JSONDecodeError, TypeError):
                meta = {}
            results.append(
                MemoryRelationship(
                    id=r["id"],
                    tenant_id=r["tenant_id"],
                    user_id=r["user_id"],
                    source_memory_id=r["source_memory_id"],
                    target_memory_id=r["target_memory_id"],
                    relationship_type=r["relationship_type"],
                    confidence=float(r["confidence"]),
                    metadata=meta,
                    created_at=r["created_at"],
                )
            )
        return results

    def traverse_memory_graph(
        self,
        root_memory_id: str,
        user_id: str,
        tenant_id: str | None = None,
        max_depth: int = 2,
        limit: int = 100,
    ) -> MemoryGraphTraversal:
        """BFS traversal of memory relationship edges from a root node.

        Uses a recursive CTE to walk edges in both directions up to max_depth.
        Returns visited memory IDs and the edges traversed.
        """
        _validate_memory_id(root_memory_id, "root_memory_id")
        if max_depth < 0 or max_depth > 5:
            raise MemoryRelationshipError("max_depth must be in [0, 5]")
        if limit < 1 or limit > 1000:
            raise MemoryRelationshipError("limit must be in [1, 1000]")
        tid = tenant_id or get_current_tenant_id()
        _validate_user_tenant(user_id, tid)

        conn = self._connect()
        try:
            cur = conn.execute(
                """
                WITH RECURSIVE walk(direction_node, depth) AS (
                    SELECT ?, 0
                    UNION ALL
                    SELECT
                        CASE
                            WHEN e.source_memory_id = walk.direction_node
                                THEN e.target_memory_id
                            ELSE e.source_memory_id
                        END,
                        walk.depth + 1
                    FROM walk
                    JOIN memory_relationships e
                      ON e.tenant_id = ?
                     AND e.user_id   = ?
                     AND (e.source_memory_id = walk.direction_node
                          OR e.target_memory_id = walk.direction_node)
                    WHERE walk.depth < ?
                )
                SELECT DISTINCT direction_node AS memory_id
                FROM walk
                ORDER BY memory_id
                LIMIT ?
                """,
                (root_memory_id, tid, user_id, max_depth, limit),
            )
            node_ids = [r["memory_id"] for r in cur.fetchall()]
            if root_memory_id not in node_ids:
                node_ids = [root_memory_id, *node_ids]

            if len(node_ids) <= 1:
                edges: list[MemoryRelationship] = []
            else:
                placeholders = ",".join("?" for _ in node_ids)
                edge_cur = conn.execute(
                    f"""
                    SELECT id, tenant_id, user_id, source_memory_id,
                           target_memory_id, relationship_type, confidence,
                           metadata, created_at
                    FROM memory_relationships
                    WHERE tenant_id = ? AND user_id = ?
                      AND (source_memory_id IN ({placeholders})
                           OR target_memory_id IN ({placeholders}))
                    """,
                    [tid, user_id, *node_ids, *node_ids],
                )
                edges = [
                    MemoryRelationship(
                        id=r["id"],
                        tenant_id=r["tenant_id"],
                        user_id=r["user_id"],
                        source_memory_id=r["source_memory_id"],
                        target_memory_id=r["target_memory_id"],
                        relationship_type=r["relationship_type"],
                        confidence=float(r["confidence"]),
                        metadata=(json.loads(r["metadata"]) if r["metadata"] else {}),
                        created_at=r["created_at"],
                    )
                    for r in edge_cur.fetchall()
                ]
        finally:
            pool = getattr(conn, "_pool", None)
            if pool is not None:
                pool.release(conn)
            else:
                conn.close()

        return MemoryGraphTraversal(
            root_memory_id=root_memory_id,
            nodes=[{"memory_id": mid} for mid in node_ids],
            edges=[e.to_dict() for e in edges],
            depth=max_depth,
        )


class _MemoryRelationshipStoreSingleton:
    """Module-level singleton for MemoryRelationshipStore."""

    _instance: MemoryRelationshipStore | None = None

    @classmethod
    def get_instance(cls) -> MemoryRelationshipStore:
        """Return the process-singleton store, initializing lazily on first call."""
        if cls._instance is None:
            cls._instance = MemoryRelationshipStore(DB_PATH)
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton (test-only helper)."""
        cls._instance = None


def get_memory_relationship_store() -> MemoryRelationshipStore:
    """Return the process-singleton store, initializing lazily on first call."""
    return _MemoryRelationshipStoreSingleton.get_instance()


def reset_memory_relationship_store() -> None:
    """Reset the singleton (test-only helper)."""
    _MemoryRelationshipStoreSingleton.reset()
