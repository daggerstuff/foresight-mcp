"""
Hybrid Retriever - Combined TF-IDF + Graph + Temporal Search.

Fuses four retrieval signals:
1. Keyword/BM25-style: Content matching with TF-IDF-like scoring
2. TF-IDF Cosine: Bag-of-words cosine similarity (NOT neural embeddings)
3. Graph: Entity-based expansion via graph traversal
4. Temporal: Time-weighted importance scoring with decay

Result merging uses Reciprocal Rank Fusion (RRF) for score combination,
which is robust across different score distributions without tuning.

Weights rationale:
keyword=1.0 (primary relevance signal), graph=0.8 (entity expansion
is high-value but indirect), tfidf_cosine=0.7 (topical similarity
via TF-IDF vector cosine, NOT tfidf_cosine embeddings), temporal=0.6
(recency is useful context but not a relevance signal by itself).

NOTE: This implementation uses TF-IDF cosine similarity, NOT true
tfidf_cosine search with neural embeddings. For actual tfidf_cosine search,
implement an embedding service (see embedding_validation.py for
dimension requirements when adding that capability).
"""

from __future__ import annotations

import logging
import math
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import ClassVar

from .config import DB_PATH
from .connection_pool import get_pool

logger = logging.getLogger("foresight_hybrid_retriever")


MAX_QUERY_LENGTH = 500
MAX_USER_ID_LENGTH = 128


def _escape_like(term: str) -> str:
    """Escape SQL LIKE metacharacters to prevent wildcard injection."""
    return term.replace("!", "!!").replace("%", "!%").replace("_", "!_")


def _validate_input(query: str, user_id: str) -> None:
    """Validate search inputs."""
    if not user_id or len(user_id) > MAX_USER_ID_LENGTH:
        raise ValueError(f"user_id must be 1-{MAX_USER_ID_LENGTH} chars")
    if len(query) > MAX_QUERY_LENGTH:
        raise ValueError(f"query must be <= {MAX_QUERY_LENGTH} chars")


@dataclass
class HybridResult:
    """A single result from hybrid retrieval."""

    memory_id: str
    content: str
    category: str | None
    importance: float
    strength_trend: str | None
    created_at: str

    keyword_score: float = 0.0
    tfidf_cosine_score: float = 0.0
    semantic_score: float = 0.0
    graph_score: float = 0.0
    temporal_score: float = 0.0
    combined_score: float = 0.0

    source_signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "memory_id": self.memory_id,
            "content": self.content,
            "category": self.category,
            "importance": self.importance,
            "strength_trend": self.strength_trend,
            "created_at": self.created_at,
            "keyword_score": round(self.keyword_score, 4),
            "tfidf_cosine_score": round(self.tfidf_cosine_score, 4),
            "semantic_score": round(self.semantic_score, 4),
            "graph_score": round(self.graph_score, 4),
            "temporal_score": round(self.temporal_score, 4),
            "combined_score": round(self.combined_score, 4),
            "source_signals": self.source_signals,
        }


@dataclass
class HybridSearchResult:
    """Complete result from a hybrid search."""

    results: list[HybridResult]
    total_candidates: int
    signal_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "total_candidates": self.total_candidates,
            "signal_counts": self.signal_counts,
            "results": [r.to_dict() for r in self.results],
        }


