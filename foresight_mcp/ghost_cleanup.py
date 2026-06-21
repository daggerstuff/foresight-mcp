"""Ghost Memory Cleanup Service.

Provides automated cleanup of ghost (archived) memories to prevent
resource exhaustion from accumulated archived data.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import DB_PATH
from .connection_pool import get_pool

logger = logging.getLogger("foresight_ghost_cleanup")


@dataclass
class CleanupStats:
    """Statistics from a cleanup run."""

    ghost_memories_found: int = 0
    ghost_memories_deleted: int = 0
    bytes_freed: int = 0
    cleanup_duration_seconds: float = 0.0
    oldest_ghost_deleted: str | None = None


class GhostMemoryCleanup:
    """
    Service for cleaning up ghost (archived) memories.

    Ghost memories are archived memories where content is redacted
    but gist is preserved. Over time, these can accumulate and
    consume database resources.

    This service provides:
    - Periodic cleanup of old ghost memories
    - Configurable TTL for ghost retention
    - Cleanup statistics and monitoring
    """

    DEFAULT_GHOST_TTL_DAYS = 90  # Default: keep ghosts for 3 months

    def __init__(self, db_path: str, ghost_ttl_days: int | None = None):
        """Initialize ghost cleanup service.

        Args:
            db_path: Path to SQLite database
            ghost_ttl_days: Days to retain ghost memories (default: 90)
        """
        self.db_path = db_path
        self.ghost_ttl_days = ghost_ttl_days or self.DEFAULT_GHOST_TTL_DAYS

    def cleanup_old_ghosts(
        self,
        tenant_id: str = "default",
        max_batch_size: int = 1000,
    ) -> CleanupStats:
        """
        Delete ghost memories older than the TTL.

        Args:
            tenant_id: Tenant ID for scoped cleanup
            max_batch_size: Maximum ghosts to delete in one batch

        Returns:
            CleanupStats with deletion statistics
        """
        start_time = datetime.now(timezone.utc)
        stats = CleanupStats()

        cutoff_date = start_time - timedelta(days=self.ghost_ttl_days)

        pool = get_pool(self.db_path)
        conn = pool.acquire()

        try:
            # Count ghosts older than TTL
            cursor = conn.execute(
                """
                SELECT COUNT(*) as count,
                       MIN(created_at) as oldest
                FROM memories
                WHERE tenant_id = ? AND is_ghost = 1
                AND created_at < ?
                """,
                (tenant_id, cutoff_date.isoformat()),
            )
            row = cursor.fetchone()
            stats.ghost_memories_found = row[0] if row else 0
            if row and row[1]:
                stats.oldest_ghost_deleted = row[1]

            if stats.ghost_memories_found == 0:
                logger.info("No ghost memories found for cleanup")
                return stats

            # Estimate bytes freed (approximate based on content length)
            cursor = conn.execute(
                """
                SELECT SUM(LENGTH(content)) as total_bytes
                FROM memories
                WHERE tenant_id = ? AND is_ghost = 1
                AND created_at < ?
                """,
                (tenant_id, cutoff_date.isoformat()),
            )
            row = cursor.fetchone()
            stats.bytes_freed = row[0] if row and row[0] else 0

            # Delete in batches
            deleted = 0
            while deleted < stats.ghost_memories_found:
                cursor = conn.execute(
                    """
                    DELETE FROM memories
                    WHERE tenant_id = ? AND is_ghost = 1
                    AND created_at < ?
                    LIMIT ?
                    """,
                    (tenant_id, cutoff_date.isoformat(), max_batch_size),
                )
                batch_deleted = cursor.rowcount
                deleted += batch_deleted

                if batch_deleted < max_batch_size:
                    break

            conn.commit()
            stats.ghost_memories_deleted = deleted

            # Also clean up orphaned entity links
            cursor = conn.execute(
                """
                DELETE FROM memory_entity_links
                WHERE memory_id IN (
                    SELECT id FROM memories
                    WHERE tenant_id = ? AND is_ghost = 1
                    AND created_at < ?
                )
                """,
                (tenant_id, cutoff_date.isoformat()),
            )
            orphaned_links = cursor.rowcount
            if orphaned_links > 0:
                logger.info(f"Cleaned up {orphaned_links} orphaned entity links")
                conn.commit()

        finally:
            pool.release(conn)

        end_time = datetime.now(timezone.utc)
        stats.cleanup_duration_seconds = (end_time - start_time).total_seconds()

        logger.info(
            f"Ghost cleanup complete: deleted {stats.ghost_memories_deleted}/"
            f"{stats.ghost_memories_found} ghosts, "
            f"freed ~{stats.bytes_freed} bytes in "
            f"{stats.cleanup_duration_seconds:.2f}s"
        )

        return stats

    def get_ghost_statistics(
        self,
        tenant_id: str = "default",
    ) -> dict:
        """
        Get statistics about ghost memories.

        Args:
            tenant_id: Tenant ID for scoped stats

        Returns:
            Dictionary with ghost memory statistics
        """
        pool = get_pool(self.db_path)
        conn = pool.acquire()

        try:
            # Total ghosts
            cursor = conn.execute(
                """
                SELECT COUNT(*) as count,
                       MIN(created_at) as oldest,
                       MAX(created_at) as newest,
                       SUM(LENGTH(content)) as total_bytes
                FROM memories
                WHERE tenant_id = ? AND is_ghost = 1
                """,
                (tenant_id,),
            )
            row = cursor.fetchone()

            # Ghosts by age bucket
            cursor = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN created_at > datetime('now', '-7 days') THEN 1 ELSE 0 END) as last_7_days,
                    SUM(CASE WHEN created_at > datetime('now', '-30 days') THEN 1 ELSE 0 END) as last_30_days,
                    SUM(CASE WHEN created_at > datetime('now', '-90 days') THEN 1 ELSE 0 END) as last_90_days,
                    SUM(CASE WHEN created_at < datetime('now', '-90 days') THEN 1 ELSE 0 END) as older_90_days
                FROM memories
                WHERE tenant_id = ? AND is_ghost = 1
                """,
                (tenant_id,),
            )
            age_row = cursor.fetchone()

            return {
                "total_ghosts": row[0] if row else 0,
                "oldest_ghost": row[1] if row else None,
                "newest_ghost": row[2] if row else None,
                "total_bytes": row[3] if row and row[3] else 0,
                "age_distribution": {
                    "last_7_days": age_row[0] if age_row else 0,
                    "last_30_days": age_row[1] if age_row else 0,
                    "last_90_days": age_row[2] if age_row else 0,
                    "older_90_days": age_row[3] if age_row else 0,
                },
                "ttl_days": self.ghost_ttl_days,
            }

        finally:
            pool.release(conn)


# Global cleanup instance
class _GhostCleanupSingleton:
    """Module-level singleton for GhostMemoryCleanup."""

    _instance: GhostMemoryCleanup | None = None
    _lock = __import__("threading").Lock()

    @classmethod
    def get_instance(cls, db_path: str | None = None, ghost_ttl_days: int | None = None) -> GhostMemoryCleanup:
        """Get or create global ghost cleanup instance."""
        with cls._lock:
            if cls._instance is None:
                if db_path is None:
                    db_path = DB_PATH
                cls._instance = GhostMemoryCleanup(db_path, ghost_ttl_days)
            return cls._instance


def get_ghost_cleanup(
    db_path: str | None = None,
    ghost_ttl_days: int | None = None,
) -> GhostMemoryCleanup:
    """Get or create global ghost cleanup instance."""
    return _GhostCleanupSingleton.get_instance(db_path, ghost_ttl_days)


def run_ghost_cleanup(tenant_id: str = "default") -> CleanupStats:
    """Run ghost cleanup with default settings."""
    return get_ghost_cleanup().cleanup_old_ghosts(tenant_id)
