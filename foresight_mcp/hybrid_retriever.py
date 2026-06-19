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
import threading
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar

from .backend.base import DatabaseBackend
from .config import DB_PATH
from .connection_pool import get_pool

logger = logging.getLogger("foresight_hybrid_retriever")

FAST_PATH_MAX_CACHE_SIZE = 128
FAST_PATH_EARLY_TERMINATION_RATIO = 2.0
FAST_PATH_MIN_CANDIDATES = 2


def _hours_since(iso_timestamp: str) -> float:
    """Compute hours elapsed between an ISO 8601 timestamp and now."""
    try:
        created = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
        return max((datetime.now(timezone.utc) - created).total_seconds() / 3600, 0.01)
    except (ValueError, AttributeError):
        return 168.0


def _normalize_query(query: str) -> str:
    stripped = query.strip().lower()
    if not stripped:
        return ""
    parts = stripped.split()
    seen: set[str] = set()
    unique: list[str] = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return " ".join(unique)


@dataclass
class HybridSearchOptions:
    """Options for hybrid search configuration."""

    tenant_id: str = "default"
    limit: int = 10
    min_importance: float = 0.1
    use_keyword: bool = True
    use_tfidf_cosine: bool = True
    use_semantic: bool | None = None
    use_graph: bool = True
    use_temporal: bool = True
    fast_path_enabled: bool = True


@dataclass
class Rankings:
    """Bundled per-signal rankings and metadata for _build_results."""

    keyword: dict[str, int] = field(default_factory=dict)
    tfidf_cosine: dict[str, int] = field(default_factory=dict)
    graph: dict[str, int] = field(default_factory=dict)
    temporal: dict[str, int] = field(default_factory=dict)
    semantic_label: str = "tfidf_cosine"
    # Entity salience: per-memory entity hit count and avg confidence
    entity_hits: dict[str, int] = field(default_factory=dict)
    entity_confidence: dict[str, float] = field(default_factory=dict)


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

    # Entity salience metadata
    entity_hits: int = 0
    entity_confidence_avg: float = 0.0

    # Decay metadata (debug only — not used in scoring)
    decay_multiplier: float = 1.0
    temporal_category: str = ""

    def to_dict(self) -> dict:
        d: dict[str, object] = {
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
            "entity_hits": self.entity_hits,
            "entity_confidence_avg": round(self.entity_confidence_avg, 4),
            "decay_multiplier": round(self.decay_multiplier, 4),
        }
        temporal_category = self.temporal_category
        if temporal_category:
            d["temporal_category"] = temporal_category
        return d