class HybridRetriever:
    """
    Combined retrieval using keyword, tfidf_cosine, graph, and temporal signals.

    Uses Reciprocal Rank Fusion (RRF) to merge ranked lists from
    each signal into a single ordered result set.

    RRF formula: score(d) = sum(1 / (k + rank_i(d)))
    where k = 60 (standard RRF constant).

    Query optimization: All sub-queries use batched IN clauses to avoid N+1 patterns.

    TF-IDF caching: IDF vectors are cached per (user_id, tenant_id) key and
    invalidated when the memory count for that scope changes. This avoids
    recomputing IDF from scratch on every query.
    """

    RRF_K = 60  # RRF smoothing constant

    # keyword=1.0 (primary relevance), graph=0.8 (indirect expansion),
    # semantic=0.7 (topical similarity beyond exact match),
    # temporal=0.6 (recency context, not relevance by itself)
    DEFAULT_WEIGHTS: ClassVar[dict[str, float]] = {
        "keyword": 1.0,
        "semantic": 0.7,
        "graph": 0.8,
        "temporal": 0.6,
    }

    def __init__(
        self,
        db_path: str,
        weights: dict[str, float] | None = None,
    ):
        self.db_path = db_path
        merged = self.DEFAULT_WEIGHTS.copy()
        if weights:
            merged.update(weights)
        if "semantic" not in merged and "tfidf_cosine" in merged:
            merged["semantic"] = merged["tfidf_cosine"]
        if "tfidf_cosine" not in merged and "semantic" in merged:
            merged["tfidf_cosine"] = merged["semantic"]
        self.weights = merged

        # TF-IDF cache: maps (user_id, tenant_id) -> {"idf": dict, "doc_count": int,
        #   "docs": dict[id, list[str]]}
        # Invalidated when doc_count changes (new/deleted memories).
        self._tfidf_cache: dict[tuple[str, str], dict] = {}
        self._tfidf_cache_lock = threading.Lock()

    def _get_connection(self) -> sqlite3.Connection:
        """Get a database connection with WAL mode for concurrent safety."""
        pool = get_pool(self.db_path)
        conn = pool.acquire()
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def search(  # noqa: PLR0913,PLR0912,PLR0915
        self,
        query: str,
        user_id: str,
        tenant_id: str = "default",
        limit: int = 10,
        min_importance: float = 0.1,
        use_keyword: bool = True,
        use_tfidf_cosine: bool = True,
        use_semantic: bool | None = None,
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
            use_tfidf_cosine: Enable tf-idf cosine similarity signal
            use_semantic: Backward-compatible alias for tf-idf cosine
            use_graph: Enable graph signal
            use_temporal: Enable temporal signal

        Returns:
            HybridSearchResult with merged, ranked results
        """
        semantic_label = "tfidf_cosine"
        if use_semantic is not None:
            use_tfidf_cosine = use_semantic
            semantic_label = "semantic"

        _validate_input(query, user_id)

        keyword_ranking: dict[str, int] = {}
        tfidf_cosine_ranking: dict[str, int] = {}
        graph_ranking: dict[str, int] = {}
        temporal_ranking: dict[str, int] = {}
        all_ids: set[str] = set()

        # Single connection for all sub-searches
        conn = self._get_connection()
        try:
            if use_keyword:
                keyword_ranking = self._keyword_search(conn, query, user_id, tenant_id, limit * 3)
                all_ids.update(keyword_ranking.keys())

            if use_tfidf_cosine:
                tfidf_cosine_ranking = self._tfidf_cosine_search(conn, query, user_id, tenant_id, limit * 3)
                all_ids.update(tfidf_cosine_ranking.keys())

            if use_graph:
                graph_ranking = self._graph_search(conn, query, user_id, tenant_id, limit * 3)
                all_ids.update(graph_ranking.keys())

            if use_temporal:
                temporal_ranking = self._temporal_search(conn, user_id, tenant_id, limit * 3, min_importance)
                all_ids.update(temporal_ranking.keys())

            if not all_ids:
                return HybridSearchResult(
                    results=[],
                    total_candidates=0,
                    signal_counts={
                        "keyword": len(keyword_ranking),
                        "tfidf_cosine": len(tfidf_cosine_ranking),
                        "graph": len(graph_ranking),
                        "temporal": len(temporal_ranking),
                    },
                )

            # Merge using RRF
            merged = self._reciprocal_rank_fusion(
                keyword_ranking, tfidf_cosine_ranking, graph_ranking, temporal_ranking
            )

            # Fetch full memory data for top candidates (same connection)
            top_ids = [mid for mid, _ in merged[:limit]]
            memories = self._fetch_memories(conn, top_ids, user_id, tenant_id)
        finally:
            conn.close()

        # Build results with scores
        results = []
        for memory_id, rrf_score in merged[:limit]:
            mem = memories.get(memory_id)
            if not mem:
                continue

            result = HybridResult(
                memory_id=memory_id,
                content=mem["content"],
                category=mem.get("category"),
                importance=mem.get("importance", 0.5),
                strength_trend=mem.get("strength_trend"),
                created_at=mem["created_at"],
                combined_score=rrf_score,
                source_signals=[],
            )

            if memory_id in keyword_ranking:
                result.keyword_score = self._rank_to_score(keyword_ranking[memory_id], len(keyword_ranking))
                result.source_signals.append("keyword")
            if memory_id in tfidf_cosine_ranking:
                result.semantic_score = self._rank_to_score(tfidf_cosine_ranking[memory_id], len(tfidf_cosine_ranking))
                result.tfidf_cosine_score = self._rank_to_score(
                    tfidf_cosine_ranking[memory_id], len(tfidf_cosine_ranking)
                )
                result.source_signals.append(semantic_label)
            if memory_id in graph_ranking:
                result.graph_score = self._rank_to_score(graph_ranking[memory_id], len(graph_ranking))
                result.source_signals.append("graph")
            if memory_id in temporal_ranking:
                result.temporal_score = self._rank_to_score(temporal_ranking[memory_id], len(temporal_ranking))
                result.source_signals.append("temporal")

            results.append(result)

        return HybridSearchResult(
            results=results,
            total_candidates=len(all_ids),
            signal_counts={
                "keyword": len(keyword_ranking),
                "tfidf_cosine": len(tfidf_cosine_ranking),
                "semantic": len(tfidf_cosine_ranking),
                "graph": len(graph_ranking),
                "temporal": len(temporal_ranking),
            },
        )

    def _keyword_search(
        self,
        conn: sqlite3.Connection,
        query: str,
        user_id: str,
        tenant_id: str,
        limit: int,
    ) -> dict[str, int]:
        """
        Keyword search with simple tf-idf-like scoring.

        Returns dict of {memory_id: rank} (1-based, lower = better).
        """
        terms = query.lower().split()
        if not terms:
            return {}

        # Escape LIKE metacharacters to prevent wildcard injection
        escaped_terms = [_escape_like(t) for t in terms]

        like_clauses = " OR ".join(["content LIKE ? ESCAPE '!'" for _ in terms])
        params = [user_id, tenant_id] + [f"%{t}%" for t in escaped_terms]

        cursor = conn.execute(
            f"""
            SELECT id, content
            FROM memories
            WHERE user_id = ? AND tenant_id = ?
            AND ({like_clauses})
            ORDER BY importance DESC, created_at DESC
            LIMIT ?
        """,
            [*params, limit],
        )

        rows = cursor.fetchall()

        # Score by term frequency
        scored = []
        for row in rows:
            mid, content = row
            content_lower = content.lower()
            tf = sum(content_lower.count(t) for t in terms)
            doc_len = max(len(content_lower.split()), 1)
            score = tf / doc_len
            scored.append((mid, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return {mid: rank + 1 for rank, (mid, _) in enumerate(scored)}

    def _build_tfidf_cache(
        self,
        conn: sqlite3.Connection,
        user_id: str,
        tenant_id: str,
    ) -> dict:
        """
        Build (or return cached) TF-IDF corpus data for a user/tenant scope.

        Cache key: (user_id, tenant_id).
        Cache is invalidated when the memory count changes, which covers
        inserts and deletes without requiring explicit invalidation calls.

        Returns a dict with keys:
            "idf"       - {term: float}
            "docs"      - {memory_id: list[str]}  (tokenized)
            "doc_count" - int
        """
        cache_key = (user_id, tenant_id)

        # Check current doc count first (cheap query)
        count_row = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE user_id = ? AND tenant_id = ? AND is_ghost = 0",
            (user_id, tenant_id),
        ).fetchone()
        current_count: int = count_row[0] if count_row else 0

        with self._tfidf_cache_lock:
            cached = self._tfidf_cache.get(cache_key)
            if cached is not None and cached["doc_count"] == current_count:
                return cached

        # Cache miss or stale — rebuild outside the lock to avoid blocking
        # other threads during the DB fetch.
        rows = conn.execute(
            "SELECT id, content FROM memories WHERE user_id = ? AND tenant_id = ? AND is_ghost = 0",
            (user_id, tenant_id),
        ).fetchall()

        docs: dict[str, list[str]] = {mid: content.lower().split() for mid, content in rows}
        n_docs = len(docs)

        doc_freq: dict[str, int] = {}
        for tokens in docs.values():
            for token in set(tokens):
                doc_freq[token] = doc_freq.get(token, 0) + 1

        idf: dict[str, float] = {term: math.log(n_docs / df) if df > 0 else 0.0 for term, df in doc_freq.items()}

        entry = {"idf": idf, "docs": docs, "doc_count": current_count}
        with self._tfidf_cache_lock:
            self._tfidf_cache[cache_key] = entry

        return entry

    def invalidate_tfidf_cache(self, user_id: str, tenant_id: str) -> None:
        """Explicitly invalidate the TF-IDF cache for a user/tenant scope.

        Call this after bulk memory operations to force an immediate rebuild
        on the next query rather than waiting for the count-based check.
        """
        with self._tfidf_cache_lock:
            self._tfidf_cache.pop((user_id, tenant_id), None)

    @staticmethod
    def _tfidf_vector(tokens: list[str], idf: dict[str, float]) -> dict[str, float]:
        """Build a TF-IDF vector (sparse dict) for a token list."""
        tf_counts: dict[str, int] = {}
        for token in tokens:
            tf_counts[token] = tf_counts.get(token, 0) + 1
        total = len(tokens) if tokens else 1
        return {term: (count / total) * idf.get(term, 0.0) for term, count in tf_counts.items()}

    @staticmethod
    def _cosine_similarity(vec_a: dict[str, float], vec_b: dict[str, float]) -> float:
        """Compute cosine similarity between two sparse vectors."""
        common = vec_a.keys() & vec_b.keys()
        if not common:
            return 0.0
        dot = sum(vec_a[k] * vec_b[k] for k in common)
        norm_a = math.sqrt(sum(v * v for v in vec_a.values()))
        norm_b = math.sqrt(sum(v * v for v in vec_b.values()))
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return dot / (norm_a * norm_b)

    def _tfidf_cosine_search(
        self,
        conn: sqlite3.Connection,
        query: str,
        user_id: str,
        tenant_id: str,
        limit: int,
    ) -> dict[str, int]:
        """
        Semantic search using TF-IDF cosine similarity.

        IDF vectors are cached per (user_id, tenant_id) and only rebuilt
        when the memory count changes, avoiding per-query recomputation.
        Pure Python implementation -- no external ML dependencies.

        Returns dict of {memory_id: rank} (1-based, lower = better).
        """
        terms = query.lower().split()
        if not terms:
            return {}

        corpus = self._build_tfidf_cache(conn, user_id, tenant_id)
        docs = corpus["docs"]
        idf = corpus["idf"]

        if not docs:
            return {}

        query_vector = self._tfidf_vector(terms, idf)

        scored: list[tuple[str, float]] = [
            (mid, self._cosine_similarity(query_vector, self._tfidf_vector(tokens, idf)))
            for mid, tokens in docs.items()
        ]
        scored.sort(key=lambda x: x[1], reverse=True)

        ranked = {mid: rank + 1 for rank, (mid, sim) in enumerate(scored) if sim > 0.0}
        if limit and len(ranked) > limit:
            ranked = dict(list(ranked.items())[:limit])
        return ranked

    def _semantic_search(
        self,
        conn: sqlite3.Connection,
        query: str,
        user_id: str,
        tenant_id: str,
        limit: int,
    ) -> dict[str, int]:
        """Backward-compatible entrypoint for semantic search alias."""
        return self._tfidf_cosine_search(conn, query, user_id, tenant_id, limit)

    def _graph_search(
        self,
        conn: sqlite3.Connection,
        query: str,
        user_id: str,
        tenant_id: str,
        limit: int,
    ) -> dict[str, int]:
        """
        Graph-based search: find entities matching query, traverse to
        find related memories.

        Returns dict of {memory_id: rank} (1-based, lower = better).
        """
        terms = query.lower().split()
        if not terms:
            return {}

        escaped_terms = [_escape_like(t) for t in terms]

        # Find entities whose name matches query terms
        like_clauses = " OR ".join(["e.name LIKE ? ESCAPE '!'" for _ in terms])
        entity_cols = {row[1] for row in conn.execute("PRAGMA table_info(memory_entities)").fetchall()}
        if "tenant_id" in entity_cols:
            sql = f"""
                SELECT DISTINCT e.id
                FROM memory_entities e
                WHERE e.user_id = ? AND e.tenant_id = ?
                  AND ({like_clauses})
            """
            params = [user_id, tenant_id] + [f"%{t}%" for t in escaped_terms]
        else:
            sql = f"""
                SELECT DISTINCT e.id
                FROM memory_entities e
                WHERE e.user_id = ?
                  AND ({like_clauses})
            """
            params = [user_id] + [f"%{t}%" for t in escaped_terms]

        cursor = conn.execute(sql, params)

        entity_ids = [row[0] for row in cursor.fetchall()]

        if not entity_ids:
            return {}

        # Find memories linked to these entities
        # Filter by tenant via memories table join
        entity_placeholders = ",".join("?" * len(entity_ids))
        cursor = conn.execute(
            f"""
            SELECT mel.memory_id, COUNT(DISTINCT mel.entity_id) as entity_hits
            FROM memory_entity_links mel
            INNER JOIN memories m ON m.id = mel.memory_id
            AND m.tenant_id = ? AND m.user_id = ?
            WHERE mel.entity_id IN ({entity_placeholders})
            AND mel.user_id = ?
            GROUP BY mel.memory_id
            ORDER BY entity_hits DESC
            LIMIT ?
        """,
            [tenant_id, user_id, *entity_ids, user_id, limit],
        )

        rows = cursor.fetchall()
        return {mid: rank + 1 for rank, (mid, _) in enumerate(rows)}

    def _temporal_search(
        self,
        conn: sqlite3.Connection,
        user_id: str,
        tenant_id: str,
        limit: int,
        min_importance: float,
    ) -> dict[str, int]:
        """
        Temporal search: rank memories by recency and importance.

        Query-independent signal -- returns most important/recent memories
        to provide recency context to the fusion.

        Returns dict of {memory_id: rank} (1-based, lower = better).
        """
        cursor = conn.execute(
            """
            SELECT id, importance, created_at, activation_count,
                   COALESCE(strength_trend, 'stable')
            FROM memories
            WHERE user_id = ? AND tenant_id = ?
            AND importance >= ?
            ORDER BY importance DESC, created_at DESC
            LIMIT ?
        """,
            (user_id, tenant_id, min_importance, limit),
        )

        rows = cursor.fetchall()
        if not rows:
            return {}

        now = datetime.now(timezone.utc)
        scored = []

        for row in rows:
            mid, importance, created_at, activation_count, trend = row

            try:
                created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                hours_old = max((now - created).total_seconds() / 3600, 0.01)
            except (ValueError, AttributeError):
                hours_old = 168.0

            time_score = pow(0.5, hours_old / 168.0)
            activation_boost = 1 + ((activation_count or 0) * 0.05)
            trend_mod = {
                "strengthening": 1.2,
                "stable": 1.0,
                "weakening": 0.8,
                "stale": 0.5,
            }.get(trend, 1.0)

            score = min(1.0, importance * time_score * activation_boost * trend_mod)
            scored.append((mid, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return {mid: rank + 1 for rank, (mid, _) in enumerate(scored)}

    def _reciprocal_rank_fusion(
        self,
        keyword: dict[str, int],
        tfidf_cosine: dict[str, int],
        graph: dict[str, int],
        temporal: dict[str, int],
    ) -> list[tuple]:
        """
        Merge ranked lists using Reciprocal Rank Fusion.

        RRF: score(d) = sum_i(w_i / (k + rank_i(d)))

        Returns list of (memory_id, rrf_score) sorted descending.
        """
        all_ids = set()
        all_ids.update(keyword.keys())
        all_ids.update(tfidf_cosine.keys())
        all_ids.update(graph.keys())
        all_ids.update(temporal.keys())

        scores: dict[str, float] = {}

        for mid in all_ids:
            score = 0.0

            if mid in keyword:
                score += self.weights["keyword"] / (self.RRF_K + keyword[mid])
            if mid in tfidf_cosine:
                score += self.weights["tfidf_cosine"] / (self.RRF_K + tfidf_cosine[mid])
            if mid in graph:
                score += self.weights["graph"] / (self.RRF_K + graph[mid])
            if mid in temporal:
                score += self.weights["temporal"] / (self.RRF_K + temporal[mid])

            scores[mid] = score

        return sorted(scores.items(), key=lambda x: x[1], reverse=True)

    def _rank_to_score(self, rank: int, total: int) -> float:
        """Convert rank position to 0-1 score."""
        if total == 0:
            return 0.0
        return 1.0 - (rank - 1) / total

    def _fetch_memories(
        self,
        conn: sqlite3.Connection,
        memory_ids: list[str],
        user_id: str,
        tenant_id: str,
    ) -> dict[str, dict]:
        """Fetch memory data for given IDs."""
        if not memory_ids:
            return {}

        placeholders = ",".join("?" * len(memory_ids))
        cursor = conn.execute(
            f"""
            SELECT id, content, category, importance,
                   strength_trend, created_at
            FROM memories
            WHERE id IN ({placeholders})
            AND user_id = ? AND tenant_id = ?
        """,
            [*memory_ids, user_id, tenant_id],
        )

        return {
            row[0]: {
                "content": row[1],
                "category": row[2],
                "importance": row[3] or 0.5,
                "strength_trend": row[4],
                "created_at": row[5],
            }
            for row in cursor.fetchall()
        }


# Global instance management (thread-safe)
class _HybridRetrieverSingleton:
    """Module-level singleton for HybridRetriever."""

    _instance: HybridRetriever | None = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls, db_path: str | None = None, weights: dict[str, float] | None = None) -> HybridRetriever:
        """Get or create global hybrid retriever instance (thread-safe)."""
        with cls._lock:
            if cls._instance is None:
                if db_path is None:
                    db_path = DB_PATH
                cls._instance = HybridRetriever(db_path, weights)
            return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset global hybrid retriever (for testing)."""
        with cls._lock:
            cls._instance = None


def get_hybrid_retriever(
    db_path: str | None = None,
    weights: dict[str, float] | None = None,
) -> HybridRetriever:
    """Get or create global hybrid retriever instance (thread-safe)."""
    return _HybridRetrieverSingleton.get_instance(db_path, weights)


def reset_hybrid_retriever() -> None:
    """Reset global hybrid retriever (for testing)."""
    _HybridRetrieverSingleton.reset()
