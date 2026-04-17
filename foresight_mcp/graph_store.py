"""
Graph Store for Entity-Relationship Memory.

SQLite-backed graph storage with:
- Entity CRUD operations
- Relationship management
- Graph traversal using recursive CTEs
- Memory-to-entity linking
"""
from __future__ import annotations
import sqlite3
import json
import logging
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field

from .entity_extractor import Entity, Relationship, ExtractionResult

logger = logging.getLogger("foresight_graph_store")


@dataclass
class GraphTraversalResult:
    """Result of graph traversal."""
    nodes: List[Entity]
    edges: List[Relationship]
    paths: List[Dict[str, Any]] = field(default_factory=list)


class GraphStore:
    """
    SQLite-backed graph store for entities and relationships.

    Supports:
    - Entity CRUD operations
    - Relationship management with confidence tracking
    - Graph traversal with depth limits
    - Memory-to-entity linking
    """

    def __init__(self, db_path: str):
        """
        Initialize graph store.

        Args:
            db_path: Path to SQLite database
        """
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database schema."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            # Entities table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS memory_entities (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    entity_type TEXT NOT NULL
                        CHECK(entity_type IN ('person', 'place', 'concept', 'event', 'emotion', 'object')),
                    description TEXT,
                    properties TEXT DEFAULT '{}',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, name, entity_type)
                )
            """)

            # Relationships table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS entity_relationships (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    source_entity_id TEXT NOT NULL,
                    target_entity_id TEXT NOT NULL,
                    relationship_type TEXT NOT NULL
                        CHECK(relationship_type IN (
                            'mentions', 'located_at', 'experienced', 'caused',
                            'relates_to', 'contradicts', 'supports', 'part_of', 'created'
                        )),
                    confidence REAL DEFAULT 1.0 CHECK(confidence >= 0 AND confidence <= 1),
                    metadata TEXT DEFAULT '{}',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, source_entity_id, target_entity_id, relationship_type)
                )
            """)

            # Memory-to-entity links
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS memory_entity_links (
                    memory_id TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    relevance_score REAL DEFAULT 1.0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (memory_id, entity_id)
                )
            """)

            # Indexes
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_entities_user ON memory_entities(user_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_entities_type ON memory_entities(entity_type)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_entities_name ON memory_entities(name)")

            cursor.execute("CREATE INDEX IF NOT EXISTS idx_rel_source ON entity_relationships(source_entity_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_rel_target ON entity_relationships(target_entity_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_rel_user ON entity_relationships(user_id)")

            cursor.execute("CREATE INDEX IF NOT EXISTS idx_links_memory ON memory_entity_links(memory_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_links_entity ON memory_entity_links(entity_id)")

            conn.commit()
            logger.info("Graph store schema initialized")

        except sqlite3.Error as e:
            conn.rollback()
            logger.error(f"Failed to initialize graph store: {e}")
            raise
        finally:
            conn.close()

    # =========================================================================
    # Entity Operations
    # =========================================================================

    def upsert_entity(self, entity: Entity, user_id: str) -> str:
        """
        Insert or update an entity.

        Args:
            entity: Entity to upsert
            user_id: User ID for ownership

        Returns:
            Entity ID
        """
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute("""
                INSERT INTO memory_entities
                    (id, user_id, name, entity_type, description, properties, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, name, entity_type) DO UPDATE SET
                    description = excluded.description,
                    properties = excluded.properties,
                    updated_at = CURRENT_TIMESTAMP
            """, (
                entity.id,
                user_id,
                entity.name,
                entity.entity_type,
                entity.description,
                json.dumps(entity.properties),
            ))

            conn.commit()
            return entity.id

        finally:
            conn.close()

    def get_entity(self, entity_id: str, user_id: str) -> Optional[Entity]:
        """
        Get an entity by ID.

        Args:
            entity_id: Entity ID
            user_id: User ID

        Returns:
            Entity or None
        """
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute("""
                SELECT id, user_id, name, entity_type, description, properties
                FROM memory_entities
                WHERE id = ? AND user_id = ?
            """, (entity_id, user_id))

            row = cursor.fetchone()
            if not row:
                return None

            return Entity(
                id=row[0],
                name=row[2],
                entity_type=row[3],  # type: ignore
                description=row[4],
                properties=json.loads(row[5] or '{}'),
            )

        finally:
            conn.close()

    def get_entities_by_type(
        self,
        user_id: str,
        entity_type: str,
        limit: int = 100
    ) -> List[Entity]:
        """
        Get all entities of a type for a user.

        Args:
            user_id: User ID
            entity_type: Entity type to filter by
            limit: Max results

        Returns:
            List of entities
        """
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute("""
                SELECT id, user_id, name, entity_type, description, properties
                FROM memory_entities
                WHERE user_id = ? AND entity_type = ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (user_id, entity_type, limit))

            return [
                Entity(
                    id=row[0],
                    name=row[2],
                    entity_type=row[3],  # type: ignore
                    description=row[4],
                    properties=json.loads(row[5] or '{}'),
                )
                for row in cursor.fetchall()
            ]

        finally:
            conn.close()

    def find_entities_by_name(
        self,
        user_id: str,
        name: str,
        limit: int = 10
    ) -> List[Entity]:
        """
        Find entities by name (partial match).

        Args:
            user_id: User ID
            name: Name to search for
            limit: Max results

        Returns:
            List of matching entities
        """
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute("""
                SELECT id, user_id, name, entity_type, description, properties
                FROM memory_entities
                WHERE user_id = ? AND name LIKE ?
                LIMIT ?
            """, (user_id, f'%{name}%', limit))

            return [
                Entity(
                    id=row[0],
                    name=row[2],
                    entity_type=row[3],  # type: ignore
                    description=row[4],
                    properties=json.loads(row[5] or '{}'),
                )
                for row in cursor.fetchall()
            ]

        finally:
            conn.close()

    # =========================================================================
    # Relationship Operations
    # =========================================================================

    def add_relationship(self, relationship: Relationship, user_id: str) -> None:
        """
        Add a relationship between entities.

        Args:
            relationship: Relationship to add
            user_id: User ID for ownership
        """
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                INSERT OR IGNORE INTO entity_relationships
                    (user_id, source_entity_id, target_entity_id, relationship_type, confidence, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                user_id,
                relationship.source_entity_id,
                relationship.target_entity_id,
                relationship.relationship_type,
                relationship.confidence,
                json.dumps(relationship.metadata),
            ))

            conn.commit()

        finally:
            conn.close()

    def get_relationships(
        self,
        entity_id: str,
        user_id: str,
        direction: str = 'both'
    ) -> List[Relationship]:
        """
        Get relationships for an entity.

        Args:
            entity_id: Entity ID
            user_id: User ID
            direction: 'in', 'out', or 'both'

        Returns:
            List of relationships
        """
        conn = sqlite3.connect(self.db_path)
        try:
            if direction == 'out':
                cursor = conn.execute("""
                    SELECT source_entity_id, target_entity_id, relationship_type, confidence, metadata
                    FROM entity_relationships
                    WHERE source_entity_id = ? AND user_id = ?
                """, (entity_id, user_id))
            elif direction == 'in':
                cursor = conn.execute("""
                    SELECT source_entity_id, target_entity_id, relationship_type, confidence, metadata
                    FROM entity_relationships
                    WHERE target_entity_id = ? AND user_id = ?
                """, (entity_id, user_id))
            else:  # both
                cursor = conn.execute("""
                    SELECT source_entity_id, target_entity_id, relationship_type, confidence, metadata
                    FROM entity_relationships
                    WHERE (source_entity_id = ? OR target_entity_id = ?) AND user_id = ?
                """, (entity_id, entity_id, user_id))

            return [
                Relationship(
                    source_entity_id=row[0],
                    target_entity_id=row[1],
                    relationship_type=row[2],  # type: ignore
                    confidence=row[3],
                    metadata=json.loads(row[4] or '{}'),
                )
                for row in cursor.fetchall()
            ]

        finally:
            conn.close()

    # =========================================================================
    # Graph Traversal
    # =========================================================================

    def traverse_graph(
        self,
        start_entity_id: str,
        user_id: str,
        max_depth: int = 2,
        max_results: int = 50,
        relationship_types: Optional[List[str]] = None
    ) -> GraphTraversalResult:
        """
        Traverse graph from a starting entity.

        Uses recursive CTE for graph traversal.

        Args:
            start_entity_id: Starting entity ID
            user_id: User ID
            max_depth: Maximum traversal depth
            max_results: Maximum results
            relationship_types: Optional filter for relationship types

        Returns:
            GraphTraversalResult with nodes and edges
        """
        conn = sqlite3.connect(self.db_path)
        try:
            type_filter = ""
            type_params = []
            if relationship_types:
                placeholders = ','.join('?' * len(relationship_types))
                type_filter = f"AND r.relationship_type IN ({placeholders})"
                type_params = relationship_types

            # Recursive CTE for graph traversal
            cursor = conn.execute(f"""
                WITH RECURSIVE graph_traversal AS (
                    -- Base case: starting node
                    SELECT
                        id as entity_id,
                        entity_type,
                        name,
                        description,
                        properties,
                        0 as depth
                    FROM memory_entities
                    WHERE id = ? AND user_id = ?

                    UNION ALL

                    -- Recursive case: traverse relationships
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
                        {type_filter}
                    )
                    JOIN memory_entities e ON e.id = CASE
                        WHEN gt.entity_id = r.source_entity_id THEN r.target_entity_id
                        ELSE r.source_entity_id
                    END
                    WHERE gt.depth < ?
                      AND e.user_id = ?
                )
                SELECT DISTINCT entity_id, entity_type, name, description, properties, depth
                FROM graph_traversal
                WHERE depth > 0
                LIMIT ?
            """, [start_entity_id, user_id, max_depth, user_id, max_results] + type_params)

            nodes = [
                Entity(
                    id=row[0],
                    name=row[2],
                    entity_type=row[1],  # type: ignore
                    description=row[3],
                    properties=json.loads(row[4] or '{}'),
                )
                for row in cursor.fetchall()
            ]

            # Get relationships between traversed nodes
            node_ids = [n.id for n in nodes]
            if node_ids:
                placeholders = ','.join('?' * len(node_ids))
                cursor = conn.execute(f"""
                    SELECT source_entity_id, target_entity_id, relationship_type, confidence, metadata
                    FROM entity_relationships
                    WHERE source_entity_id IN ({placeholders})
                      AND target_entity_id IN ({placeholders})
                      AND user_id = ?
                """, node_ids + node_ids + [user_id])

                edges = [
                    Relationship(
                        source_entity_id=row[0],
                        target_entity_id=row[1],
                        relationship_type=row[2],  # type: ignore
                        confidence=row[3],
                        metadata=json.loads(row[4] or '{}'),
                    )
                    for row in cursor.fetchall()
                ]
            else:
                edges = []

            return GraphTraversalResult(nodes=nodes, edges=edges)

        finally:
            conn.close()

    # =========================================================================
    # Memory Integration
    # =========================================================================

    def link_memory_to_entities(
        self,
        memory_id: str,
        entity_ids: List[str],
        user_id: str,
        scores: Optional[Dict[str, float]] = None
    ) -> None:
        """
        Link a memory to entities.

        Args:
            memory_id: Memory ID
            entity_ids: Entity IDs to link
            user_id: User ID
            scores: Optional relevance scores per entity
        """
        conn = sqlite3.connect(self.db_path)
        try:
            for entity_id in entity_ids:
                score = scores.get(entity_id, 1.0) if scores else 1.0
                conn.execute("""
                    INSERT OR REPLACE INTO memory_entity_links
                        (memory_id, entity_id, relevance_score, created_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                """, (memory_id, entity_id, score))

            conn.commit()

        finally:
            conn.close()

    def get_memories_for_entity(
        self,
        entity_id: str,
        user_id: str,
        limit: int = 50
    ) -> List[str]:
        """
        Get memory IDs linked to an entity.

        Args:
            entity_id: Entity ID
            user_id: User ID
            limit: Max results

        Returns:
            List of memory IDs
        """
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute("""
                SELECT DISTINCT mel.memory_id
                FROM memory_entity_links mel
                WHERE mel.entity_id = ?
                LIMIT ?
            """, (entity_id, limit))

            return [row[0] for row in cursor.fetchall()]

        finally:
            conn.close()

    def find_related_memories(
        self,
        entity_id: str,
        user_id: str,
        depth: int = 2,
        limit: int = 20
    ) -> List[str]:
        """
        Find memories related to an entity via graph traversal.

        Args:
            entity_id: Starting entity ID
            user_id: User ID
            depth: Traversal depth
            limit: Max results

        Returns:
            List of memory IDs
        """
        conn = sqlite3.connect(self.db_path)
        try:
            # Get connected entities up to specified depth
            cursor = conn.execute("""
                WITH RECURSIVE connected AS (
                    SELECT entity_id, 0 as depth
                    FROM memory_entity_links
                    WHERE entity_id = ?

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
                )
                SELECT DISTINCT mel.memory_id
                FROM memory_entity_links mel
                JOIN connected c ON mel.entity_id = c.entity_id
                WHERE c.depth > 0
                LIMIT ?
            """, (entity_id, depth, limit))

            return [row[0] for row in cursor.fetchall()]

        finally:
            conn.close()

    # =========================================================================
    # Batch Operations
    # =========================================================================

    def process_extraction_result(
        self,
        result: ExtractionResult,
        user_id: str
    ) -> None:
        """
        Process an extraction result, storing all entities and relationships.

        Args:
            result: Extraction result to process
            user_id: User ID for ownership
        """
        for entity in result.entities:
            self.upsert_entity(entity, user_id)

        for relationship in result.relationships:
            self.add_relationship(relationship, user_id)


# Global instance management
_graph_store: Optional[GraphStore] = None


def get_graph_store(db_path: Optional[str] = None) -> GraphStore:
    """Get or create global graph store instance."""
    global _graph_store
    if _graph_store is None:
        if db_path is None:
            from .server import DB_PATH
            db_path = DB_PATH
        _graph_store = GraphStore(db_path)
    return _graph_store


def reset_graph_store() -> None:
    """Reset global graph store (for testing)."""
    global _graph_store
    _graph_store = None
