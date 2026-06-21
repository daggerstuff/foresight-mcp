"""
Graph Store for Entity-Relationship Memory.

SQLite-backed graph storage with:
- Entity CRUD operations
- Relationship management
- Graph traversal using recursive CTEs
- Memory-to-entity linking
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from .backend.base import DatabaseBackend
from .config import DB_PATH
from .connection_pool import get_pool
from .entity_extractor import Entity, EntityType, ExtractionResult, Relationship, RelationshipType
from .sql_helpers import build_type_filter
from .tenant_context import get_current_tenant_id

logger = logging.getLogger("foresight_graph_store")

MAX_USER_ID_LENGTH = 128
MAX_TENANT_ID_LENGTH = 64
MAX_NODE_IDS_IN_CLAUSE = 1000  # Prevent excessively large IN clauses


def _escape_like(term: str) -> str:
    """Escape SQL LIKE metacharacters to prevent wildcard injection."""
    # Replace in order: ! first, then %, then _ to avoid double escaping
    return term.replace("!", "!!").replace("%", "!%").replace("_", "!_")


def _validate_input(user_id: str, tenant_id: str | None = None) -> None:
    """Validate user_id and tenant_id input."""
    if not user_id or len(user_id) > MAX_USER_ID_LENGTH:
        raise ValueError(f"user_id must be 1-{MAX_USER_ID_LENGTH} chars")
    tid = tenant_id or get_current_tenant_id()
    if not tid or len(tid) > MAX_TENANT_ID_LENGTH:
        raise ValueError(f"tenant_id must be 1-{MAX_TENANT_ID_LENGTH} chars")


@dataclass
class GraphTraversalResult:
    """Result of graph traversal."""

    nodes: list[Entity]
    edges: list[Relationship]
    paths: list[dict[str, Any]] = field(default_factory=list)


class GraphStore:
    """
    SQLite-backed graph store for entities and relationships.

    Supports:
    - Entity CRUD operations
    - Relationship management with confidence tracking
    - Graph traversal with depth limits
    - Memory-to-entity linking
    """

    def __init__(self, db_path: str, backend: DatabaseBackend | None = None):
        self.db_path = db_path
        self.backend = backend
        if backend is None:
            self._init_db()

    def _fetch_rows(self, sql: str, params: tuple | list | None = None) -> list[dict[str, Any]]:
        if self.backend is not None:
            p: tuple | dict = tuple(params) if isinstance(params, list) else (params or ())
            rows = self.backend.fetch(sql, p)
            return [dict(row) for row in rows]
        _pool = get_pool(self.db_path)
        conn = _pool.acquire()
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            cursor = conn.execute(sql, params or [])
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        finally:
            _pool.release(conn)

    def _execute_sql(self, sql: str, params: tuple | list | None = None) -> None:
        if self.backend is not None:
            p: tuple | dict = tuple(params) if isinstance(params, list) else (params or ())
            self.backend.execute(sql, p)
            return
        _pool = get_pool(self.db_path)
        conn = _pool.acquire()
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            conn.execute(sql, params or [])
            conn.commit()
        finally:
            _pool.release(conn)

    @contextmanager
    def _connection(self) -> Generator[Any]:
        if self.backend is not None:
            raise RuntimeError(
                "_connection() not available when backend is set. "
                "Use _fetch_rows/_execute_sql for backend-agnostic access."
            )
        _pool = get_pool(self.db_path)
        conn = _pool.acquire()
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
        finally:
            _pool.release(conn)

    def _detect_columns(self, table: str) -> list[str]:
        """Detect column names for a table, backend-aware."""
        if self.backend is not None:
            return [
                "id",
                "user_id",
                "tenant_id",
                "name",
                "entity_type",
                "description",
                "properties",
                "created_at",
                "updated_at",
            ]
        with self._connection() as conn:
            cursor = conn.execute(f"PRAGMA table_info({table})")
            return [row["name"] for row in cursor.fetchall()]

    def _init_db(self) -> None:
        """Initialize database schema (SQLite only)."""
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            cursor = conn.cursor()

        try:
            # Entities table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS memory_entities (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                entity_type TEXT NOT NULL
                CHECK(entity_type IN ('person', 'place', 'concept', 'event', 'emotion', 'object', 'cluster')),
                description TEXT,
                properties TEXT DEFAULT '{}',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(tenant_id, user_id, name, entity_type)
            )
            """)

            # Relationships table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS entity_relationships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                user_id TEXT NOT NULL,
                source_entity_id TEXT NOT NULL,
                target_entity_id TEXT NOT NULL,
                relationship_type TEXT NOT NULL
                CHECK(relationship_type IN (
                    'mentions', 'located_at', 'experienced', 'caused',
                    'relates_to', 'contradicts', 'supports', 'part_of', 'created'
                )),
                confidence REAL DEFAULT 1.0 CHECK(confidence >= 0 AND confidence <= 1),
        last_accessed TEXT DEFAULT CURRENT_TIMESTAMP,
        decay_factor REAL DEFAULT 1.0 CHECK(decay_factor >= 0 AND decay_factor <= 1),
                metadata TEXT DEFAULT '{}',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(tenant_id, user_id, source_entity_id, target_entity_id, relationship_type)
            )
            """)

            # Memory-to-entity links
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS memory_entity_links (
                memory_id TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                user_id TEXT NOT NULL,
                relevance_score REAL DEFAULT 1.0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (memory_id, entity_id)
            )
            """)

            # Memories table (if not existing, create full schema)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS memories (
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
                accessed_at TEXT DEFAULT CURRENT_TIMESTAMP,
                importance REAL DEFAULT 1.0,
                decay_rate REAL DEFAULT 0.01,
                activation_count INTEGER DEFAULT 0,
                retrieval_count INTEGER DEFAULT 0,
                strength_trend TEXT DEFAULT 'stable',
                last_retrieved_at TEXT,
                version INTEGER DEFAULT 1
            )
            """)

            # Memory versions table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS memory_versions (
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
            )
            """)

            # Migrate existing tables that lack tenant_id before creating indexes
            self._migrate_add_tenant_id(conn)

            conn.commit()

            # Indexes (after migration ensures tenant_id column exists)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_entities_user ON memory_entities(user_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_entities_tenant ON memory_entities(tenant_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_entities_tenant ON memory_entities(tenant_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_entities_type ON memory_entities(entity_type)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_entities_name ON memory_entities(name)")

            cursor.execute("CREATE INDEX IF NOT EXISTS idx_rel_source ON entity_relationships(source_entity_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_rel_target ON entity_relationships(target_entity_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_rel_user ON entity_relationships(user_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_rel_tenant ON entity_relationships(tenant_id)")

            cursor.execute("CREATE INDEX IF NOT EXISTS idx_links_memory ON memory_entity_links(memory_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_links_entity ON memory_entity_links(entity_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_links_user ON memory_entity_links(user_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_links_tenant ON memory_entity_links(tenant_id)")

            conn.commit()
            logger.info("Graph store schema initialized")

        except sqlite3.Error as e:
            conn.rollback()
            logger.error(f"Failed to initialize graph store: {e}")
            raise

    # =========================================================================
    # Schema Migration
    # =========================================================================

    def _migrate_add_tenant_id(self, conn) -> None:
        for table in ("memory_entities", "entity_relationships", "memory_entity_links"):
            try:
                # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query
                cursor = conn.execute(f"PRAGMA table_info({table})")
                columns = [row["name"] for row in cursor.fetchall()]
            except sqlite3.OperationalError:
                continue  # Table doesn't exist yet, CREATE TABLE IF NOT EXISTS will handle it

            if "tenant_id" not in columns:
                if table == "memory_entities":
                    self._rebuild_entities_table(conn)
                elif table == "entity_relationships":
                    self._rebuild_relationships_table(conn)
                else:
                    conn.execute("ALTER TABLE memory_entity_links ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'")
                logger.info(f"Migration: added tenant_id to {table}")

    @staticmethod
    def _rebuild_entities_table(conn: sqlite3.Connection) -> None:
        """Rebuild memory_entities table to add tenant_id in UNIQUE constraint."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_entities_new (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                entity_type TEXT NOT NULL
                CHECK(entity_type IN ('person', 'place', 'concept', 'event', 'emotion', 'object', 'cluster')),
                description TEXT,
                properties TEXT DEFAULT '{}',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(tenant_id, user_id, name, entity_type)
            )
        """)
        conn.execute("""
            INSERT OR IGNORE INTO memory_entities_new
            (id, tenant_id, user_id, name, entity_type, description, properties, created_at, updated_at)
            SELECT id, 'default', user_id, name, entity_type, description, properties, created_at, updated_at
            FROM memory_entities
        """)
        conn.execute("DROP TABLE memory_entities")
        conn.execute("ALTER TABLE memory_entities_new RENAME TO memory_entities")

    @staticmethod
    def _rebuild_relationships_table(conn: sqlite3.Connection) -> None:
        """Rebuild entity_relationships table to add tenant_id in UNIQUE constraint."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS entity_relationships_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                user_id TEXT NOT NULL,
                source_entity_id TEXT NOT NULL,
                target_entity_id TEXT NOT NULL,
                relationship_type TEXT NOT NULL
                CHECK(relationship_type IN (
                    'mentions', 'located_at', 'experienced', 'caused',
                    'relates_to', 'contradicts', 'supports', 'part_of', 'created'
                )),
                confidence REAL DEFAULT 1.0 CHECK(confidence >= 0 AND confidence <= 1),
        last_accessed TEXT DEFAULT CURRENT_TIMESTAMP,
        decay_factor REAL DEFAULT 1.0 CHECK(decay_factor >= 0 AND decay_factor <= 1),
                metadata TEXT DEFAULT '{}',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(tenant_id, user_id, source_entity_id, target_entity_id, relationship_type)
            )
        """)
        conn.execute("""
            INSERT OR IGNORE INTO entity_relationships_new
            (tenant_id, user_id, source_entity_id, target_entity_id, relationship_type, confidence, metadata, created_at)
            SELECT 'default', user_id, source_entity_id, target_entity_id, relationship_type, confidence, metadata, created_at
            FROM entity_relationships
        """)
        conn.execute("DROP TABLE entity_relationships")
        conn.execute("ALTER TABLE entity_relationships_new RENAME TO entity_relationships")

    # =========================================================================
    # Entity Operations
    # =========================================================================

    def upsert_entity(self, entity: Entity, user_id: str, tenant_id: str | None = None) -> str:
        tid = tenant_id or get_current_tenant_id()
        _validate_input(user_id, tid)
        self._execute_sql(
            """
        INSERT INTO memory_entities
        (id, tenant_id, user_id, name, entity_type, description, properties, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT(tenant_id, user_id, name, entity_type) DO UPDATE SET
            description = excluded.description,
            properties = excluded.properties,
            updated_at = CURRENT_TIMESTAMP
        """,
            (
                entity.id,
                tid,
                user_id,
                entity.name,
                entity.entity_type,
                entity.description,
                json.dumps(entity.properties),
            ),
        )
        return entity.id

    def get_entity(self, entity_id: str, user_id: str, tenant_id: str | None = None) -> Entity | None:
        tid = tenant_id or get_current_tenant_id()
        _validate_input(user_id, tid)
        rows = self._fetch_rows(
            """
        SELECT id, user_id, name, entity_type, description, properties
        FROM memory_entities
        WHERE id = ? AND tenant_id = ? AND user_id = ?
        """,
            (entity_id, tid, user_id),
        )
        if not rows:
            return None

        row = rows[0]
        return Entity(
            id=row["id"],
            name=row["name"],
            entity_type=cast(Literal["person", "place", "concept", "event", "emotion", "object"], row["entity_type"]),
            description=row["description"],
            properties=json.loads(row["properties"] or "{}"),
        )

    def get_entities_by_type(
        self,
        user_id: str,
        entity_type: str,
        limit: int = 100,
        tenant_id: str | None = None,
    ) -> list[Entity]:
        tid = tenant_id or get_current_tenant_id()
        _validate_input(user_id, tid)
        rows = self._fetch_rows(
            """
        SELECT id, user_id, name, entity_type, description, properties
        FROM memory_entities
        WHERE tenant_id = ? AND user_id = ? AND entity_type = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
            (tid, user_id, entity_type, limit),
        )
        return [
            Entity(
                id=row["id"],
                name=row["name"],
                entity_type=cast(
                    Literal["person", "place", "concept", "event", "emotion", "object"], row["entity_type"]
                ),
                description=row["description"],
                properties=json.loads(row["properties"] or "{}"),
            )
            for row in rows
        ]

    def find_entities_by_name(
        self,
        user_id: str,
        name: str,
        limit: int = 10,
        tenant_id: str | None = None,
    ) -> list[Entity]:
        tid = tenant_id or get_current_tenant_id()
        _validate_input(user_id, tid)
        escaped = _escape_like(name)
        rows = self._fetch_rows(
            """
        SELECT id, user_id, name, entity_type, description, properties
        FROM memory_entities
        WHERE tenant_id = ? AND user_id = ? AND name LIKE ? ESCAPE '!'
        LIMIT ?
        """,
            (tid, user_id, f"%{escaped}%", limit),
        )
        return [
            Entity(
                id=row["id"],
                name=row["name"],
                entity_type=cast(
                    Literal["person", "place", "concept", "event", "emotion", "object"], row["entity_type"]
                ),
                description=row["description"],
                properties=json.loads(row["properties"] or "{}"),
            )
            for row in rows
        ]

    # =========================================================================
    # Relationship Operations
    # =========================================================================

    def add_relationship(self, relationship: Relationship, user_id: str, tenant_id: str | None = None) -> None:
        tid = tenant_id or get_current_tenant_id()
        _validate_input(user_id, tid)
        self._execute_sql(
            """
        INSERT OR IGNORE INTO entity_relationships
        (tenant_id, user_id, source_entity_id, target_entity_id, relationship_type, confidence, metadata)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
            (
                tid,
                user_id,
                relationship.source_entity_id,
                relationship.target_entity_id,
                relationship.relationship_type,
                relationship.confidence,
                json.dumps(relationship.metadata),
            ),
        )

    def get_relationships(
        self,
        entity_id: str,
        user_id: str,
        direction: str = "both",
        tenant_id: str | None = None,
    ) -> list[Relationship]:
        tid = tenant_id or get_current_tenant_id()
        _validate_input(user_id, tid)
        if direction == "out":
            rows = self._fetch_rows(
                """
            SELECT source_entity_id, target_entity_id, relationship_type, confidence, metadata
            FROM entity_relationships
            WHERE source_entity_id = ? AND tenant_id = ? AND user_id = ?
            """,
                (entity_id, tid, user_id),
            )
        elif direction == "in":
            rows = self._fetch_rows(
                """
            SELECT source_entity_id, target_entity_id, relationship_type, confidence, metadata
            FROM entity_relationships
            WHERE target_entity_id = ? AND tenant_id = ? AND user_id = ?
            """,
                (entity_id, tid, user_id),
            )
        else:  # both
            rows = self._fetch_rows(
                """
            SELECT source_entity_id, target_entity_id, relationship_type, confidence, metadata
            FROM entity_relationships
            WHERE (source_entity_id = ? OR target_entity_id = ?) AND tenant_id = ? AND user_id = ?
            """,
                (entity_id, entity_id, tid, user_id),
            )

        return [
            Relationship(
                source_entity_id=row["source_entity_id"],
                target_entity_id=row["target_entity_id"],
                relationship_type=cast(RelationshipType, row["relationship_type"]),
                confidence=row["confidence"],
                metadata=json.loads(row["metadata"] or "{}"),
            )
            for row in rows
        ]

    # =========================================================================
    # Graph Traversal
    # =========================================================================

    def traverse_graph(
        self,
        start_entity_id: str,
        user_id: str,
        *args: Any,
        tenant_id: str | None = None,
        **kwargs: Any,
    ) -> GraphTraversalResult:
        max_depth = kwargs.get("max_depth", args[0] if len(args) > 0 else 2)
        max_results = kwargs.get("max_results", args[1] if len(args) > 1 else 50)
        relationship_types: list[str] | None = kwargs.get("relationship_types", args[2] if len(args) > 2 else None)
        tid = tenant_id or kwargs.get("tenant_id", args[3] if len(args) > 3 else None) or get_current_tenant_id()
        _validate_input(user_id, tid)
        type_filter, type_params = build_type_filter(relationship_types or [])

        query = f"""
        WITH RECURSIVE graph_traversal AS (
            SELECT
                id as entity_id,
                entity_type,
                name,
                description,
                properties,
                0 as depth
            FROM memory_entities
            WHERE id = ? AND tenant_id = ? AND user_id = ?

            UNION ALL

            SELECT
                CASE
                    WHEN gt.entity_id = r.source_entity_id THEN r.target_entity_id
                    ELSE r.source_entity_id
                END as entity_id,
                e.entity_type,
                e.name,
                e.description,
                e.properties,
                gt.depth + 1
            FROM graph_traversal gt
            JOIN entity_relationships r ON (
                (gt.entity_id = r.source_entity_id OR gt.entity_id = r.target_entity_id)
                AND r.tenant_id = ?
                {type_filter}
                AND r.confidence * r.decay_factor >= 0.1
            )
            JOIN memory_entities e ON e.id = CASE
                WHEN gt.entity_id = r.source_entity_id THEN r.target_entity_id
                ELSE r.source_entity_id
            END
            WHERE gt.depth < ?
            AND e.tenant_id = ?
            AND e.user_id = ?
        )
        SELECT DISTINCT entity_id, entity_type, name, description, properties, depth
        FROM graph_traversal
        WHERE depth > 0
        LIMIT ?
        """
        node_rows = self._fetch_rows(
            query,
            [
                start_entity_id,
                tid,
                user_id,
                tid,
                *type_params,
                max_depth,
                tid,
                user_id,
                max_results,
            ],
        )

        nodes = [
            Entity(
                id=row["entity_id"],
                name=row["name"],
                entity_type=cast(EntityType, row["entity_type"]),
                description=row["description"],
                properties=json.loads(row["properties"] or "{}"),
            )
            for row in node_rows
        ]

        node_ids = [n.id for n in nodes]
        if node_ids:
            if len(node_ids) > MAX_NODE_IDS_IN_CLAUSE:
                logger.warning(
                    f"Truncating node_ids from {len(node_ids)} to {MAX_NODE_IDS_IN_CLAUSE} "
                    "to prevent excessively large IN clause"
                )
                node_ids = node_ids[:MAX_NODE_IDS_IN_CLAUSE]

            edges = []
            edge_rows = []
            batch_size = 100
            for i in range(0, len(node_ids), batch_size):
                batch = node_ids[i : i + batch_size]
                if not batch:
                    continue
                placeholders = ",".join("?" * len(batch))
                batch_edge_rows = self._fetch_rows(
                    f"""
                SELECT source_entity_id, target_entity_id, relationship_type, confidence, metadata
                FROM entity_relationships
                WHERE (source_entity_id IN ({placeholders})
                OR target_entity_id IN ({placeholders}))
                AND tenant_id = ?
                AND user_id = ?
                """,
                    batch + batch + [tid, user_id],
                )
                edge_rows.extend(batch_edge_rows)

            edges = [
                Relationship(
                    source_entity_id=row["source_entity_id"],
                    target_entity_id=row["target_entity_id"],
                    relationship_type=cast(RelationshipType, row["relationship_type"]),
                    confidence=row["confidence"],
                    metadata=json.loads(row["metadata"] or "{}"),
                )
                for row in edge_rows
            ]
        else:
            edges = []

        return GraphTraversalResult(nodes=nodes, edges=edges)

    # =========================================================================
    # Memory Integration
    # =========================================================================

    def link_memory_to_entities(
        self,
        memory_id: str,
        entity_ids: list[str],
        user_id: str,
        scores: dict[str, float] | None = None,
        tenant_id: str | None = None,
    ) -> None:
        tid = tenant_id or get_current_tenant_id()
        _validate_input(user_id, tid)
        for entity_id in entity_ids:
            score = scores.get(entity_id, 1.0) if scores else 1.0
            self._execute_sql(
                """
            INSERT OR REPLACE INTO memory_entity_links
            (memory_id, entity_id, tenant_id, user_id, relevance_score, created_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
                (memory_id, entity_id, tid, user_id, score),
            )

    def get_memories_for_entity(
        self,
        entity_id: str,
        user_id: str,
        limit: int = 50,
        tenant_id: str | None = None,
    ) -> list[str]:
        tid = tenant_id or get_current_tenant_id()
        _validate_input(user_id, tid)
        rows = self._fetch_rows(
            """
        SELECT DISTINCT mel.memory_id
        FROM memory_entity_links mel
        WHERE mel.entity_id = ? AND mel.tenant_id = ? AND mel.user_id = ?
        LIMIT ?
        """,
            (entity_id, tid, user_id, limit),
        )
        return [row["memory_id"] for row in rows]

    def find_related_memories(
        self,
        entity_id: str,
        user_id: str,
        depth: int = 2,
        limit: int = 20,
        tenant_id: str | None = None,
    ) -> list[str]:
        tid = tenant_id or get_current_tenant_id()
        _validate_input(user_id, tid)
        rows = self._fetch_rows(
            """
        WITH RECURSIVE connected AS (
            SELECT entity_id, 0 as depth
            FROM memory_entity_links
            WHERE entity_id = ? AND tenant_id = ? AND user_id = ?

            UNION

            SELECT CASE
                WHEN c.entity_id = r.source_entity_id THEN r.target_entity_id
                ELSE r.source_entity_id
            END as entity_id,
            c.depth + 1
            FROM connected c
            JOIN entity_relationships r ON (
                c.entity_id = r.source_entity_id OR c.entity_id = r.target_entity_id
            )
            WHERE c.depth < ?
            AND r.tenant_id = ?
            AND r.user_id = ?
        )
        SELECT DISTINCT mel.memory_id
        FROM memory_entity_links mel
        JOIN connected c ON mel.entity_id = c.entity_id
        WHERE c.depth > 0
        AND mel.tenant_id = ?
        AND mel.user_id = ?
        LIMIT ?
        """,
            (entity_id, tid, user_id, depth, tid, user_id, tid, user_id, limit),
        )
        return [row["memory_id"] for row in rows]

    # =========================================================================
    # Batch Operations
    # =========================================================================

    def process_extraction_result(
        self,
        result: ExtractionResult,
        user_id: str,
        tenant_id: str | None = None,
    ) -> None:
        """Process an extraction result, storing all entities and relationships."""
        for entity in result.entities:
            self.upsert_entity(entity, user_id, tenant_id=tenant_id)

        for relationship in result.relationships:
            self.add_relationship(relationship, user_id, tenant_id=tenant_id)


# Global instance management
_state: dict[str, Any] = {"graph_store": None}
_lock = threading.Lock()


def get_graph_store(db_path: str | None = None, backend: DatabaseBackend | None = None) -> GraphStore:
    with _lock:
        if _state["graph_store"] is None:
            if db_path is None:
                db_path = DB_PATH
            _state["graph_store"] = GraphStore(db_path, backend=backend)
        return _state["graph_store"]


def reset_graph_store() -> None:
    """Reset global graph store (for testing)."""
    with _lock:
        _state["graph_store"] = None
