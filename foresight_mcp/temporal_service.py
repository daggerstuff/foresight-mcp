"""Temporal Service - Decay algorithms and freshness trend tracking.

Implements:
- Exponential decay based on Ebbinghaus forgetting curve
- Category-based half-life multipliers
- Real-time trend calculation on memory access
- Batch decay update service for periodic recalculation
"""

from __future__ import annotations

import logging

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from .connection_pool import get_pool

logger = logging.getLogger("foresight_temporal")


def _is_missing_tenant_column_error(error: Exception) -> bool:
    message = str(error).lower()
    return "no such column: tenant_id" in message or "no column named tenant_id" in message


FreshnessTrend = Literal["stable", "strengthening", "weakening", "stale"]


@dataclass
class DecayConfig:
    """Configuration for memory decay calculations."""

    half_life_hours: float = 168.0  # 1 week default
    min_importance: float = 0.1
    activation_boost: float = 1.2
    strengthening_threshold: int = 5  # activations needed for 'strengthening'
    stale_threshold: float = 0.2
    category_multiplier: float = 1.0

    @classmethod
    def from_db_row(cls, row: tuple) -> "DecayConfig":
        """Create DecayConfig from database row."""
        return cls(
            half_life_hours=row[2],
            min_importance=row[3],
            activation_boost=row[4],
            strengthening_threshold=row[5],
            stale_threshold=row[6],
        )


