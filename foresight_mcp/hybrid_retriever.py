"""
Hybrid Retriever - Combined Vector + Graph + Temporal Search.

Fuses four retrieval signals:
1. Keyword/BM25-style: Content matching with tf-idf-like scoring
2. Semantic/TF-IDF: Cosine similarity between query and document vectors
3. Graph: Entity-based expansion via graph traversal
4. Temporal: Time-weighted importance scoring with decay

Result merging uses Reciprocal Rank Fusion (RRF) for score combination,
which is robust across different score distributions without tuning.

Weights rationale:
keyword=1.0 (primary relevance signal), graph=0.8 (entity expansion
is high-value but indirect), semantic=0.7 (captures topical similarity
beyond exact term match), temporal=0.6 (recency is useful context
but not a relevance signal by itself).
"""
from __future__ import annotations
import math
import sqlite3
import threading
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Set
from datetime import datetime, timezone

logger = logging.getLogger("foresight_hybrid_retriever")

MAX_QUERY_LENGTH = 500
MAX_USER_ID_LENGTH = 128


def _escape_like(term: str) -> str:
    """Escape SQL LIKE metacharacters to prevent wildcard injection."""
    return term.replace('!', '!!').replace('%', '!%').replace('_', '!_')


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
    category: Optional[str]
    importance: float
    strength_trend: Optional[str]
    created_at: str

    keyword_score: float = 0.0
    semantic_score: float = 0.0
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
            'semantic_score': round(self.semantic_score, 4),
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
    Combined retrieval using keyword, semantic, graph, and temporal signals.

    Uses Reciprocal Rank Fusion (RRF) to merge ranked lists from
    each signal into a single ordered result set.

    RRF formula: score(d) = sum(1 / (k + rank_i(d)))
    where k = 60 (standard RRF constant).
    """

    RRF_K = 60  # RRF smoothing constant

    # keyword=1.0 (primary relevance), graph=0.8 (indirect expansion),
    # semantic=0.7 (topical similarity beyond exact match),
    # temporal=0.6 (recency context, not relevance by itself)
    DEFAULT_WEIGHTS = {
        'keyword': 1.0,
        'semantic': 0.7,
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

    def _get_connection(self) -> sqlite3.Connection:
        """Get a database connection with WAL mode for concurrent safety."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def search(
        self,
        query: str,
        user_id: str,
        tenant_id: str = 'default',
        limit: int = 10,
        min_importance: float = 0.1,
        use_keyword: bool = True,
        use_semantic: bool = True,
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
            use_semantic: Enable semantic/TF-IDF cosine similarity signal
            use_graph: Enable graph signal
            use_temporal: Enable temporal signal

        Returns:
            HybridSearchResult with merged, ranked results
        """
        _validate_input(query, user_id)

        keyword_ranking: Dict[str, int] = {}
        semantic_ranking: Dict[str, int] = {}
        graph_ranking: Dict[str, int] = {}
        temporal_ranking: Dict[str, int] = {}
        all_ids: Set[str] = set()

        # Single connection for all sub-searches
        conn = self._get_connection()
        try:
            if use_keyword:
                keyword_ranking = self._keyword_search(
                    conn, query, user_id, tenant_id, limit * 3
                )
                all_ids.update(keyword_ranking.keys())

            if use_semantic:
                semantic_ranking = self._semantic_search(
                    conn, query, user_id, tenant_id, limit * 3
                )
                all_ids.update(semantic_ranking.keys())

            if use_graph:
                graph_ranking = self._graph_search(
                    conn, query, user_id, tenant_id, limit * 3
                )
                all_ids.update(graph_ranking.keys())

            if use_temporal:
                temporal_ranking = self._temporal_search(
                    conn, user_id, tenant_id, limit * 3, min_importance
                )
                all_ids.update(temporal_ranking.keys())

            if not all_ids:
                return HybridSearchResult(
                    results=[],
                    total_candidates=0,
                    signal_counts={
                        'keyword': len(keyword_ranking),
                        'semantic': len(semantic_ranking),
                        'graph': len(graph_ranking),
                        'temporal': len(temporal_ranking),
                    },
                )

            # Merge using RRF
            merged = self._reciprocal_rank_fusion(
                keyword_ranking, semantic_ranking, graph_ranking, temporal_ranking
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
                content=mem['content'],
                category=mem.get('category'),
                importance=mem.get('importance', 0.5),
                strength_trend=mem.get('strength_trend'),
                created_at=mem['created_at'],
                combined_score=rrf_score,
                source_signals=[],
            )

            if memory_id in keyword_ranking:
                result.keyword_score = self._rank_to_score(
                    keyword_ranking[memory_id], len(keyword_ranking)
                )
                result.source_signals.append('keyword')
            if memory_id in semantic_ranking:
                result.semantic_score = self._rank_to_score(
                    semantic_ranking[memory_id], len(semantic_ranking)
                )
                result.source_signals.append('semantic')
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
                'semantic': len(semantic_ranking),
                'graph': len(graph_ranking),
                'temporal': len(temporal_ranking),
            },
        )

    def _keyword_search(
        self,
        conn: sqlite3.Connection,
        query: str,
        user_id: str,
        tenant_id: str,
        limit: int,
    ) -> Dict[str, int]:
        """
        Keyword search with simple tf-idf-like scoring.

        Returns dict of {memory_id: rank} (1-based, lower = better).
        """
        terms = query.lower().split()
        if not terms:
            return {}

        # Escape LIKE metacharacters to prevent wildcard injection
        escaped_terms = [_escape_like(t) for t in terms]

        like_clauses = " OR ".join(
            ["content LIKE ? ESCAPE '!'" for _ in terms]
        )
        params = [user_id, tenant_id] + [f"%{t}%" for t in escaped_terms]

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
            score = tf / doc_len
            scored.append((mid, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return {mid: rank + 1 for rank, (mid, _) in enumerate(scored)}

    def _semantic_search(
        self,
        conn: sqlite3.Connection,
        query: str,
        user_id: str,
        tenant_id: str,
        limit: int,
    ) -> Dict[str, int]:
        """
        Semantic search using TF-IDF cosine similarity.

        Builds TF-IDF vectors for all user memories and the query,
        then ranks memories by cosine similarity to the query.
        Pure Python implementation -- no external ML dependencies.

        Returns dict of {memory_id: rank} (1-based, lower = better).
        """
        terms = query.lower().split()
        if not terms:
            return {}

        cursor = conn.execute("""
            SELECT id, content
            FROM memories
            WHERE user_id = ? AND tenant_id = ?
            AND is_ghost = 0
        """, (user_id, tenant_id))

        rows = cursor.fetchall()
        if not rows:
            return {}

        # Tokenize all documents
        docs: Dict[str, List[str]] = {}
        for row in rows:
            mid, content = row
            docs[mid] = content.lower().split()

        n_docs = len(docs)
        if n_docs == 0:
            return {}

        # Build document frequency map (how many docs contain each term)
        doc_freq: Dict[str, int] = {}
        for tokens in docs.values():
            seen: Set[str] = set(tokens)
            for token in seen:
                doc_freq[token] = doc_freq.get(token, 0) + 1

        # Compute IDF for each term: log(N / df)
        idf: Dict[str, float] = {}
        for term, df in doc_freq.items():
            idf[term] = math.log(n_docs / df) if df > 0 else 0.0

        def _tfidf_vector(tokens: List[str]) -> Dict[str, float]:
            """Build a TF-IDF vector (sparse dict) for a token list."""
            tf_counts: Dict[str, int] = {}
            for token in tokens:
                tf_counts[token] = tf_counts.get(token, 0) + 1
            total = len(tokens) if tokens else 1
            vec: Dict[str, float] = {}
            for term, count in tf_counts.items():
                tf = count / total
                vec[term] = tf * idf.get(term, 0.0)
            return vec

        def _cosine_similarity(
            vec_a: Dict[str, float], vec_b: Dict[str, float]
        ) -> float:
            """Compute cosine similarity between two sparse vectors."""
            common = set(vec_a.keys()) & set(vec_b.keys())
            if not common:
                return 0.0
            dot = sum(vec_a[k] * vec_b[k] for k in common)
            norm_a = math.sqrt(sum(v * v for v in vec_a.values()))
            norm_b = math.sqrt(sum(v * v for v in vec_b.values()))
            if norm_a == 0.0 or norm_b == 0.0:
                return 0.0
            return dot / (norm_a * norm_b)

        # Build query vector
        query_vector = _tfidf_vector(terms)

        # Score each document by cosine similarity to query
        scored: List[tuple] = []
        for mid, tokens in docs.items():
            doc_vector = _tfidf_vector(tokens)
            sim = _cosine_similarity(query_vector, doc_vector)
            scored.append((mid, sim))

        scored.sort(key=lambda x: x[1], reverse=True)

        # Only return documents with non-zero similarity, up to limit
        ranked = {mid: rank + 1 for rank, (mid, sim) in enumerate(scored)
                  if sim > 0.0}
        if limit and len(ranked) > limit:
            ranked = dict(list(ranked.items())[:limit])
        return ranked

    def _graph_search(
        self,
        conn: sqlite3.Connection,
        query: str,
        user_id: str,
        tenant_id: str,
        limit: int,
    ) -> Dict[str, int]:
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
        like_clauses = " OR ".join(
            ["e.name LIKE ? ESCAPE '!'" for _ in terms]
        )
        params = [user_id] + [f"%{t}%" for t in escaped_terms]

        cursor = conn.execute(f"""
            SELECT DISTINCT e.id
            FROM memory_entities e
            WHERE e.user_id = ?
            AND ({like_clauses})
        """, params)

        entity_ids = [row[0] for row in cursor.fetchall()]

        if not entity_ids:
            return {}

        # Find memories linked to these entities
        # Filter by tenant via memories table join
        entity_placeholders = ','.join('?' * len(entity_ids))
        cursor = conn.execute(f"""
            SELECT mel.memory_id, COUNT(DISTINCT mel.entity_id) as entity_hits
            FROM memory_entity_links mel
            INNER JOIN memories m ON m.id = mel.memory_id
            AND m.tenant_id = ? AND m.user_id = ?
            WHERE mel.entity_id IN ({entity_placeholders})
            AND mel.user_id = ?
            GROUP BY mel.memory_id
            ORDER BY entity_hits DESC
            LIMIT ?
        """, [tenant_id, user_id] + entity_ids + [user_id, limit])

        rows = cursor.fetchall()
        return {mid: rank + 1 for rank, (mid, _) in enumerate(rows)}

    def _temporal_search(
        self,
        conn: sqlite3.Connection,
        user_id: str,
        tenant_id: str,
        limit: int,
        min_importance: float,
    ) -> Dict[str, int]:
        """
        Temporal search: rank memories by recency and importance.

        Query-independent signal -- returns most important/recent memories
        to provide recency context to the fusion.

        Returns dict of {memory_id: rank} (1-based, lower = better).
        """
        cursor = conn.execute("""
            SELECT id, importance, created_at, activation_count,
                   COALESCE(strength_trend, 'stable')
            FROM memories
            WHERE user_id = ? AND tenant_id = ?
            AND importance >= ?
            ORDER BY importance DESC, created_at DESC
            LIMIT ?
        """, (user_id, tenant_id, min_importance, limit))

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
                hours_old = 168.0

            time_score = pow(0.5, hours_old / 168.0)
            activation_boost = 1 + ((activation_count or 0) * 0.05)
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

    def _reciprocal_rank_fusion(
        self,
        keyword: Dict[str, int],
        semantic: Dict[str, int],
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
        all_ids.update(semantic.keys())
        all_ids.update(graph.keys())
        all_ids.update(temporal.keys())

        scores: Dict[str, float] = {}

        for mid in all_ids:
            score = 0.0

            if mid in keyword:
                score += self.weights['keyword'] / (
                    self.RRF_K + keyword[mid]
                )
            if mid in semantic:
                score += self.weights['semantic'] / (
                    self.RRF_K + semantic[mid]
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
        conn: sqlite3.Connection,
        memory_ids: List[str],
        user_id: str,
        tenant_id: str,
    ) -> Dict[str, dict]:
        """Fetch memory data for given IDs."""
        if not memory_ids:
            return {}

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


# Global instance management (thread-safe)
_hybrid_retriever: Optional[HybridRetriever] = None
_retriever_lock = threading.Lock()


def get_hybrid_retriever(
    db_path: Optional[str] = None,
    weights: Optional[Dict[str, float]] = None,
) -> HybridRetriever:
    """Get or create global hybrid retriever instance (thread-safe)."""
    global _hybrid_retriever
    with _retriever_lock:
        if _hybrid_retriever is None:
            if db_path is None:
                from .config import DB_PATH
                db_path = DB_PATH
            _hybrid_retriever = HybridRetriever(db_path, weights)
        return _hybrid_retriever


def reset_hybrid_retriever() -> None:
    """Reset global hybrid retriever (for testing)."""
    global _hybrid_retriever
    with _retriever_lock:
        _hybrid_retriever = None