@dataclass
class HybridSearchResult:
    """Complete result from a hybrid search."""

    results: list[HybridResult]
    total_candidates: int
    signal_counts: dict[str, int | str] = field(default_factory=dict)

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
    # temporal=0.8 (recency context with decay-aware scoring)
    DEFAULT_WEIGHTS: ClassVar[dict[str, float]] = {
        "keyword": 1.0,
        "semantic": 0.7,
        "graph": 0.8,
        "temporal": 0.8,
    }

    # Trend modifiers applied to temporal score per strength_trend value.
    # These can be overridden via `weights["trend_mod_strengthening"]` etc.
    DEFAULT_TREND_MODS: ClassVar[dict[str, float]] = {
        "strengthening": 1.2,
        "stable": 1.0,
        "weakening": 0.8,
        "stale": 0.5,
    }

    # Category half-life multipliers (applied on top of base half-life).
    # Categories not listed use multiplier=1.0.
    DEFAULT_CATEGORY_MULTIPLIERS: ClassVar[dict[str, float]] = {
        "session": 0.5,  # session memories decay twice as fast
        "fact": 1.0,
        "preference": 1.5,  # preferences decay more slowly
        "trait": 2.0,  # traits/personality decay the slowest
    }

    def __init__(
        self,
        db_path: str,
        weights: dict[str, float] | None = None,
        backend: DatabaseBackend | None = None,
    ):
        self.db_path = db_path
        self._backend = backend
        merged = self.DEFAULT_WEIGHTS.copy()
        if weights:
            merged.update(weights)
        if "semantic" not in merged and "tfidf_cosine" in merged:
            merged["semantic"] = merged["tfidf_cosine"]
        if "tfidf_cosine" not in merged and "semantic" in merged:
            merged["tfidf_cosine"] = merged["semantic"]
        self.weights = merged

        # Trend modifiers: read from weights or fall back to defaults.
        # Prefix each trend key with "trend_mod_" for explicit override.
        self.trend_mods: dict[str, float] = {}
        for trend in ("strengthening", "stable", "weakening", "stale"):
            weight_key = f"trend_mod_{trend}"
            self.trend_mods[trend] = self.weights.get(weight_key, self.DEFAULT_TREND_MODS[trend])

        # Category half-life multipliers.
        self.category_multipliers: dict[str, float] = dict(self.DEFAULT_CATEGORY_MULTIPLIERS)
        for cat_key in self.DEFAULT_CATEGORY_MULTIPLIERS:
            weight_key = f"category_mult_{cat_key}"
            if weight_key in self.weights:
                self.category_multipliers[cat_key] = self.weights[weight_key]

        # TF-IDF cache: maps (user_id, tenant_id) -> {"idf": dict, "doc_count": int,
        #   "docs": dict[id, list[str]]}
        # Invalidated when doc_count changes (new/deleted memories).
        self._tfidf_cache: dict[tuple[str, str], dict] = {}
        self._tfidf_cache_lock = threading.Lock()
        self._schema_cache: dict[str, set[str]] = {}
        self._result_cache: OrderedDict[tuple[str, str, str], Any] = OrderedDict()
        self._result_cache_lock = threading.Lock()

        # Entity metadata side-channel (set by _run_graph_search, consumed by _build_results)
        self._entity_hits: dict[str, int] = {}
        self._entity_confidence: dict[str, float] = {}

    def _get_connection(self) -> Any:
        """Get a database connection with WAL mode for concurrent safety."""
        pool = get_pool(self.db_path)
        conn = pool.acquire()
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _fetch_rows(self, sql: str, params: tuple | list | dict = ()) -> list[dict]:
        """Execute a SELECT and return rows as dicts (backend-agnostic)."""
        if self._backend is not None:
            _p: tuple | dict = tuple(params) if isinstance(params, list) else params  # type: ignore[arg-type]
            return self._backend.fetch(sql, _p)
        conn = self._get_connection()
        try:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def _execute_sql(self, sql: str, params: tuple | list | dict = ()) -> None:
        """Execute a write query with auto-commit (backend-agnostic)."""
        if self._backend is not None:
            _p: tuple | dict = tuple(params) if isinstance(params, list) else params  # type: ignore[arg-type]
            self._backend.execute(sql, _p)
        else:
            conn = self._get_connection()
            try:
                conn.execute(sql, params)
                conn.commit()
            finally:
                conn.close()

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        """Context manager yielding a raw connection (backend-agnostic).

        For multi-step operations (transactions, PRAGMA introspection).
        When a backend is provided, delegates to ``backend.connection()``.
        When falling back to SQLite, acquires from the pool.
        """
        if self._backend is not None:
            with self._backend.connection() as conn:
                yield conn
        else:
            conn = self._get_connection()
            try:
                yield conn
            finally:
                conn.close()

    def _detect_columns(self, table_name: str) -> set[str]:
        """Return the set of column names for a table (cached + backend-agnostic)."""
        if table_name in self._schema_cache:
            return self._schema_cache[table_name]

        if self._backend is not None:
            # Backend path: assume standard schema
            if table_name == "memory_entities":
                cols: set[str] = {
                    "id",
                    "user_id",
                    "tenant_id",
                    "name",
                    "entity_type",
                    "description",
                    "properties",
                    "created_at",
                    "updated_at",
                    "confidence",
                }
            elif table_name == "memory_entity_links":
                cols = {
                    "memory_id",
                    "entity_id",
                    "tenant_id",
                    "user_id",
                    "relevance_score",
                    "created_at",
                }
            else:
                cols = set()
        else:
            with self._connection() as conn:
                rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
                cols = {row[1] for row in rows}

        self._schema_cache[table_name] = cols
        return cols

    def _cache_key(self, query: str, user_id: str, tenant_id: str) -> tuple[str, str, str]:
        return (_normalize_query(query), user_id, tenant_id)

    def _get_cached_result(self, cache_key: tuple[str, str, str]) -> Any | None:
        with self._result_cache_lock:
            if cache_key in self._result_cache:
                result = self._result_cache[cache_key]
                self._result_cache.move_to_end(cache_key)
                return result
        return None

    def _cache_result(self, cache_key: tuple[str, str, str], result: Any) -> None:
        with self._result_cache_lock:
            if cache_key in self._result_cache:
                self._result_cache.move_to_end(cache_key)
            else:
                self._result_cache[cache_key] = result
                while len(self._result_cache) > FAST_PATH_MAX_CACHE_SIZE:
                    self._result_cache.popitem(last=False)

    def search(
        self,
        query: str,
        user_id: str,
        options: HybridSearchOptions | None = None,
        **kwargs: Any,
    ) -> HybridSearchResult:
        """
        Execute hybrid search combining all enabled signals.

        Args:
            query: Search query string
            user_id: User ID
            options: Hybrid search options (if None, uses defaults)
            **kwargs: Backward-compatible keyword arguments (limit, use_keyword, etc.)

        Returns:
            HybridSearchResult with merged, ranked results
        """
        # Handle default options + backward compat kwargs
        if options is None:
            options = HybridSearchOptions(**kwargs)

        # Validate input
        _validate_input(query, user_id)

        # Handle use_semantic as backward-compatible alias for use_tfidf_cosine
        use_tfidf_cosine = options.use_tfidf_cosine
        semantic_label = "tfidf_cosine"
        if options.use_semantic is not None:
            use_tfidf_cosine = options.use_semantic
            semantic_label = "semantic"

        # Extract search parameters from options
        tenant_id = options.tenant_id
        limit = options.limit
        min_importance = options.min_importance
        use_keyword = options.use_keyword
        use_graph = options.use_graph
        use_temporal = options.use_temporal
        fast_path_enabled = options.fast_path_enabled

        cache_key = self._cache_key(query, user_id, tenant_id)

        if fast_path_enabled:
            cached = self._get_cached_result(cache_key)
            if cached is not None:
                cached.signal_counts["fast_path"] = "cache"
                return cached

        keyword_ranking = self._run_keyword_search(query, user_id, tenant_id, limit) if use_keyword else {}
        tfidf_cosine_ranking = self._run_tfidf_search(query, user_id, tenant_id, limit) if use_tfidf_cosine else {}
        graph_ranking = self._run_graph_search(query, user_id, tenant_id, limit) if use_graph else {}
        temporal_ranking = self._run_temporal_search(user_id, tenant_id, limit, min_importance) if use_temporal else {}

        rankings = Rankings(
            keyword=keyword_ranking,
            tfidf_cosine=tfidf_cosine_ranking,
            graph=graph_ranking,
            temporal=temporal_ranking,
            semantic_label=semantic_label,
            entity_hits=dict(self._entity_hits),
            entity_confidence=dict(self._entity_confidence),
        )

        all_ids = set()
        all_ids.update(keyword_ranking.keys())
        all_ids.update(tfidf_cosine_ranking.keys())
        all_ids.update(graph_ranking.keys())
        all_ids.update(temporal_ranking.keys())

        if not all_ids:
            result = HybridSearchResult(
                results=[],
                total_candidates=0,
                signal_counts={
                    "keyword": len(rankings.keyword),
                    "tfidf_cosine": len(rankings.tfidf_cosine),
                    "graph": len(rankings.graph),
                    "temporal": len(rankings.temporal),
                },
            )
            if fast_path_enabled:
                self._cache_result(cache_key, result)
            return result

        merged = self._reciprocal_rank_fusion(keyword_ranking, tfidf_cosine_ranking, graph_ranking, temporal_ranking)

        early = self._try_early_termination(merged, rankings, user_id, options)
        if early is not None:
            self._cache_result(cache_key, early)
            return early

        top_ids = [mid for mid, _ in merged[:limit]]
        memories = self._fetch_memories_for_top_ids(top_ids, user_id, tenant_id)

        results = self._build_results(
            merged,
            memories,
            limit,
            rankings,
        )

        result = self._make_search_result(results, all_ids, rankings)
        if fast_path_enabled:
            self._cache_result(cache_key, result)
        return result

    def _make_search_result(
        self,
        results: list[HybridResult],
        all_ids: set[str],
        rankings: Rankings,
        extra_signals: dict[str, int | str] | None = None,
    ) -> HybridSearchResult:
        """Construct a HybridSearchResult with standard signal counts."""
        signal_counts: dict[str, int | str] = {
            "keyword": len(rankings.keyword),
            "tfidf_cosine": len(rankings.tfidf_cosine),
            "semantic": len(rankings.tfidf_cosine),
            "graph": len(rankings.graph),
            "temporal": len(rankings.temporal),
        }
        if extra_signals:
            signal_counts.update(extra_signals)
        return HybridSearchResult(results=results, total_candidates=len(all_ids), signal_counts=signal_counts)

    def _try_early_termination(
        self,
        merged: list[tuple],
        rankings: Rankings,
        user_id: str,
        options: HybridSearchOptions,
    ) -> HybridSearchResult | None:
        """Return an early-termination result if conditions are met, else None."""
        if not (
            options.fast_path_enabled
            and options.use_keyword
            and not options.use_tfidf_cosine
            and not options.use_graph
            and not options.use_temporal
            and len(rankings.keyword) >= options.limit
            and len(merged) >= FAST_PATH_MIN_CANDIDATES
        ):
            return None
        top_score = merged[0][1]
        second_score = merged[1][1]
        if top_score < FAST_PATH_EARLY_TERMINATION_RATIO * second_score:
            return None
        all_ids = set(rankings.keyword) | set(rankings.tfidf_cosine) | set(rankings.graph) | set(rankings.temporal)
        top_ids = [mid for mid, _ in merged[: options.limit]]
        memories = self._fetch_memories_for_top_ids(top_ids, user_id, options.tenant_id)
        results = self._build_results(merged, memories, options.limit, rankings)
        return self._make_search_result(results, all_ids, rankings, extra_signals={"fast_path": "early_termination"})

    def _keyword_search(
        self,
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

        rows = self._fetch_rows(
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

        # Score by term frequency
        scored = []
        for row in rows:
            mid = row["id"]
            content = row["content"]
            content_lower = content.lower()
            tf = sum(content_lower.count(t) for t in terms)
            doc_len = max(len(content_lower.split()), 1)
            score = tf / doc_len
            scored.append((mid, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return {mid: rank + 1 for rank, (mid, _) in enumerate(scored)}

    def _build_tfidf_cache(
        self,
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

        with self._tfidf_cache_lock:
            cached = self._tfidf_cache.get(cache_key)

        if cached is not None:
            count_rows = self._fetch_rows(
                "SELECT COUNT(*) as cnt FROM memories WHERE user_id = ? AND tenant_id = ? AND is_ghost = 0",
                (user_id, tenant_id),
            )
            current_count = count_rows[0]["cnt"] if count_rows else 0
            if cached["doc_count"] == current_count:
                return cached

        rows = self._fetch_rows(
            "SELECT id, content FROM memories WHERE user_id = ? AND tenant_id = ? AND is_ghost = 0",
            (user_id, tenant_id),
        )

        docs: dict[str, list[str]] = {row["id"]: row["content"].lower().split() for row in rows}
        n_docs = len(docs)

        doc_freq: dict[str, int] = {}
        for tokens in docs.values():
            for token in set(tokens):
                doc_freq[token] = doc_freq.get(token, 0) + 1

        idf: dict[str, float] = {term: math.log(n_docs / df) if df > 0 else 0.0 for term, df in doc_freq.items()}

        count_rows2 = self._fetch_rows(
            "SELECT COUNT(*) as cnt FROM memories WHERE user_id = ? AND tenant_id = ? AND is_ghost = 0",
            (user_id, tenant_id),
        )
        current_count = count_rows2[0]["cnt"] if count_rows2 else 0

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

        corpus = self._build_tfidf_cache(user_id, tenant_id)
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
        query: str,
        user_id: str,
        tenant_id: str,
        limit: int,
    ) -> dict[str, int]:
        """Backward-compatible entrypoint for semantic search alias."""
        return self._tfidf_cosine_search(query, user_id, tenant_id, limit)

    def _graph_search(
        self,
        query: str,
        user_id: str,
        tenant_id: str,
        limit: int,
    ) -> tuple[dict[str, int], dict[str, int], dict[str, float]]:
        """
        Graph-based search: find entities matching query, traverse to
        find related memories with confidence-weighted scoring.

        Scoring factors:
            1. Number of distinct matching entities per memory
            2. Entity confidence (higher confidence = stronger signal)
            3. Edge decay_factor (fresher relationships weighted more)
            4. Link relevance_score (context-specific importance of the
                memory-entity association)

        Returns:
            Tuple of (rankings, entity_hits, entity_confidence):
            - rankings: {memory_id: rank} 1-based, lower=better
            - entity_hits: {memory_id: int} matched entities count
            - entity_confidence: {memory_id: float} avg entity confidence
        """
        terms = query.lower().split()
        if not terms:
            return ({}, {}, {})

        escaped_terms = [_escape_like(t) for t in terms]

        like_clauses = " OR ".join(["e.name LIKE ? ESCAPE '!'" for _ in terms])
        entity_cols = self._detect_columns("memory_entities")

        has_confidence = "confidence" in entity_cols
        has_tenant = "tenant_id" in entity_cols

        if has_tenant:
            sql = f"""
                SELECT DISTINCT e.id
                {", e.confidence" if has_confidence else ""}
                FROM memory_entities e
                WHERE e.user_id = ? AND e.tenant_id = ?
                  AND ({like_clauses})
            """
            params: list[Any] = [user_id, tenant_id] + [f"%{t}%" for t in escaped_terms]
        else:
            sql = f"""
                SELECT DISTINCT e.id
                {", e.confidence" if has_confidence else ""}
                FROM memory_entities e
                WHERE e.user_id = ?
                  AND ({like_clauses})
            """
            params = [user_id] + [f"%{t}%" for t in escaped_terms]

        entity_rows = self._fetch_rows(sql, params)

        if not entity_rows:
            return ({}, {}, {})

        entity_ids: list[str] = []
        entity_conf: dict[str, float] = {}
        for row in entity_rows:
            eid = row["id"]
            conf = float(row["confidence"]) if has_confidence and row.get("confidence") is not None else 1.0
            entity_ids.append(eid)
            entity_conf[eid] = conf

        entity_placeholders = ",".join("?" * len(entity_ids))
        link_cols = self._detect_columns("memory_entity_links")
        has_relevance = "relevance_score" in link_cols

        relevance_col = ", COALESCE(AVG(mel.relevance_score), 1.0) as avg_relevance" if has_relevance else ""
        rows = self._fetch_rows(
            f"""
            SELECT
                mel.memory_id,
                COUNT(DISTINCT mel.entity_id) as entity_hits,
                COALESCE(AVG(me.confidence), 1.0) as avg_entity_conf,
                COALESCE(AVG(er.confidence * er.decay_factor), 1.0) as avg_edge_quality{relevance_col}
            FROM memory_entity_links mel
            INNER JOIN memories m ON m.id = mel.memory_id
                AND m.tenant_id = ? AND m.user_id = ? AND m.is_ghost = 0
            LEFT JOIN memory_entities me ON me.id = mel.entity_id
                AND me.user_id = ?
            LEFT JOIN entity_relationships er ON (
                (er.source_entity_id = mel.entity_id OR er.target_entity_id = mel.entity_id)
                AND er.user_id = ?
            )
            WHERE mel.entity_id IN ({entity_placeholders})
            AND mel.user_id = ?
            GROUP BY mel.memory_id
            ORDER BY
                entity_hits DESC,
                avg_edge_quality DESC
            LIMIT ?
        """,
            [tenant_id, user_id, user_id, user_id, *entity_ids, user_id, limit],
        )

        if not rows:
            return ({}, {}, {})

        scored: list[tuple[str, float]] = []
        hits_map: dict[str, int] = {}
        conf_map: dict[str, float] = {}
        for row in rows:
            mid = row["memory_id"]
            entity_hits = row.get("entity_hits") or 1
            avg_entity_conf = row.get("avg_entity_conf") or 1.0
            avg_edge_quality = row.get("avg_edge_quality") or 1.0
            avg_relevance = row.get("avg_relevance", 1.0) if has_relevance else 1.0
            graph_score = entity_hits * avg_entity_conf * avg_edge_quality * avg_relevance
            scored.append((mid, graph_score))
            hits_map[mid] = entity_hits
            conf_map[mid] = avg_entity_conf

        scored.sort(key=lambda x: x[1], reverse=True)
        rankings = {mid: rank + 1 for rank, (mid, _) in enumerate(scored)}
        return (rankings, hits_map, conf_map)

    def _compute_burst_boost(self, activation_count: int, last_retrieved_at: str | None, now: datetime) -> float:
        """Compute a super-linear boost for recently-active memories.

        Uses a sqrt curve so that 1-2 activations give modest boost while
        many retrievals in a short window compound non-linearly. The boost
        is further amplified when the last retrieval was very recent.
        """
        if activation_count <= 0:
            return 1.0

        base_boost = 1.0 + (activation_count**0.5) * 0.1

        # Amplify if last retrieval was within the past hour (burst signal)
        if last_retrieved_at:
            try:
                last_ret = datetime.fromisoformat(last_retrieved_at.replace("Z", "+00:00"))
                hours_since_retrieval = max((now - last_ret).total_seconds() / 3600, 0.0)
                if hours_since_retrieval < 1.0:
                    # Burst: recent retrieval within the hour — extra 1.5x max
                    burst_factor = 1.0 + (1.0 - hours_since_retrieval) * 0.5
                    base_boost *= burst_factor
            except (ValueError, AttributeError):
                pass

        return min(base_boost, 3.0)  # cap at 3x

    def _compute_time_score(self, hours_old: float, category: str | None) -> float:
        """Compute exponential decay time score with category-aware half-life."""
        half_life = 168.0  # base: 1 week
        if category:
            cat_mult = self.category_multipliers.get(category, 1.0)
            half_life *= cat_mult
        return pow(0.5, max(0.0, hours_old) / half_life)

    STALE_DAYS = 90  # memories older than this are "stale"
    RECENT_HOURS = 24  # memories created within this window are "recent"

    @classmethod
    def _classify_temporal_category(cls, hours_old: float, category: str | None, strength_trend: str | None) -> str:
        """Classify a memory into a temporal bucket for debug transparency.

        Categories are derived from existing metadata (no manual tagging):
          - current_state: user preferences and traits (stable identity signals)
          - recent_activity: created within the last RECENT_HOURS
          - future_plan: pending items (action-oriented, forward-looking)
          - stale: strength_trend == 'stale' OR older than STALE_DAYS
          - historical: everything else
        """
        if category in ("preference", "trait"):
            return "current_state"
        if hours_old <= cls.RECENT_HOURS:
            return "recent_activity"
        if category == "pending":
            return "future_plan"
        if strength_trend == "stale" or hours_old >= cls.STALE_DAYS * 24:
            return "stale"
        return "historical"

    def _temporal_search(
        self,
        user_id: str,
        tenant_id: str,
        limit: int,
        min_importance: float,
    ) -> dict[str, int]:
        """
        Temporal search: rank memories by recency, importance, and decay state.

        Query-independent signal that provides recency context to the fusion.
        Uses three components:
        1. current_strength (from decay model) OR importance as baseline
        2. Exponential recency decay with category-aware half-life
        3. Activation burst boost for recently-retrieved memories

        Returns dict of {memory_id: rank} (1-based, lower = better).
        """
        rows = self._fetch_rows(
            """
            SELECT id, importance, current_strength, created_at,
                   activation_count, COALESCE(strength_trend, 'stable') as strength_trend,
                   last_retrieved_at, category
            FROM memories
            WHERE user_id = ? AND tenant_id = ?
            AND importance >= ?
            AND is_ghost = 0
            ORDER BY created_at DESC
            LIMIT ?
        """,
            (user_id, tenant_id, min_importance, limit),
        )

        if not rows:
            return {}

        now = datetime.now(timezone.utc)
        scored = []

        for row in rows:
            mid = row["id"]
            importance = row["importance"]
            current_strength = row["current_strength"]
            created_at = row["created_at"]
            activation_count = row["activation_count"]
            trend = row["strength_trend"]
            last_retrieved_at = row["last_retrieved_at"]
            category = row["category"]

            try:
                created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                hours_old = max((now - created).total_seconds() / 3600, 0.01)
            except (ValueError, AttributeError):
                hours_old = 168.0

            # 1. Baseline: use current_strength if available (from decay model),
            #    otherwise fall back to creator-set importance.
            strength = current_strength if current_strength is not None else importance

            # 2. Exponential recency with category-aware half-life
            time_score = self._compute_time_score(hours_old, category)

            # 3. Activation burst boost
            burst_boost = self._compute_burst_boost(activation_count or 0, last_retrieved_at, now)

            # 4. Trend modifier
            trend_mod = self.trend_mods.get(trend, 1.0)

            score = min(1.0, strength * time_score * burst_boost * trend_mod)
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
        memory_ids: list[str],
        user_id: str,
        tenant_id: str,
    ) -> dict[str, dict]:
        """Fetch memory data for given IDs."""
        if not memory_ids:
            return {}

        placeholders = ",".join("?" * len(memory_ids))
        rows = self._fetch_rows(
            f"""
            SELECT id, content, category, importance,
                   strength_trend, created_at, current_strength
            FROM memories
            WHERE id IN ({placeholders})
            AND user_id = ? AND tenant_id = ?
        """,
            [*memory_ids, user_id, tenant_id],
        )

        return {
            row["id"]: {
                "content": row["content"],
                "category": row["category"],
                "importance": row["importance"] or 0.5,
                "strength_trend": row["strength_trend"],
                "created_at": row["created_at"],
                "current_strength": row["current_strength"],
            }
            for row in rows
        }

    def _run_keyword_search(self, query: str, user_id: str, tenant_id: str, limit: int) -> dict[str, int]:
        """Run keyword search and return ranking dict."""
        return self._keyword_search(query, user_id, tenant_id, limit * 3)

    def _run_tfidf_search(self, query: str, user_id: str, tenant_id: str, limit: int) -> dict[str, int]:
        """Run TF-IDF search and return ranking dict."""
        return self._tfidf_cosine_search(query, user_id, tenant_id, limit * 3)

    def _run_graph_search(self, query: str, user_id: str, tenant_id: str, limit: int) -> dict[str, int]:
        """Run graph search and return ranking dict with entity metadata side-channel."""
        rankings, hits, conf = self._graph_search(query, user_id, tenant_id, limit * 3)
        self._entity_hits = hits
        self._entity_confidence = conf
        return rankings

    def _run_temporal_search(self, user_id: str, tenant_id: str, limit: int, min_importance: float) -> dict[str, int]:
        """Run temporal search and return ranking dict."""
        return self._temporal_search(user_id, tenant_id, limit * 3, min_importance)

    def _build_results(
        self,
        merged: list[tuple],
        memories: dict[str, dict],
        limit: int,
        rankings: Rankings,
    ) -> list[HybridResult]:
        """Build HybridResult objects from merged rankings."""
        results = []
        for memory_id, rrf_score in merged[:limit]:
            mem = memories.get(memory_id)
            if not mem:
                continue

            importance = mem.get("importance", 0.5)
            current_strength = mem.get("current_strength")

            created_at = mem["created_at"]
            hours_old = _hours_since(created_at) if created_at else 0.0
            strength_trend = mem.get("strength_trend")
            category = mem.get("category")
            temporal_category = self._classify_temporal_category(hours_old, category, strength_trend)

            result = HybridResult(
                memory_id=memory_id,
                content=mem["content"],
                category=category,
                importance=importance,
                strength_trend=strength_trend,
                created_at=created_at,
                combined_score=rrf_score,
                source_signals=[],
                temporal_category=temporal_category,
            )

            if memory_id in rankings.keyword:
                result.keyword_score = self._rank_to_score(rankings.keyword[memory_id], len(rankings.keyword))
                result.source_signals.append("keyword")
            if memory_id in rankings.tfidf_cosine:
                result.semantic_score = self._rank_to_score(
                    rankings.tfidf_cosine[memory_id], len(rankings.tfidf_cosine)
                )
                result.tfidf_cosine_score = self._rank_to_score(
                    rankings.tfidf_cosine[memory_id], len(rankings.tfidf_cosine)
                )
                result.source_signals.append(rankings.semantic_label)
            if memory_id in rankings.graph:
                result.graph_score = self._rank_to_score(rankings.graph[memory_id], len(rankings.graph))
                result.source_signals.append("graph")
                result.entity_hits = rankings.entity_hits.get(memory_id, 0)
                result.entity_confidence_avg = rankings.entity_confidence.get(memory_id, 0.0)
            if memory_id in rankings.temporal:
                result.temporal_score = self._rank_to_score(rankings.temporal[memory_id], len(rankings.temporal))
                result.source_signals.append("temporal")

            # Cross-cutting decay multiplier: penalize memories whose current strength
            # has decayed below their original importance. This applies decay as a
            # post-RRF factor so it affects ALL signals, not just temporal.
            # Floor at 0.2 so even heavily decayed memories can still surface
            # when strongly relevant (decay/reinforcement cannot fully suppress).
            decay_multiplier = 1.0
            if current_strength is not None and importance > 0:
                ratio = current_strength / importance
                decay_multiplier = max(0.2, min(1.0, ratio))
            result.combined_score = rrf_score * decay_multiplier
            result.decay_multiplier = decay_multiplier

            results.append(result)

        # Re-sort by decay-adjusted combined score
        results.sort(key=lambda r: r.combined_score, reverse=True)
        return results

    def _fetch_memories_for_top_ids(
        self,
        top_ids: list[str],
        user_id: str,
        tenant_id: str,
    ) -> dict[str, dict]:
        """Fetch memory data for top IDs using a temporary connection."""
        if not top_ids:
            return {}

        return self._fetch_memories(top_ids, user_id, tenant_id)


# Global instance management (thread-safe)
class _HybridRetrieverSingleton:
    """Module-level singleton for HybridRetriever."""

    _instance: HybridRetriever | None = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(
        cls,
        db_path: str | None = None,
        weights: dict[str, float] | None = None,
        backend: DatabaseBackend | None = None,
    ) -> HybridRetriever:
        """Get or create global hybrid retriever instance (thread-safe)."""
        with cls._lock:
            if cls._instance is None:
                if db_path is None:
                    db_path = DB_PATH
                cls._instance = HybridRetriever(db_path, weights, backend)
            return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset global hybrid retriever (for testing)."""
        with cls._lock:
            cls._instance = None


def get_hybrid_retriever(
    db_path: str | None = None,
    weights: dict[str, float] | None = None,
    backend: DatabaseBackend | None = None,
) -> HybridRetriever:
    """Get or create global hybrid retriever instance (thread-safe)."""
    return _HybridRetrieverSingleton.get_instance(db_path, weights, backend)


def reset_hybrid_retriever() -> None:
    """Reset global hybrid retriever (for testing)."""
    _HybridRetrieverSingleton.reset()
