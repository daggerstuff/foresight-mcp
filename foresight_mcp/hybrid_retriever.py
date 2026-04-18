"""
Hybrid Retriever - Combined Vector + Graph + Temporal Search.

Fuses three retrieval signals:
1. Keyword/BM25-style: Content matching with tf-idf-like scoring
2. Graph: Entity-based expansion via graph traversal
3. Temporal: Time-weighted importance scoring with decay

Result merging uses Reciprocal Rank Fusion (RRF) for score combination,
which is robust across different score distributions without tuning.
"""
from __future__ import annotations
import sqlite3
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Set
from datetime import datetime, timezone

logger = logging.getLogger("foresight_hybrid_retriever")


@dataclass
class HybridResult:
    """A single result from hybrid retrieval."""
    memory_id: str
    content: str
    category: Optional[str]
    importance: float
    strength_trend: Optional[str]
    created_at: str

    keyword_score: float = 0.0
    graph_score: float = 0.0
    temporal_score: float = 0.0
    combined_score: float = 0.0

    source_signals: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'memory_id': self.memory_id,
            'content': self.content,
            'category': self.category,
            'importance': self.importance,
            'strength_trend': self.strength_trend,
            'created_at': self.created_at,
            'keyword_score': round(self.keyword_score, 4),
            'graph_score': round(self.graph_score, 4),
            'temporal_score': round(self.temporal_score, 4),
            'combined_score': round(self.combined_score, 4),
            'source_signals': self.source_signals,
        }


@dataclass
class HybridSearchResult:
    """Complete result from a hybrid search."""
    results: List[HybridResult]
    total_candidates: int
    signal_counts: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            'total_candidates': self.total_candidates,
            'signal_counts': self.signal_counts,
            'results': [r.to_dict() for r in self.results],
        }


