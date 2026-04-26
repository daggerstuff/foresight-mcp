"""Temporal Query Patterns for Time-Based Memory Retrieval.

Implements:
- Time-window retrieval (today/week/month/year)
- Time-weighted vector search
- Historical state queries
- Trend analysis
"""
from __future__ import annotations

import logging
from .connection_pool import get_pool

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

logger = logging.getLogger("foresight_temporal_queries")

TimeWindow = Literal["today", "week", "month", "year"]


@dataclass
class TemporalQueryResult:
    """Result of a temporal query."""
    memory_id: str
    content: str
    importance: float
    strength_trend: str
    activation_count: int
    created_at: str
    accessed_at: str
    category: str | None
    time_score: float = 0.0  # Recency score (0-1)
    combined_score: float = 0.0  # Vector + time combined


class TemporalQueryBuilder:
    """
    Builder for temporal memory queries.

    Provides fluent interface for time-based memory retrieval.
    """

    def __init__(self, db_path: str):
        """Initialize query builder."""
        self.db_path = db_path

    def _get_window_hours(self, window: TimeWindow) -> int:
        """Get hours for time window."""
        return {
            "today": 24,
            "week": 168,
            "month": 720,
            "year": 8760,
        }[window]

    def get_memories_from_window(
        self,
        user_id: str,
        window: TimeWindow,
        limit: int = 50,
        min_importance: float = 0.1,
        category: str | None = None,
        tenant_id: str = "default"
    ) -> list[TemporalQueryResult]:
        """
        Get memories from a time window, handling optional tenant column.
        """
        pool = get_pool(self.db_path)
        conn = pool.acquire()
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            window_hours = self._get_window_hours(window)
            cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)

            category_clause = "AND category = ?" if category else ""
            base_params = [user_id, tenant_id, cutoff.isoformat(), min_importance]
            if category:
                base_params.append(category)
            base_params.append(limit)

            sql = f"""
                SELECT
                    id, content, importance, strength_trend,
                    activation_count, created_at, accessed_at, category
                FROM memories
                WHERE user_id = ? AND tenant_id = ?
                AND created_at >= ?
                AND importance >= ?
                {category_clause}
                ORDER BY importance DESC, created_at DESC
                LIMIT ?
            """
            try:
                cursor = conn.execute(sql, base_params)
            except Exception as e:
                if "no column named tenant_id" in str(e):
                    # Retry without tenant filter
                    params = [user_id, cutoff.isoformat(), min_importance]
                    if category:
                        params.append(category)
                    params.append(limit)
                    sql_no_tenant = f"""
                        SELECT
                            id, content, importance, strength_trend,
                            activation_count, created_at, accessed_at, category
                        FROM memories
                        WHERE user_id = ?
                        AND created_at >= ?
                        AND importance >= ?
                        {category_clause}
                        ORDER BY importance DESC, created_at DESC
                        LIMIT ?
                    """
                    cursor = conn.execute(sql_no_tenant, params)
                else:
                    raise

            rows = cursor.fetchall()
            return [
                TemporalQueryResult(
                    memory_id=row[0],
                    content=row[1],
                    importance=row[2],
                    strength_trend=row[3],
                    activation_count=row[4],
                    created_at=row[5],
                    accessed_at=row[6],
                    category=row[7],
                )
                for row in rows
            ]
        finally:
            pool.release(conn)
        """
        Get memories from a time window.

        Args:
            user_id: User ID
            window: Time window (today/week/month/year)
            limit: Max results
            min_importance: Minimum importance threshold
            category: Optional category filter

        Returns:
            List of TemporalQueryResult
        """
        pool = get_pool(self.db_path)
        conn = pool.acquire()
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            window_hours = self._get_window_hours(window)
            cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)

            category_clause = "AND category = ?" if category else ""
            # Build params in the correct order matching placeholders.
            params = [user_id, tenant_id, cutoff.isoformat(), min_importance]
            if category:
                params.append(category)
            params.append(limit)

            cursor = conn.execute(f"""
                SELECT
                    id, content, importance, strength_trend,
                    activation_count, created_at, accessed_at, category
                FROM memories
                WHERE user_id = ? AND tenant_id = ?
                AND created_at >= ?
                AND importance >= ?
                {category_clause}
                ORDER BY importance DESC, created_at DESC
                LIMIT ?
            """, params)

            return [
                TemporalQueryResult(
                    memory_id=row[0],
                    content=row[1],
                    importance=row[2],
                    strength_trend=row[3],
                    activation_count=row[4],
                    created_at=row[5],
                    accessed_at=row[6],
                    category=row[7],
                )
                for row in cursor.fetchall()
            ]
        finally:
            pool.release(conn)

    def get_memories_as_of_time(
        self,
        user_id: str,
        target_date: datetime,
        category: str | None = None,
        min_importance: float = 0.1,
        tenant_id: str = "default"
    ) -> list[TemporalQueryResult]:
        """
        Get memories as of a specific time, with optional tenant handling.
        """
        pool = get_pool(self.db_path)
        conn = pool.acquire()
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            category_clause = "AND category = ?" if category else ""
            base_params = [user_id, tenant_id, target_date.isoformat(), min_importance]
            if category:
                base_params = [user_id, tenant_id, category, target_date.isoformat(), min_importance]

            sql = f"""
                SELECT
                    id, content, importance, strength_trend,
                    activation_count, created_at, accessed_at, category
                FROM memories
                WHERE user_id = ? AND tenant_id = ?
                AND created_at <= ?
                AND importance > ?
                {category_clause}
                ORDER BY created_at DESC
            """
            try:
                cursor = conn.execute(sql, base_params)
            except Exception as e:
                if "no column named tenant_id" in str(e):
                    # Retry without tenant filter
                    params = [user_id, target_date.isoformat(), min_importance]
                    if category:
                        params = [user_id, category, target_date.isoformat(), min_importance]
                    sql_no_tenant = f"""
                        SELECT
                            id, content, importance, strength_trend,
                            activation_count, created_at, accessed_at, category
                        FROM memories
                        WHERE user_id = ?
                        AND created_at <= ?
                        AND importance > ?
                        {category_clause}
                        ORDER BY created_at DESC
                    """
                    cursor = conn.execute(sql_no_tenant, params)
                else:
                    raise

            rows = cursor.fetchall()
            return [
                TemporalQueryResult(
                    memory_id=row[0],
                    content=row[1],
                    importance=row[2],
                    strength_trend=row[3],
                    activation_count=row[4],
                    created_at=row[5],
                    accessed_at=row[6],
                    category=row[7],
                )
                for row in rows
            ]
        finally:
            pool.release(conn)
        """
        Get memories as they existed at a specific time.

        Useful for historical state queries.

        Args:
            user_id: User ID
            target_date: Target date/time
            category: Optional category filter
            min_importance: Minimum importance threshold

        Returns:
            List of TemporalQueryResult
        """
        pool = get_pool(self.db_path)
        conn = pool.acquire()
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            category_clause = "AND category = ?" if category else ""
            params = [user_id, tenant_id, target_date.isoformat(), min_importance]
            if category:
                params = [user_id, tenant_id, category, target_date.isoformat(), min_importance]

            cursor = conn.execute(f"""
                SELECT
                    id, content, importance, strength_trend,
                    activation_count, created_at, accessed_at, category
                FROM memories
                WHERE user_id = ? AND tenant_id = ?
                AND created_at <= ?
                AND importance > ?
                {category_clause}
                ORDER BY created_at DESC
            """, params)

            return [
                TemporalQueryResult(
                    memory_id=row[0],
                    content=row[1],
                    importance=row[2],
                    strength_trend=row[3],
                    activation_count=row[4],
                    created_at=row[5],
                    accessed_at=row[6],
                    category=row[7],
                )
                for row in cursor.fetchall()
            ]
        finally:
            pool.release(conn)

    def get_memories_by_trend(
        self,
        user_id: str,
        trend: str,
        limit: int = 50,
        category: str | None = None,
        tenant_id: str = "default"
    ) -> list[TemporalQueryResult]:
        """
        Get memories by trend, handling optional tenant column.
        """
        pool = get_pool(self.db_path)
        conn = pool.acquire()
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            category_clause = "AND category = ?" if category else ""
            base_params = [user_id, tenant_id, trend, limit]
            if category:
                base_params = [user_id, tenant_id, category, trend, limit]

            sql = f"""
                SELECT
                    id, content, importance, strength_trend,
                    activation_count, created_at, accessed_at, category
                FROM memories
                WHERE user_id = ? AND tenant_id = ?
                AND strength_trend = ?
                {category_clause}
                ORDER BY created_at DESC
                LIMIT ?
            """
            try:
                cursor = conn.execute(sql, base_params)
            except Exception as e:
                if "no column named tenant_id" in str(e):
                    params = [user_id, trend, limit]
                    if category:
                        params = [user_id, category, trend, limit]
                    sql_no_tenant = f"""
                        SELECT
                            id, content, importance, strength_trend,
                            activation_count, created_at, accessed_at, category
                        FROM memories
                        WHERE user_id = ?
                        AND strength_trend = ?
                        {category_clause}
                        ORDER BY created_at DESC
                        LIMIT ?
                    """
                    cursor = conn.execute(sql_no_tenant, params)
                else:
                    raise

            rows = cursor.fetchall()
            return [
                TemporalQueryResult(
                    memory_id=row[0],
                    content=row[1],
                    importance=row[2],
                    strength_trend=row[3],
                    activation_count=row[4],
                    created_at=row[5],
                    accessed_at=row[6],
                    category=row[7],
                )
                for row in rows
            ]
        finally:
            pool.release(conn)
        """
        Get memories by freshness trend.

        Args:
            user_id: User ID
            trend: Trend type (stable/strengthening/weakening/stale)
            limit: Max results
            category: Optional category filter

        Returns:
            List of TemporalQueryResult
        """
        pool = get_pool(self.db_path)
        conn = pool.acquire()
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            category_clause = "AND category = ?" if category else ""
            params = [user_id, tenant_id, trend, limit]
            if category:
                params = [user_id, tenant_id, category, trend, limit]

            cursor = conn.execute(f"""
                SELECT
                    id, content, importance, strength_trend,
                    activation_count, created_at, accessed_at, category
                FROM memories
                WHERE user_id = ? AND tenant_id = ?
                AND strength_trend = ?
                {category_clause}
                ORDER BY created_at DESC
                LIMIT ?
            """, params)

            return [
                TemporalQueryResult(
                    memory_id=row[0],
                    content=row[1],
                    importance=row[2],
                    strength_trend=row[3],
                    activation_count=row[4],
                    created_at=row[5],
                    accessed_at=row[6],
                    category=row[7],
                )
                for row in cursor.fetchall()
            ]
        finally:
            pool.release(conn)

    def analyze_trends(
        self,
        user_id: str,
        timeframe: str = "30 days",
        tenant_id: str = "default"
    ) -> dict:
        """
        Analyze memory trends over time.

        Args:
            user_id: User ID
            timeframe: Timeframe for analysis (e.g., '30 days', '7 days')

        Returns:
            Dictionary with trend analysis
        """
        pool = get_pool(self.db_path)
        conn = pool.acquire()
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            # Daily stats
            cursor = conn.execute(f"""
                SELECT
                    strftime('%Y-%m-%d', created_at) as date,
                    COUNT(*) as count,
                    AVG(importance) as avg_importance,
                    SUM(CASE WHEN strength_trend = 'strengthening' THEN 1 ELSE 0 END) as strengthening,
                    SUM(CASE WHEN strength_trend = 'stale' THEN 1 ELSE 0 END) as stale
                FROM memories
                WHERE user_id = ? AND tenant_id = ?
                AND created_at >= datetime('now', '-' || ?)
                GROUP BY date
                ORDER BY date
            """, (user_id, tenant_id, timeframe))

            daily_stats = [
                {
                    "date": row[0],
                    "count": row[1],
                    "avg_importance": row[2] or 0,
                    "strengthening": row[3] or 0,
                    "stale": row[4] or 0,
                }
                for row in cursor.fetchall()
            ]

            # Category breakdown
            cursor = conn.execute(f"""
                SELECT
                    COALESCE(category, 'general') as category,
                    COUNT(*) as count,
                    AVG(importance) as avg_importance,
                    SUM(activation_count) as total_activations
                FROM memories
                WHERE user_id = ? AND tenant_id = ?
                AND created_at >= datetime('now', '-' || ?)
                GROUP BY category
                ORDER BY count DESC
            """, (user_id, tenant_id, timeframe))

            category_breakdown = [
                {
                    "category": row[0],
                    "count": row[1],
                    "avg_importance": row[2] or 0,
                    "total_activations": row[3] or 0,
                }
                for row in cursor.fetchall()
            ]

            return {
                "daily_stats": daily_stats,
                "category_breakdown": category_breakdown,
                "overall_trend": self._calculate_overall_trend(daily_stats),
            }
        finally:
            pool.release(conn)

    def _calculate_overall_trend(self, daily_stats: list[dict]) -> str:
        """Calculate overall trend from daily stats."""
        if len(daily_stats) < 3:
            return "insufficient_data"

        # Simple trend: compare first half avg to second half avg
        mid = len(daily_stats) // 2
        first_half = [d["avg_importance"] for d in daily_stats[:mid]]
        second_half = [d["avg_importance"] for d in daily_stats[mid:]]

        first_avg = sum(first_half) / len(first_half)
        second_avg = sum(second_half) / len(second_half)

        delta = second_avg - first_avg

        if delta > 0.1:
            return "improving"
        elif delta < -0.1:
            return "declining"
        return "stable"

    def get_time_weighted_scores(
        self,
        memory_ids: list[str],
        user_id: str,
        tenant_id: str = "default"
    ) -> dict:
        """
        Calculate time-weighted scores for memories.

        Used for re-ranking vector search results with recency bias.

        Args:
            memory_ids: List of memory IDs
            user_id: User ID

        Returns:
            Dictionary mapping memory_id to time_score
        """
        pool = get_pool(self.db_path)
        conn = pool.acquire()
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            placeholders = ",".join("?" * len(memory_ids))
            cursor = conn.execute(f"""
                SELECT id, created_at, activation_count
                FROM memories
                WHERE id IN ({placeholders}) AND user_id = ? AND tenant_id = ?
            """, memory_ids + [user_id, tenant_id])

            scores = {}
            now = datetime.now(timezone.utc)

            for row in cursor:
                memory_id, created_at, activation_count = row
                created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                hours_old = (now - created).total_seconds() / 3600

                # Exponential decay for recency score (1 = very recent, 0 = very old)
                # 168 hours = 1 week half-life
                time_score = pow(0.5, hours_old / 168)

                # Boost for activation
                activation_boost = 1 + (activation_count * 0.05)
                time_score = min(1.0, time_score * activation_boost)

                scores[memory_id] = time_score

            return scores
        finally:
            pool.release(conn)


# Global instance management (thread-safe)
_temporal_query_builder: TemporalQueryBuilder | None = None
_temporal_query_lock = threading.Lock()


def get_temporal_query_builder(db_path: str | None = None) -> TemporalQueryBuilder:
    """Get or create global temporal query builder instance (thread-safe)."""
    global _temporal_query_builder
    with _temporal_query_lock:
        if _temporal_query_builder is None:
            if db_path is None:
                from .config import DB_PATH
                db_path = DB_PATH
            _temporal_query_builder = TemporalQueryBuilder(db_path)
    return _temporal_query_builder


def reset_temporal_query_builder() -> None:
    """Reset global temporal query builder (for testing)."""
    global _temporal_query_builder
    with _temporal_query_lock:
        _temporal_query_builder = None
