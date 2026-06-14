"""Graph Edge Temporal Decay Service.

Implements time-based decay for graph relationships to prevent
the graph from becoming stale with outdated connections.

Decay model:
- Edges decay exponentially based on time since last access
- decay_factor = 0.5 ^ (hours_elapsed / half_life_hours)
- Stale edges (below threshold) can be auto-pruned
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import DB_PATH
from .connection_pool import get_pool

logger = logging.getLogger("foresight_graph_decay")


@dataclass
class EdgeDecayStats:
    """Statistics from edge decay calculation."""

    edges_processed: int = 0
    edges_updated: int = 0
    edges_pruned: int = 0
    avg_decay_factor: float = 0.0


class GraphEdgeDecay:
    """
    Service for managing temporal decay of graph edges.

    Graph relationships can become stale over time if not
    actively used. This service:
    - Applies exponential decay to edge confidence scores
    - Updates last_accessed timestamps on access
    - Prunes edges below confidence threshold
    """

    DEFAULT_HALF_LIFE_HOURS = 168.0  # 1 week
    DEFAULT_PRUNE_THRESHOLD = 0.1  # Remove edges below 10% confidence
    DEFAULT_DECAY_UPDATE_BATCH = 1000

    def __init__(
        self,
        db_path: str,
        half_life_hours: float | None = None,
        prune_threshold: float | None = None,
    ):
        """Initialize graph edge decay service.

        Args:
            db_path: Path to SQLite database
            half_life_hours: Hours for 50% decay (default: 168 = 1 week)
            prune_threshold: Confidence threshold for pruning (default: 0.1)
        """
        self.db_path = db_path
        self.half_life_hours = half_life_hours or self.DEFAULT_HALF_LIFE_HOURS
        self.prune_threshold = prune_threshold or self.DEFAULT_PRUNE_THRESHOLD

    def calculate_decay_factor(self, last_accessed: str) -> float:
        """
        Calculate decay factor based on time since last access.

        Uses exponential decay: factor = 0.5 ^ (hours_elapsed / half_life)

        Args:
            last_accessed: ISO format timestamp of last access

        Returns:
            Decay factor between 0 and 1
        """
        last = datetime.fromisoformat(last_accessed.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        hours_elapsed = (now - last).total_seconds() / 3600

        # Exponential decay: 0.5 ^ (hours / half_life)
        decay_factor = pow(0.5, hours_elapsed / self.half_life_hours)
        return max(0.0, min(1.0, decay_factor))

    def update_edge_decay(
        self,
        tenant_id: str = "default",
        _batch_size: int = 1000,
    ) -> EdgeDecayStats:
        """
        Update decay factors for all edges.

        Args:
            tenant_id: Tenant ID for scoped update
            batch_size: Number of edges to process per batch

        Returns:
            EdgeDecayStats with update statistics
        """
        stats = EdgeDecayStats()

        pool = get_pool(self.db_path)
        conn = pool.acquire()

        try:
            # Update decay factors using SQL calculation per row
            # decay_factor = 0.5 ^ (hours_elapsed / half_life)
            cursor = conn.execute(
                """
                UPDATE entity_relationships
                SET decay_factor = MAX(0.0, MIN(1.0,
                    POWER(0.5,
                        (JULIANDAY('now') - JULIANDAY(last_accessed)) * 24.0 / ?
                    )
                ))
                WHERE tenant_id = ?
                """,
                (self.half_life_hours, tenant_id),
            )
            stats.edges_updated = cursor.rowcount
            stats.edges_processed = stats.edges_updated

            conn.commit()

            # Calculate average decay factor
            cursor = conn.execute(
                """
                SELECT AVG(decay_factor)
                FROM entity_relationships
                WHERE tenant_id = ?
                """,
                (tenant_id,),
            )
            row = cursor.fetchone()
            stats.avg_decay_factor = row[0] if row and row[0] else 1.0

        finally:
            pool.release(conn)

        logger.info(
            f"Edge decay update: processed {stats.edges_processed} edges, "
            f"avg decay factor: {stats.avg_decay_factor:.3f}"
        )

        return stats

    def prune_stale_edges(
        self,
        tenant_id: str = "default",
        max_prune_count: int = 1000,
    ) -> EdgeDecayStats:
        """
        Remove edges with decayed confidence below threshold.

        Args:
            tenant_id: Tenant ID for scoped pruning
            max_prune_count: Maximum edges to prune in one run

        Returns:
            EdgeDecayStats with prune statistics
        """
        stats = EdgeDecayStats()

        pool = get_pool(self.db_path)
        conn = pool.acquire()

        try:
            # Count edges below threshold
            cursor = conn.execute(
                """
                SELECT COUNT(*)
                FROM entity_relationships
                WHERE tenant_id = ?
                AND (decay_factor * confidence) < ?
                """,
                (tenant_id, self.prune_threshold),
            )
            stats.edges_processed = cursor.fetchone()[0]

            # Prune in batch
            cursor = conn.execute(
                """
                DELETE FROM entity_relationships
                WHERE tenant_id = ?
                AND (decay_factor * confidence) < ?
                LIMIT ?
                """,
                (tenant_id, self.prune_threshold, max_prune_count),
            )
            stats.edges_pruned = cursor.rowcount

            conn.commit()

        finally:
            pool.release(conn)

        if stats.edges_pruned > 0:
            logger.info(f"Pruned {stats.edges_pruned} stale edges (threshold: {self.prune_threshold})")

        return stats

    def update_edge_on_access(
        self,
        edge_id: int,
        tenant_id: str = "default",
    ) -> None:
        """
        Update last_accessed timestamp when edge is traversed.

        This prevents frequently-used edges from decaying.

        Args:
            edge_id: Edge ID to update
            tenant_id: Tenant ID for scoping
        """
        pool = get_pool(self.db_path)
        conn = pool.acquire()

        try:
            conn.execute(
                """
                UPDATE entity_relationships
                SET last_accessed = ?
                WHERE id = ? AND tenant_id = ?
                """,
                (datetime.now(timezone.utc).isoformat(), edge_id, tenant_id),
            )
            conn.commit()
        finally:
            pool.release(conn)

    def get_edge_effective_confidence(
        self,
        edge_id: int,
        tenant_id: str = "default",
    ) -> float | None:
        """
        Get effective confidence (confidence * decay_factor) for an edge.

        Args:
            edge_id: Edge ID
            tenant_id: Tenant ID for scoping

        Returns:
            Effective confidence or None if edge not found
        """
        pool = get_pool(self.db_path)
        conn = pool.acquire()

        try:
            cursor = conn.execute(
                """
                SELECT confidence * decay_factor as effective_confidence
                FROM entity_relationships
                WHERE id = ? AND tenant_id = ?
                """,
                (edge_id, tenant_id),
            )
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            pool.release(conn)


# Global instance
class _GraphEdgeDecaySingleton:
    """Module-level singleton for GraphEdgeDecay."""

    _instance: GraphEdgeDecay | None = None
    _lock = __import__("threading").RLock()

    @classmethod
    def get_instance(cls, db_path: str | None = None, half_life_hours: float | None = None) -> GraphEdgeDecay:
        """Get or create global graph edge decay service."""
        with cls._lock:
            if cls._instance is None:
                if db_path is None:
                    db_path = DB_PATH
                cls._instance = GraphEdgeDecay(db_path, half_life_hours)
            return cls._instance


def get_graph_edge_decay(
    db_path: str | None = None,
    half_life_hours: float | None = None,
) -> GraphEdgeDecay:
    """Get or create global graph edge decay service."""
    return _GraphEdgeDecaySingleton.get_instance(db_path, half_life_hours)


def run_edge_decay_update(tenant_id: str = "default") -> EdgeDecayStats:
    """Run edge decay update with default settings."""
    return get_graph_edge_decay().update_edge_decay(tenant_id)


def run_edge_pruning(tenant_id: str = "default") -> EdgeDecayStats:
    """Run edge pruning with default settings."""
    return get_graph_edge_decay().prune_stale_edges(tenant_id)