class TemporalService:
    """
    Service for managing temporal aspects of memories.

    Handles:
    - Decay calculations (exponential based on Ebbinghaus curve)
    - Freshness trend tracking
    - Activation counting on memory access
    - Batch decay updates
    """

    def __init__(self, db_path: str):
        """Initialize temporal service."""
        self.db_path = db_path

    def _get_decay_config(self, user_id: str, category: str = "general", tenant_id: str = "default") -> DecayConfig:
        """Get decay configuration for user/category."""
        pool = get_pool(self.db_path)
        conn = pool.acquire()
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(decay_config)").fetchall()]
            has_tenant_id = "tenant_id" in cols

            if has_tenant_id:
                cursor = conn.execute(
                    """
                    SELECT user_id, category, half_life_hours, min_importance,
                           activation_boost, strengthening_threshold, stale_threshold
                    FROM decay_config
                    WHERE user_id = ? AND category = ? AND tenant_id = ?
                """,
                    (user_id, category, tenant_id),
                )
            else:
                # Backward compatibility for pre-tenant schemas (e.g., test fixtures)
                cursor = conn.execute(
                    """
                    SELECT user_id, category, half_life_hours, min_importance,
                           activation_boost, strengthening_threshold, stale_threshold
                    FROM decay_config
                    WHERE user_id = ? AND category = ?
                """,
                    (user_id, category),
                )

            row = cursor.fetchone()
            if row:
                return DecayConfig.from_db_row(row)

            # Fall back to default
            return DecayConfig()
        finally:
            pool.release(conn)

    def calculate_decay(
        self,
        importance: float,
        created_at: str,
        activation_count: int,
        category: str = "general",
        user_id: str = "default",
    ) -> tuple[float, FreshnessTrend]:
        """
        Calculate decay for a memory.

        Uses exponential decay based on Ebbinghaus forgetting curve:
        I(t) = I0 * (0.5)^(t / half_life)

        Args:
            importance: Current importance value
            created_at: ISO format timestamp when memory was created
            activation_count: Number of times memory has been accessed
            category: Memory category for half-life multiplier
            user_id: User ID for config lookup

        Returns:
            Tuple of (new_importance, freshness_trend)
        """
        config = self._get_decay_config(user_id, category)

        # Calculate hours elapsed
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        hours_elapsed = (now - created).total_seconds() / 3600

        # Apply category multiplier to half-life
        effective_half_life = config.half_life_hours * config.category_multiplier

        # Exponential decay: I(t) = I0 * (0.5)^(t / half_life)
        decay_factor = pow(0.5, hours_elapsed / effective_half_life)
        new_importance = max(config.min_importance, importance * decay_factor)

        # Calculate trend
        trend = self._calculate_trend(new_importance, activation_count, hours_elapsed, config)

        return new_importance, trend

    def _calculate_trend(
        self, importance: float, activation_count: int, hours_since_creation: float, config: DecayConfig
    ) -> FreshnessTrend:
        """
        Calculate freshness trend based on activation and importance.

        Trends:
        - strengthening: Frequent activation (>= threshold)
        - weakening: Not accessed recently relative to half-life
        - stale: Below importance threshold
        - stable: Normal decay, no significant activity
        """
        # Stale: Below threshold
        if importance <= config.stale_threshold:
            return "stale"

        # Strengthening: Frequent activation
        if activation_count >= config.strengthening_threshold:
            return "strengthening"

        # Weakening: Not accessed recently (use half-life as reference)
        # If memory hasn't been activated much and is decaying normally
        if activation_count < 2 and hours_since_creation > config.half_life_hours * 1.0:
            return "weakening"

        return "stable"

    def on_memory_retrieved(
        self,
        memory_id: str,
        user_id: str,
        importance: float = 1.0,
        activation_boost: float | None = None,
        tenant_id: str = "default",
    ) -> tuple[float, FreshnessTrend]:
        """
        Call when a memory is retrieved/accessed.

        Updates:
        - accessed_at timestamp
        - activation_count
        - retrieval_count
        - importance (with boost)
        - strength_trend

        Args:
            memory_id: Memory ID
            user_id: User ID
            importance: Current importance value
            activation_boost: Boost multiplier (uses config default if None)

        Returns:
            Tuple of (new_importance, new_trend)
        """
        pool = get_pool(self.db_path)
        conn = pool.acquire()
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            # Get current memory data
            try:
                cursor = conn.execute(
                    """
                    SELECT importance, activation_count, created_at, category
                    FROM memories WHERE id = ? AND user_id = ? AND tenant_id = ?
                """,
                    (memory_id, user_id, tenant_id),
                )
            except Exception as e:
                if _is_missing_tenant_column_error(e):
                    cursor = conn.execute(
                        """
                        SELECT importance, activation_count, created_at, category
                        FROM memories WHERE id = ? AND user_id = ?
                    """,
                        (memory_id, user_id),
                    )
                else:
                    raise

            row = cursor.fetchone()
            if not row:
                logger.warning(f"Memory {memory_id} not found for retrieval update")
                return importance, "stable"

            current_importance, activation_count, created_at, category = row

            # Get config for boost
            config = self._get_decay_config(user_id, category or "general", tenant_id)
            boost = activation_boost or config.activation_boost

            # Boost importance
            new_importance = min(1.0, current_importance * boost)

            # Increment counters
            new_activation_count = activation_count + 1

            # Calculate trend
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            hours_elapsed = (now - created).total_seconds() / 3600

            trend = self._calculate_trend(new_importance, new_activation_count, hours_elapsed, config)

            # Update database — capture now once to avoid two syscalls
            now_iso = now.isoformat()
            try:
                conn.execute(
                    """
                    UPDATE memories
                    SET accessed_at = ?,
                        activation_count = ?,
                        retrieval_count = retrieval_count + 1,
                        importance = MAX(?, ?),
                        strength_trend = ?,
                        last_retrieved_at = ?
                    WHERE id = ? AND user_id = ? AND tenant_id = ?
                """,
                    (
                        now_iso,
                        new_activation_count,
                        new_importance,
                        config.min_importance,
                        trend,
                        now_iso,
                        memory_id,
                        user_id,
                        tenant_id,
                    ),
                )
            except Exception as e:
                if _is_missing_tenant_column_error(e):
                    conn.execute(
                        """
                        UPDATE memories
                        SET accessed_at = ?,
                            activation_count = ?,
                            retrieval_count = retrieval_count + 1,
                            importance = MAX(?, ?),
                            strength_trend = ?,
                            last_retrieved_at = ?
                        WHERE id = ? AND user_id = ?
                    """,
                        (
                            now_iso,
                            new_activation_count,
                            new_importance,
                            config.min_importance,
                            trend,
                            now_iso,
                            memory_id,
                            user_id,
                        ),
                    )
                else:
                    raise

            conn.commit()
            return new_importance, trend

        finally:
            pool.release(conn)

    def batch_update_decay(
        self,
        user_id: str,
        tenant_id: str = "default",
        batch_size: int = 500,
    ) -> int:
        """
        Batch update decay for all user memories in paginated chunks.

        Processes memories in batches of ``batch_size`` to avoid loading
        the entire dataset into memory for users with large memory stores.

        Args:
            user_id: User ID to process.
            tenant_id: Tenant scope.
            batch_size: Number of memories to process per DB round-trip.

        Returns:
            Total number of memories updated.
        """
        pool = get_pool(self.db_path)
        conn = pool.acquire()
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            updated_count = 0
            offset = 0

            while True:
                conn.execute("BEGIN")
                try:
                    try:
                        cursor = conn.execute(
                            """
                            SELECT id, importance, created_at, activation_count,
                                   COALESCE(category, 'general') as category
                            FROM memories
                            WHERE user_id = ? AND tenant_id = ?
                            LIMIT ? OFFSET ?
                            """,
                            (user_id, tenant_id, batch_size, offset),
                        )
                    except Exception as e:
                        if _is_missing_tenant_column_error(e):
                            cursor = conn.execute(
                                """
                                SELECT id, importance, created_at, activation_count,
                                       COALESCE(category, 'general') as category
                                FROM memories
                                WHERE user_id = ?
                                LIMIT ? OFFSET ?
                                """,
                                (user_id, batch_size, offset),
                            )
                        else:
                            raise

                    memories = cursor.fetchall()
                    if not memories:
                        conn.execute("COMMIT")
                        break

                    now = datetime.now(timezone.utc).isoformat()
                    for memory_id, importance, created_at, activation_count, category in memories:
                        new_importance, trend = self.calculate_decay(
                            importance=importance,
                            created_at=created_at,
                            activation_count=activation_count,
                            category=category,
                            user_id=user_id,
                        )
                        try:
                            conn.execute(
                                """
                                UPDATE memories
                                SET importance = ?, strength_trend = ?, updated_at = ?
                                WHERE id = ? AND user_id = ? AND tenant_id = ?
                                """,
                                (new_importance, trend, now, memory_id, user_id, tenant_id),
                            )
                        except Exception as e:
                            if _is_missing_tenant_column_error(e):
                                conn.execute(
                                    """
                                    UPDATE memories
                                    SET importance = ?, strength_trend = ?, updated_at = ?
                                    WHERE id = ? AND user_id = ?
                                    """,
                                    (new_importance, trend, now, memory_id, user_id),
                                )
                            else:
                                raise
                        updated_count += 1

                    conn.execute("COMMIT")
                    offset += batch_size

                except Exception:
                    conn.execute("ROLLBACK")
                    raise

            logger.info("Batch decay update completed: %d memories updated", updated_count)
            return updated_count
        finally:
            pool.release(conn)

    def get_memory_stats(self, user_id: str, tenant_id: str = "default") -> dict:
        """
        Get temporal statistics for user memories.

        Args:
            user_id: User ID

        Returns:
            Dictionary with temporal stats
        """
        pool = get_pool(self.db_path)
        conn = pool.acquire()
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            cursor = conn.execute(
                """
                SELECT
                    COUNT(*) as total_memories,
                    AVG(importance) as avg_importance,
                    SUM(CASE WHEN strength_trend = 'stable' THEN 1 ELSE 0 END) as stable_count,
                    SUM(CASE WHEN strength_trend = 'strengthening' THEN 1 ELSE 0 END) as strengthening_count,
                    SUM(CASE WHEN strength_trend = 'weakening' THEN 1 ELSE 0 END) as weakening_count,
                    SUM(CASE WHEN strength_trend = 'stale' THEN 1 ELSE 0 END) as stale_count,
                    SUM(activation_count) as total_activations
                FROM memories
                WHERE user_id = ? AND tenant_id = ?
            """,
                (user_id, tenant_id),
            )

            row = cursor.fetchone()
            return {
                "total_memories": row[0] or 0,
                "avg_importance": row[1] or 0,
                "stable_count": row[2] or 0,
                "strengthening_count": row[3] or 0,
                "weakening_count": row[4] or 0,
                "stale_count": row[5] or 0,
                "total_activations": row[6] or 0,
            }
        finally:
            pool.release(conn)


# Global instance management (thread-safe)
_temporal_service: TemporalService | None = None
_temporal_service_lock = threading.Lock()


def get_temporal_service(db_path: str | None = None) -> TemporalService:
    """Get or create global temporal service instance (thread-safe)."""
    global _temporal_service
    with _temporal_service_lock:
        if _temporal_service is None:
            if db_path is None:
                from .config import DB_PATH

                db_path = DB_PATH
            _temporal_service = TemporalService(db_path)
    return _temporal_service


def reset_temporal_service() -> None:
    """Reset global temporal service (for testing)."""
    global _temporal_service
    with _temporal_service_lock:
        _temporal_service = None