class HybridRetriever:
    """
    Combined retrieval using keyword, graph, and temporal signals.

    Uses Reciprocal Rank Fusion (RRF) to merge ranked lists from
    each signal into a single ordered result set.

    RRF formula: score(d) = sum(1 / (k + rank_i(d)))
    where k = 60 (standard RRF constant).
    """

    RRF_K = 60  # RRF smoothing constant

    # Configurable weights per signal
    DEFAULT_WEIGHTS = {
        'keyword': 1.0,
        'graph': 0.8,
        'temporal': 0.6,
    }

    def __init__(
        self,
        db_path: str,
        weights: Optional[Dict[str, float]] = None,
    ):
        self.db_path = db_path
        self.weights = weights or self.DEFAULT_WEIGHTS.copy()

    def search(
        self,
        query: str,
        user_id: str,
        tenant_id: str = 'default',
        limit: int = 10,
        min_importance: float = 0.1,
        use_keyword: bool = True,
        use_graph: bool = True,
        use_temporal: bool = True,
    ) -> HybridSearchResult:
        """
        Execute hybrid search combining all enabled signals.

        Args:
            query: Search query string
            user_id: User ID
            tenant_id: Tenant ID for isolation
            limit: Maximum results to return
            min_importance: Minimum importance filter
            use_keyword: Enable keyword signal
            use_graph: Enable graph signal
            use_temporal: Enable temporal signal

        Returns:
            HybridSearchResult with merged, ranked results
        """
        # Collect ranked lists from each signal
        keyword_ranking: Dict[str, int] = {}
        graph_ranking: Dict[str, int] = {}
        temporal_ranking: Dict[str, int] = {}

        # Track all candidate memory IDs
        all_ids: Set[str] = set()

        if use_keyword:
            keyword_ranking = self._keyword_search(
                query, user_id, tenant_id, limit * 3
            )
            all_ids.update(keyword_ranking.keys())

        if use_graph:
            graph_ranking = self._graph_search(
                query, user_id, tenant_id, limit * 3
            )
            all_ids.update(graph_ranking.keys())

        if use_temporal:
            temporal_ranking = self._temporal_search(
                user_id, tenant_id, limit * 3, min_importance
            )
            all_ids.update(temporal_ranking.keys())

        if not all_ids:
            return HybridSearchResult(
                results=[],
                total_candidates=0,
                signal_counts={
                    'keyword': len(keyword_ranking),
                    'graph': len(graph_ranking),
                    'temporal': len(temporal_ranking),
                },
            )

        # Merge using RRF
        merged = self._reciprocal_rank_fusion(
            keyword_ranking, graph_ranking, temporal_ranking
        )

        # Fetch full memory data for top candidates
        top_ids = [mid for mid, _ in merged[:limit]]
        memories = self._fetch_memories(top_ids, user_id, tenant_id)

        # Build results with scores
        results = []
        for memory_id, rrf_score in merged[:limit]:
            mem = memories.get(memory_id)
            if not mem:
                continue

            result = HybridResult(
                memory_id=memory_id,
                content=mem['content'],
                category=mem.get('category'),
                importance=mem.get('importance', 0.5),
                strength_trend=mem.get('strength_trend'),
                created_at=mem['created_at'],
                combined_score=rrf_score,
                source_signals=[],
            )

            # Track which signals contributed
            if memory_id in keyword_ranking:
                result.keyword_score = self._rank_to_score(
                    keyword_ranking[memory_id], len(keyword_ranking)
                )
                result.source_signals.append('keyword')
            if memory_id in graph_ranking:
                result.graph_score = self._rank_to_score(
                    graph_ranking[memory_id], len(graph_ranking)
                )
                result.source_signals.append('graph')
            if memory_id in temporal_ranking:
                result.temporal_score = self._rank_to_score(
                    temporal_ranking[memory_id], len(temporal_ranking)
                )
                result.source_signals.append('temporal')

            results.append(result)

        return HybridSearchResult(
            results=results,
            total_candidates=len(all_ids),
            signal_counts={
                'keyword': len(keyword_ranking),
                'graph': len(graph_ranking),
                'temporal': len(temporal_ranking),
            },
        )

    def _keyword_search(
        self,
        query: str,
        user_id: str,
        tenant_id: str,
        limit: int,
    ) -> Dict[str, int]:
        """
        Keyword search with simple tf-idf-like scoring.

        Returns dict of {memory_id: rank} (1-based, lower = better).
        """
        conn = sqlite3.connect(self.db_path)
        try:
            terms = query.lower().split()
            if not terms:
                return {}

            # Build WHERE clause matching any term
            like_clauses = " OR ".join(["content LIKE ?" for _ in terms])
            params = [user_id, tenant_id] + [f"%{t}%" for t in terms]

            cursor = conn.execute(f"""
                SELECT id, content
                FROM memories
                WHERE user_id = ? AND tenant_id = ?
                AND ({like_clauses})
                ORDER BY importance DESC, created_at DESC
                LIMIT ?
            """, params + [limit])

            rows = cursor.fetchall()

            # Score by term frequency
            scored = []
            for row in rows:
                mid, content = row
                content_lower = content.lower()
                tf = sum(content_lower.count(t) for t in terms)
                doc_len = max(len(content_lower.split()), 1)
                # Normalized term frequency
                score = tf / doc_len
                scored.append((mid, score))

            # Sort by score descending, assign ranks
            scored.sort(key=lambda x: x[1], reverse=True)
            return {mid: rank + 1 for rank, (mid, _) in enumerate(scored)}
        finally:
            conn.close()

    def _graph_search(
        self,
        query: str,
        user_id: str,
        tenant_id: str,  # noqa: ARG - reserved for future graph-level tenant isolation
        limit: int,
    ) -> Dict[str, int]:
        """
        Graph-based search: find entities matching query, traverse to
        find related memories.

        Returns dict of {memory_id: rank} (1-based, lower = better).
        """
        _ = tenant_id  # Graph entities are user-scoped; tenant used at memory level
        conn = sqlite3.connect(self.db_path)
        try:
            terms = query.lower().split()
            if not terms:
                return {}

            # Find entities whose name matches query terms
            like_clauses = " OR ".join(["e.name LIKE ?" for _ in terms])
            params = [user_id] + [f"%{t}%" for t in terms]

            cursor = conn.execute(f"""
                SELECT DISTINCT e.id
                FROM memory_entities e
                WHERE e.user_id = ?
                AND ({like_clauses})
            """, params)

            entity_ids = [row[0] for row in cursor.fetchall()]

            if not entity_ids:
                return {}

            # Find memories linked to these entities, with graph depth bonus
            # Memories linked to more query-matching entities rank higher
            entity_placeholders = ','.join('?' * len(entity_ids))
            cursor = conn.execute(f"""
                SELECT mel.memory_id, COUNT(DISTINCT mel.entity_id) as entity_hits
                FROM memory_entity_links mel
                WHERE mel.entity_id IN ({entity_placeholders})
                AND mel.user_id = ?
                GROUP BY mel.memory_id
                ORDER BY entity_hits DESC
                LIMIT ?
            """, entity_ids + [user_id, limit])

            rows = cursor.fetchall()
            return {mid: rank + 1 for rank, (mid, _) in enumerate(rows)}
        finally:
            conn.close()

    def _temporal_search(
        self,
        user_id: str,
        tenant_id: str,
        limit: int,
        min_importance: float,
    ) -> Dict[str, int]:
        """
        Temporal search: rank memories by recency and importance.

        Uses exponential decay for time scoring plus activation boost.

        Returns dict of {memory_id: rank} (1-based, lower = better).
        """
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute("""
                SELECT id, importance, created_at, activation_count,
                       COALESCE(strength_trend, 'stable')
                FROM memories
                WHERE user_id = ? AND tenant_id = ?
                AND importance >= ?
                ORDER BY importance DESC, created_at DESC
                LIMIT ?
            """, (user_id, tenant_id, min_importance, limit * 2))

            rows = cursor.fetchall()
            if not rows:
                return {}

            now = datetime.now(timezone.utc)
            scored = []

            for row in rows:
                mid, importance, created_at, activation_count, trend = row

                try:
                    created = datetime.fromisoformat(
                        created_at.replace('Z', '+00:00')
                    )
                    hours_old = max(
                        (now - created).total_seconds() / 3600, 0.01
                    )
                except (ValueError, AttributeError):
                    hours_old = 168.0  # Default to 1 week

                # Exponential decay (168h half-life)
                time_score = pow(0.5, hours_old / 168.0)

                # Activation boost
                activation_boost = 1 + ((activation_count or 0) * 0.05)

                # Trend modifier
                trend_mod = {
                    'strengthening': 1.2,
                    'stable': 1.0,
                    'weakening': 0.8,
                    'stale': 0.5,
                }.get(trend, 1.0)

                score = min(1.0, importance * time_score * activation_boost * trend_mod)
                scored.append((mid, score))

            scored.sort(key=lambda x: x[1], reverse=True)
            return {mid: rank + 1 for rank, (mid, _) in enumerate(scored)}
        finally:
            conn.close()

    def _reciprocal_rank_fusion(
        self,
        keyword: Dict[str, int],
        graph: Dict[str, int],
        temporal: Dict[str, int],
    ) -> List[tuple]:
        """
        Merge ranked lists using Reciprocal Rank Fusion.

        RRF: score(d) = sum_i(w_i / (k + rank_i(d)))

        Returns list of (memory_id, rrf_score) sorted descending.
        """
        all_ids = set()
        all_ids.update(keyword.keys())
        all_ids.update(graph.keys())
        all_ids.update(temporal.keys())

        scores: Dict[str, float] = {}

        for mid in all_ids:
            score = 0.0

            if mid in keyword:
                score += self.weights['keyword'] / (
                    self.RRF_K + keyword[mid]
                )
            if mid in graph:
                score += self.weights['graph'] / (
                    self.RRF_K + graph[mid]
                )
            if mid in temporal:
                score += self.weights['temporal'] / (
                    self.RRF_K + temporal[mid]
                )

            scores[mid] = score

        return sorted(scores.items(), key=lambda x: x[1], reverse=True)

    def _rank_to_score(self, rank: int, total: int) -> float:
        """Convert rank position to 0-1 score."""
        if total == 0:
            return 0.0
        return 1.0 - (rank - 1) / total

    def _fetch_memories(
        self,
        memory_ids: List[str],
        user_id: str,
        tenant_id: str,
    ) -> Dict[str, dict]:
        """Fetch memory data for given IDs."""
        if not memory_ids:
            return {}

        conn = sqlite3.connect(self.db_path)
        try:
            placeholders = ','.join('?' * len(memory_ids))
            cursor = conn.execute(f"""
                SELECT id, content, category, importance,
                       strength_trend, created_at
                FROM memories
                WHERE id IN ({placeholders})
                AND user_id = ? AND tenant_id = ?
            """, memory_ids + [user_id, tenant_id])

            return {
                row[0]: {
                    'content': row[1],
                    'category': row[2],
                    'importance': row[3] or 0.5,
                    'strength_trend': row[4],
                    'created_at': row[5],
                }
                for row in cursor.fetchall()
            }
        finally:
            conn.close()


# Global instance management
_hybrid_retriever: Optional[HybridRetriever] = None


def get_hybrid_retriever(
    db_path: Optional[str] = None,
    weights: Optional[Dict[str, float]] = None,
) -> HybridRetriever:
    """Get or create global hybrid retriever instance."""
    global _hybrid_retriever
    if _hybrid_retriever is None:
        if db_path is None:
            from .server import DB_PATH
            db_path = DB_PATH
        _hybrid_retriever = HybridRetriever(db_path, weights)
    return _hybrid_retriever


def reset_hybrid_retriever() -> None:
    """Reset global hybrid retriever (for testing)."""
    global _hybrid_retriever
    _hybrid_retriever = None
