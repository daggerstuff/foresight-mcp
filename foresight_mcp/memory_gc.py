"""Lightweight Memory GC Service.

Provides periodic garbage collection for memory system resources:
- Deletes memories past retention policy TTL (ephemeral >24h, short_term >7d)
- Prunes old memory_decay_events log (>30d)
- Prunes old maintenance events (>60d)
- Cleans orphaned entity links and embeddings (no FK cascade to protect them)

This is intentionally lightweight — no ML, no synthesis, just SQL batched deletes.
Designed to run frequently alongside heavy maintenance operations.
"""

import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone

from .config import DB_PATH
from .connection_pool import get_pool

logger = logging.getLogger("foresight_memory_gc")

# Retention TTLs in hours
DEFAULT_RETENTION_TTLS: dict[str, int] = {
    "ephemeral": 24,
    "short_term": 168,  # 7 days
}


@dataclass
class GCConfig:
    """Configuration for memory garbage collection."""

    retention_ttls: dict[str, int] | None = None
    decay_events_retention_days: int = 30
    maintenance_events_retention_days: int = 60
    max_batch_size: int = 500

    def to_dict(self) -> dict:
        return {
            "retention_ttls": dict(self.retention_ttls) if self.retention_ttls else dict(DEFAULT_RETENTION_TTLS),
            "decay_events_retention_days": self.decay_events_retention_days,
            "maintenance_events_retention_days": self.maintenance_events_retention_days,
            "max_batch_size": self.max_batch_size,
        }


@dataclass
class GCStats:
    """Statistics from a GC run."""

    expired_memories_found: int = 0
    expired_memories_deleted: int = 0
    decay_events_pruned: int = 0
    maintenance_events_pruned: int = 0
    orphan_links_cleaned: int = 0
    orphan_embeddings_cleaned: int = 0
    gc_duration_seconds: float = 0.0
    bytes_freed: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class MemoryGC:
    """
    Lightweight memory garbage collector.

    Performs cheap, frequent cleanup operations:
    1. Delete memories past retention TTL (ephemeral >24h, short_term >7d)
    2. Prune old memory_decay_events log
    3. Prune old maintenance events from events table
    4. Clean orphaned memory_entity_links and memory_embeddings
    """

    def __init__(self, db_path: str):
        self.db_path = db_path

    def run(
        self,
        tenant_id: str = "default",
        config: GCConfig | None = None,
    ) -> GCStats:
        """Run garbage collection across all phases.

        Args:
            tenant_id: Tenant ID for scoped cleanup
            config: GC configuration (uses defaults if None)

        Returns:
            GCStats with per-phase deletion counts
        """
        start_time = datetime.now(timezone.utc)
        cfg = config or GCConfig()
        ttls = cfg.retention_ttls if cfg.retention_ttls is not None else DEFAULT_RETENTION_TTLS
        stats = GCStats()

        pool = get_pool(self.db_path)
        conn = pool.acquire()

        try:
            # ------------------------------------------------------------------
            # Phase 1: Delete expired memories by retention policy
            # ------------------------------------------------------------------
            for retention, hours in ttls.items():
                cutoff = (start_time - timedelta(hours=hours)).isoformat()

                # Count found + estimate bytes
                cursor = conn.execute(
                    """
                    SELECT COUNT(*), COALESCE(SUM(LENGTH(content)), 0)
                    FROM memories
                    WHERE tenant_id = ? AND retention = ? AND created_at < ?
                    """,
                    (tenant_id, retention, cutoff),
                )
                row = cursor.fetchone()
                count_found = row[0] if row else 0
                bytes_est = row[1] if row else 0
                stats.expired_memories_found += count_found
                stats.bytes_freed += bytes_est

                if count_found == 0:
                    continue

                deleted = 0
                while deleted < count_found:
                    cursor = conn.execute(
                        """
                        DELETE FROM memories WHERE rowid IN (
                            SELECT rowid FROM memories
                            WHERE tenant_id = ? AND retention = ? AND created_at < ?
                            LIMIT ?
                        )
                        """,
                        (tenant_id, retention, cutoff, cfg.max_batch_size),
                    )
                    batch = cursor.rowcount
                    deleted += batch
                    if batch == 0:
                        break

                stats.expired_memories_deleted += deleted

            conn.commit()

            # ------------------------------------------------------------------
            # Phase 2: Prune old memory_decay_events
            # ------------------------------------------------------------------
            decay_cutoff = (start_time - timedelta(days=cfg.decay_events_retention_days)).isoformat()
            cursor = conn.execute(
                """
                DELETE FROM memory_decay_events
                WHERE created_at < ?
                """,
                (decay_cutoff,),
            )
            stats.decay_events_pruned = cursor.rowcount
            conn.commit()

            # ------------------------------------------------------------------
            # Phase 3: Prune old maintenance events
            # ------------------------------------------------------------------
            maint_cutoff = (start_time - timedelta(days=cfg.maintenance_events_retention_days)).isoformat()
            cursor = conn.execute(
                """
                DELETE FROM events
                WHERE event_type LIKE 'maintenance%' AND timestamp < ?
                """,
                (maint_cutoff,),
            )
            stats.maintenance_events_pruned = cursor.rowcount
            conn.commit()

            # ------------------------------------------------------------------
            # Phase 4: Clean orphaned data (no FK cascade protection)
            # ------------------------------------------------------------------

            # 4a: Orphaned memory_entity_links
            cursor = conn.execute(
                """
                DELETE FROM memory_entity_links
                WHERE memory_id NOT IN (SELECT id FROM memories)
                """,
            )
            stats.orphan_links_cleaned = cursor.rowcount
            conn.commit()

            # 4b: Orphaned memory_embeddings
            cursor = conn.execute(
                """
                DELETE FROM memory_embeddings
                WHERE memory_id NOT IN (SELECT id FROM memories)
                """,
            )
            stats.orphan_embeddings_cleaned = cursor.rowcount
            conn.commit()

        finally:
            pool.release(conn)

        stats.gc_duration_seconds = (datetime.now(timezone.utc) - start_time).total_seconds()

        logger.info(
            f"GC complete: deleted {stats.expired_memories_deleted}/{stats.expired_memories_found} "
            f"expired memories (~{stats.bytes_freed} bytes), "
            f"pruned {stats.decay_events_pruned} decay events, "
            f"{stats.maintenance_events_pruned} maintenance events, "
            f"{stats.orphan_links_cleaned} orphan links, "
            f"{stats.orphan_embeddings_cleaned} orphan embeddings "
            f"in {stats.gc_duration_seconds:.2f}s"
        )

        return stats


# ---------------------------------------------------------------------------
# Singleton pattern (following ghost_cleanup.py)
# ---------------------------------------------------------------------------


class _MemoryGCSingleton:
    """Module-level singleton for MemoryGC."""

    _instance: MemoryGC | None = None
    _lock = __import__("threading").Lock()

    @classmethod
    def get_instance(cls, db_path: str | None = None) -> MemoryGC:
        """Get or create global memory GC instance."""
        with cls._lock:
            if cls._instance is None:
                if db_path is None:
                    db_path = DB_PATH
                cls._instance = MemoryGC(db_path)
            return cls._instance


def get_memory_gc(db_path: str | None = None) -> MemoryGC:
    """Get or create global memory GC instance."""
    return _MemoryGCSingleton.get_instance(db_path)


def run_memory_gc(tenant_id: str = "default", config: GCConfig | None = None) -> GCStats:
    """Run memory GC with default settings."""
    return get_memory_gc().run(tenant_id, config)
