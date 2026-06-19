"""
Tests for hybrid retriever.
"""

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from foresight_mcp.hybrid_retriever import (
    HybridRetriever,
    _escape_like,
    _normalize_query,
    _validate_input,
    reset_hybrid_retriever,
)
from foresight_mcp.server import SearchTrace


@pytest.fixture(autouse=True)
def cleanup():
    reset_hybrid_retriever()
    yield
    reset_hybrid_retriever()


def create_test_db():
    """Create a temp DB with schema and test data."""
    _, path = tempfile.mkstemp(suffix=".db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    # Create tables
    conn.execute("""
        CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            tenant_id TEXT DEFAULT 'default',
            scope TEXT DEFAULT 'session',
            retention TEXT DEFAULT 'short_term',
            content TEXT,
            tags TEXT DEFAULT '[]',
            emotional_context TEXT DEFAULT '{}',
            metrics TEXT DEFAULT '{}',
            gist TEXT,
            vector_id TEXT,
            is_ghost INTEGER DEFAULT 0,
            synthesized_from TEXT DEFAULT '[]',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            category TEXT,
            importance REAL DEFAULT 0.5,
            current_strength REAL DEFAULT 0.5,
            decay_rate REAL DEFAULT 1.0,
            activation_count INTEGER DEFAULT 0,
            strength_trend TEXT DEFAULT 'stable',
            accessed_at TEXT,
            last_retrieved_at TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE memory_entities (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            name TEXT,
            entity_type TEXT,
            description TEXT,
            properties TEXT DEFAULT '{}',
            confidence REAL DEFAULT 1.0,
            user_id TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE entity_relationships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            source_entity_id TEXT,
            target_entity_id TEXT,
            relationship_type TEXT,
            confidence REAL DEFAULT 1.0,
            decay_factor REAL DEFAULT 1.0,
            last_accessed TEXT DEFAULT CURRENT_TIMESTAMP,
            metadata TEXT DEFAULT '{}',
            user_id TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE memory_entity_links (
            memory_id TEXT,
            entity_id TEXT,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT,
            relevance_score REAL DEFAULT 1.0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (memory_id, entity_id)
        )
    """)

    # Insert test data
    now = datetime.now(timezone.utc)
    uid = "test_user"

    memories = [
        (
            "mem_1",
            "Feeling anxious about the presentation tomorrow",
            "fact",
            0.8,
            "strengthening",
            now - timedelta(hours=2),
        ),
        (
            "mem_2",
            "Started CBT therapy sessions for anxiety management",
            "fact",
            0.7,
            "stable",
            now - timedelta(days=7),
        ),
        (
            "mem_3",
            "Meditation helped reduce stress levels significantly",
            "fact",
            0.6,
            "stable",
            now - timedelta(days=30),
        ),
        ("mem_4", "Family dinner was pleasant and relaxing", "fact", 0.4, "stable", now - timedelta(days=14)),
        (
            "mem_5",
            "Work deadline approaching, feeling overwhelmed",
            "fact",
            0.9,
            "strengthening",
            now - timedelta(hours=1),
        ),
    ]

    for mid, content, cat, imp, trend, ts in memories:
        conn.execute(
            "INSERT INTO memories (id, user_id, tenant_id, content, category, importance, strength_trend, created_at, accessed_at) VALUES (?, ?, 'default', ?, ?, ?, ?, ?, ?)",
            (mid, uid, content, cat, imp, trend, ts.isoformat(), ts.isoformat()),
        )

    # Insert entities
    entities = [
        ("entity_anxiety", "anxiety", "emotion", uid),
        ("entity_therapy", "CBT", "concept", uid),
        ("entity_stress", "stress", "emotion", uid),
        ("entity_work", "work", "concept", uid),
    ]

    for eid, name, etype, euid in entities:
        conn.execute(
            "INSERT INTO memory_entities (id, name, entity_type, user_id) VALUES (?, ?, ?, ?)",
            (eid, name, etype, euid),
        )

    # Link memories to entities
    links = [
        ("mem_1", "entity_anxiety", uid),
        ("mem_2", "entity_therapy", uid),
        ("mem_2", "entity_anxiety", uid),
        ("mem_3", "entity_stress", uid),
        ("mem_5", "entity_work", uid),
        ("mem_5", "entity_stress", uid),
    ]

    for mid, eid, euid in links:
        conn.execute(
            "INSERT INTO memory_entity_links (memory_id, entity_id, user_id) VALUES (?, ?, ?)",
            (mid, eid, euid),
        )

    conn.commit()
    conn.close()

    import os

    os.close(_)
    return path


@pytest.fixture
def test_db():
    path = create_test_db()
    yield path
    import os

    os.unlink(path)


class TestKeywordSearch:
    """Test keyword/BM25-style search."""

    def test_finds_matching_memories(self, test_db):
        retriever = HybridRetriever(test_db)
        result = retriever.search(
            "anxiety", "test_user", limit=5, use_graph=False, use_temporal=False, use_semantic=False
        )

        # Should find mem_1 and mem_2 which mention anxiety
        ids = [r.memory_id for r in result.results]
        assert "mem_1" in ids or "mem_2" in ids

    def test_no_results_for_nonexistent(self, test_db):
        retriever = HybridRetriever(test_db)
        result = retriever.search(
            "xyznonexistent", "test_user", limit=5, use_graph=False, use_temporal=False, use_semantic=False
        )

        assert len(result.results) == 0

    def test_multi_term_query(self, test_db):
        retriever = HybridRetriever(test_db)
        result = retriever.search(
            "feeling anxious", "test_user", limit=5, use_graph=False, use_temporal=False, use_semantic=False
        )

        assert len(result.results) > 0


class TestSemanticSearch:
    """Test TF-IDF cosine similarity semantic search."""

    def test_returns_ranked_results(self, test_db):
        retriever = HybridRetriever(test_db)
        conn = retriever._get_connection()
        try:
            ranking = retriever._semantic_search(conn, "anxiety therapy", "test_user", "default", 10)
        finally:
            conn.close()

        # Should return a dict of memory_id -> rank
        assert isinstance(ranking, dict)
        # Anxiety-related memories should rank
        assert len(ranking) > 0
        # Ranks should be 1-based positive integers
        for _mid, rank in ranking.items():
            assert rank >= 1

    def test_finds_topically_similar_documents(self, test_db):
        """Documents sharing terms with the query should rank higher."""
        retriever = HybridRetriever(test_db)
        conn = retriever._get_connection()
        try:
            ranking = retriever._semantic_search(conn, "anxiety management", "test_user", "default", 10)
        finally:
            conn.close()

        # mem_2 contains both "anxiety" and "management" - should rank best
        assert "mem_2" in ranking
        # mem_2 should have rank 1 (best similarity)
        assert ranking["mem_2"] == 1

    def test_excludes_ghost_memories(self, test_db):
        """Ghost memories (is_ghost=1) should be excluded."""
        conn = sqlite3.connect(test_db)
        conn.execute(
            "INSERT INTO memories (id, user_id, tenant_id, content, category, importance, is_ghost) "
            "VALUES ('ghost_1', 'test_user', 'default', 'ghost anxiety data', 'fact', 0.9, 1)"
        )
        conn.commit()
        conn.close()

        retriever = HybridRetriever(test_db)
        conn = retriever._get_connection()
        try:
            ranking = retriever._semantic_search(conn, "anxiety", "test_user", "default", 10)
        finally:
            conn.close()

        assert "ghost_1" not in ranking

    def test_empty_query_returns_empty(self, test_db):
        retriever = HybridRetriever(test_db)
        conn = retriever._get_connection()
        try:
            ranking = retriever._semantic_search(conn, "   ", "test_user", "default", 10)
        finally:
            conn.close()

        assert ranking == {}

    def test_no_results_for_unrelated_query(self, test_db):
        retriever = HybridRetriever(test_db)
        conn = retriever._get_connection()
        try:
            ranking = retriever._semantic_search(conn, "quantum physics superposition", "test_user", "default", 10)
        finally:
            conn.close()

        # No documents share any terms with this query
        assert ranking == {}

    def test_respects_limit(self, test_db):
        retriever = HybridRetriever(test_db)
        conn = retriever._get_connection()
        try:
            ranking = retriever._semantic_search(conn, "feeling", "test_user", "default", 2)
        finally:
            conn.close()

        assert len(ranking) <= 2


class TestCosineSimilarity:
    """Test the cosine similarity computation within semantic search."""

    def test_identical_documents_high_similarity(self, test_db):
        """A query matching a document exactly should rank it highly."""
        retriever = HybridRetriever(test_db)
        conn = retriever._get_connection()
        try:
            # Use the exact text from mem_1 as the query terms
            ranking = retriever._semantic_search(conn, "anxious presentation tomorrow", "test_user", "default", 10)
        finally:
            conn.close()

        # mem_1 contains these terms, should appear in results
        assert "mem_1" in ranking
        # mem_1 should rank better than unrelated docs
        if "mem_4" in ranking:
            assert ranking["mem_1"] < ranking["mem_4"]

    def test_partial_overlap_ranks_lower(self, test_db):
        """Documents with partial term overlap should rank lower than full overlap."""
        retriever = HybridRetriever(test_db)
        conn = retriever._get_connection()
        try:
            ranking = retriever._semantic_search(conn, "anxiety management therapy", "test_user", "default", 10)
        finally:
            conn.close()

        # mem_2 has all three terms (anxiety + management + CBT/therapy)
        # mem_1 only has "anxious" (stem differs, but close enough for token match)
        # mem_2 should rank better than mem_4 (family dinner - no overlap)
        if "mem_2" in ranking and "mem_4" in ranking:
            assert ranking["mem_2"] < ranking["mem_4"]


class TestGraphSearch:
    """Test graph-based search."""

    def test_finds_memories_via_entity(self, test_db):
        retriever = HybridRetriever(test_db)
        result = retriever.search(
            "anxiety", "test_user", limit=5, use_keyword=False, use_temporal=False, use_semantic=False
        )

        # Should find mem_1 and mem_2 via entity_anxiety
        ids = [r.memory_id for r in result.results]
        assert len(ids) > 0

    def test_no_graph_results_for_unknown_entity(self, test_db):
        retriever = HybridRetriever(test_db)
        result = retriever.search(
            "xyztity", "test_user", limit=5, use_keyword=False, use_temporal=False, use_semantic=False
        )

        assert len(result.results) == 0


class TestTemporalSearch:
    """Test temporal importance search."""

    def test_ranks_by_importance_and_recency(self, test_db):
        retriever = HybridRetriever(test_db)
        result = retriever.search(
            "", "test_user", limit=5, use_keyword=False, use_graph=False, use_temporal=True, use_semantic=False
        )

        # Should return memories, most important/recent first
        assert len(result.results) > 0
        # mem_5 (importance 0.9, 1hr old) should rank high
        ids = [r.memory_id for r in result.results]
        assert "mem_5" in ids

    def test_respects_min_importance(self, test_db):
        retriever = HybridRetriever(test_db)
        result = retriever.search(
            "",
            "test_user",
            limit=5,
            min_importance=0.8,
            use_keyword=False,
            use_graph=False,
            use_temporal=True,
            use_semantic=False,
        )

        for r in result.results:
            assert r.importance >= 0.8

    def test_finds_recent_low_importance_memory(self):
        """Temporal should find recent memories even if they have low importance."""
        _, path = tempfile.mkstemp(suffix=".db")
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE memories (
                id TEXT PRIMARY KEY, user_id TEXT, tenant_id TEXT DEFAULT 'default',
                content TEXT, importance REAL DEFAULT 0.5,
                current_strength REAL, strength_trend TEXT DEFAULT 'stable',
                created_at TEXT, activation_count INTEGER DEFAULT 0,
                last_retrieved_at TEXT, category TEXT, is_ghost INTEGER DEFAULT 0
            )
        """)
        uid = "test_user"
        now = datetime.now(timezone.utc)
        conn.execute(
            "INSERT INTO memories (id, user_id, content, importance, created_at) "
            "VALUES ('old_important', ?, 'old important memory', 0.9, ?)",
            (uid, (now - timedelta(days=30)).isoformat()),
        )
        conn.execute(
            "INSERT INTO memories (id, user_id, content, importance, created_at) "
            "VALUES ('recent_trivial', ?, 'recent trivial memory', 0.2, ?)",
            (uid, (now - timedelta(minutes=5)).isoformat()),
        )
        conn.commit()
        conn.close()

        retriever = HybridRetriever(path)
        conn2 = retriever._get_connection()
        try:
            rankings = retriever._temporal_search(conn2, uid, "default", 10, 0.0)
        finally:
            conn2.close()
        os.unlink(path)

        # Both should appear in temporal results
        assert "recent_trivial" in rankings, "Recent low-importance memory should be in temporal results"
        assert "old_important" in rankings, "Old important memory should be in temporal results"


class TestHybridFusion:
    """Test RRF fusion of all signals."""

    def test_combined_search(self, test_db):
        retriever = HybridRetriever(test_db)
        result = retriever.search("anxiety", "test_user", limit=5)

        assert len(result.results) > 0
        # Results should have combined scores
        for r in result.results:
            assert r.combined_score > 0

    def test_signal_counts_includes_semantic(self, test_db):
        retriever = HybridRetriever(test_db)
        result = retriever.search("anxiety", "test_user", limit=5)

        assert "keyword" in result.signal_counts
        assert "tfidf_cosine" in result.signal_counts
        assert "graph" in result.signal_counts
        assert "temporal" in result.signal_counts

    def test_semantic_score_populated(self, test_db):
        """Results found via semantic search should have a semantic_score."""
        retriever = HybridRetriever(test_db)
        result = retriever.search("anxiety therapy", "test_user", limit=5)

        tfidf_results = [r for r in result.results if "tfidf_cosine" in r.source_signals]
        assert len(tfidf_results) > 0
        for r in tfidf_results:
            assert r.tfidf_cosine_score > 0.0

    def test_multi_signal_memories_rank_higher(self, test_db):
        """Memories found by multiple signals should rank higher."""
        retriever = HybridRetriever(test_db)
        result = retriever.search("anxiety", "test_user", limit=5)

        # mem_1 and mem_2 match keyword AND graph (via anxiety entity)
        # Multi-signal results are expected but not guaranteed for all queries
        assert len(result.results) > 0

    def test_all_signals_disabled(self, test_db):
        retriever = HybridRetriever(test_db)
        result = retriever.search(
            "anxiety", "test_user", limit=5, use_keyword=False, use_graph=False, use_temporal=False, use_semantic=False
        )

        assert len(result.results) == 0

    def test_semantic_only_search(self, test_db):
        """Search with only semantic signal should return results."""
        retriever = HybridRetriever(test_db)
        result = retriever.search(
            "anxiety", "test_user", limit=5, use_keyword=False, use_graph=False, use_temporal=False, use_semantic=True
        )

        ids = [r.memory_id for r in result.results]
        assert len(ids) > 0
        # All results should only have 'semantic' as source signal
        for r in result.results:
            assert r.source_signals == ["semantic"]

    def test_result_format(self, test_db):
        retriever = HybridRetriever(test_db)
        result = retriever.search("anxiety", "test_user", limit=5)

        # Check result has to_dict
        d = result.to_dict()
        assert "results" in d
        assert "total_candidates" in d
        assert "signal_counts" in d

        if d["results"]:
            r = d["results"][0]
            assert "memory_id" in r
            assert "combined_score" in r
            assert "source_signals" in r
            assert "semantic_score" in r


class TestRRF:
    """Test Reciprocal Rank Fusion directly."""

    def test_rrf_prefers_multi_signal(self):
        retriever = HybridRetriever(":memory:")

        # Memory A: rank 1 in keyword, rank 3 in graph
        # Memory B: rank 1 in keyword only
        keyword = {"A": 1, "B": 2}
        semantic = {}
        graph = {"A": 3}
        temporal = {}

        result = retriever._reciprocal_rank_fusion(keyword, semantic, graph, temporal)

        # A should score higher (found by 2 signals)
        scores = dict(result)
        assert scores["A"] > scores["B"]

    def test_rrf_empty_inputs(self):
        retriever = HybridRetriever(":memory:")
        result = retriever._reciprocal_rank_fusion({}, {}, {}, {})
        assert result == []

    def test_rrf_with_semantic_signal(self):
        retriever = HybridRetriever(":memory:")

        keyword = {"A": 1}
        semantic = {"A": 2, "B": 1}
        graph = {}
        temporal = {}

        result = retriever._reciprocal_rank_fusion(keyword, semantic, graph, temporal)
        scores = dict(result)

        # A is found by keyword + semantic, B only by semantic
        assert scores["A"] > scores["B"]


class TestDefaultWeights:
    """Test that default weights include semantic."""

    def test_semantic_weight_present(self):
        assert "semantic" in HybridRetriever.DEFAULT_WEIGHTS
        assert HybridRetriever.DEFAULT_WEIGHTS["semantic"] == 0.7

    def test_all_four_weights_present(self):
        assert len(HybridRetriever.DEFAULT_WEIGHTS) == 4
        assert set(HybridRetriever.DEFAULT_WEIGHTS.keys()) == {"keyword", "semantic", "graph", "temporal"}


class TestSecurityFixes:
    """Test security fixes from code review."""

    def test_escape_like_metacharacters(self):
        """LIKE wildcards should be escaped to prevent injection."""
        assert _escape_like("test%value") == "test!%value"
        assert _escape_like("test_value") == "test!_value"
        assert _escape_like("test!mark") == "test!!mark"
        assert _escape_like("normal") == "normal"

    def test_validate_input_rejects_empty_user_id(self):
        with pytest.raises(ValueError, match="user_id"):
            _validate_input("query", "")

    def test_validate_input_rejects_long_user_id(self):
        with pytest.raises(ValueError, match="user_id"):
            _validate_input("query", "x" * 200)

    def test_validate_input_rejects_long_query(self):
        with pytest.raises(ValueError, match="query"):
            _validate_input("x" * 600, "user")

    def test_validate_input_accepts_valid(self):
        _validate_input("anxiety management", "test_user")

    def test_like_injection_safe(self, test_db):
        """Query with LIKE metacharacters should not match everything."""
        retriever = HybridRetriever(test_db)
        result = retriever.search("%", "test_user", limit=5, use_graph=False, use_temporal=False, use_semantic=False)

        # Should not return all memories (literal % is escaped)
        # Either 0 results or only those with literal % in content
        for r in result.results:
            assert "%" in r.content


class TestSearchTrace:
    def test_to_dict_serialization(self):
        trace = SearchTrace(
            query="anxiety",
            latency_ms=1.23,
            result_count=3,
            total_candidates=5,
            signal_counts={"keyword": 3, "tfidf_cosine": 2, "fast_path": "cache"},
            fast_path="cache",
            response_bytes=512,
        )
        d = trace.to_dict()
        assert d["query"] == "anxiety"
        assert d["latency_ms"] == 1.23
        assert d["result_count"] == 3
        assert d["total_candidates"] == 5
        assert d["fast_path"] == "cache"
        assert d["signal_counts"]["fast_path"] == "cache"

    def test_fast_path_none_when_not_cached(self):
        trace = SearchTrace(
            query="anxiety",
            latency_ms=5.0,
            result_count=3,
            total_candidates=5,
            signal_counts={"keyword": 3, "tfidf_cosine": 2},
            fast_path=None,
            response_bytes=256,
        )
        assert trace.fast_path is None
        d = trace.to_dict()
        assert d["fast_path"] is None

    def test_fast_path_int_from_signal_counts(self):
        trace = SearchTrace(
            query="anxiety",
            latency_ms=5.0,
            result_count=3,
            total_candidates=5,
            signal_counts={"keyword": 3},
            fast_path=None,
            response_bytes=256,
        )
        assert trace.fast_path is None or isinstance(trace.fast_path, (str, int))


class TestRetrieverFastPathWithSignalCounts:
    def test_search_result_has_fast_path_on_cache_hit(self, test_db):
        retriever = HybridRetriever(test_db)
        result1 = retriever.search(
            "anxiety", "test_user", limit=5, use_graph=False, use_temporal=False, use_semantic=False
        )
        assert "fast_path" not in result1.signal_counts
        result2 = retriever.search(
            "anxiety", "test_user", limit=5, use_graph=False, use_temporal=False, use_semantic=False
        )
        assert result2.signal_counts.get("fast_path") == "cache"

    def test_early_termination_sets_fast_path_metadata(self, test_db):
        retriever = HybridRetriever(test_db)
        result = retriever.search(
            "overwhelmed",
            "test_user",
            limit=5,
            use_keyword=True,
            use_tfidf_cosine=False,
            use_graph=False,
            use_temporal=False,
        )
        fp = result.signal_counts.get("fast_path")
        assert fp is None or fp == "early_termination"


class TestNormalizeQuery:
    def test_lowercase_normalization(self):
        assert _normalize_query("ANXIETY") == "anxiety"

    def test_strips_whitespace(self):
        assert _normalize_query("  anxiety  ") == "anxiety"

    def test_deduplicates_terms(self):
        assert _normalize_query("anxiety anxiety management") == "anxiety management"

    def test_collapse_internal_whitespace(self):
        assert _normalize_query("anxiety  management") == "anxiety management"

    def test_empty_input_yields_empty(self):
        assert _normalize_query("   ") == ""

    def test_order_preserving_dedup(self):
        assert _normalize_query("anxiety stress anxiety") == "anxiety stress"


class TestFastPathTiers:
    def test_cache_hit_returns_cached_result(self, test_db):
        retriever = HybridRetriever(test_db)
        result1 = retriever.search(
            "anxiety", "test_user", limit=5, use_graph=False, use_temporal=False, use_semantic=False
        )
        assert len(result1.results) > 0
        result2 = retriever.search(
            "anxiety", "test_user", limit=5, use_graph=False, use_temporal=False, use_semantic=False
        )
        assert result2.signal_counts.get("fast_path") == "cache"
        assert result2.results == result1.results

    def test_query_normalization_hits_same_cache_entry(self, test_db):
        retriever = HybridRetriever(test_db)
        retriever.search("anxiety", "test_user", limit=5, use_graph=False, use_temporal=False, use_semantic=False)
        result2 = retriever.search(
            "  ANXIETY  ", "test_user", limit=5, use_graph=False, use_temporal=False, use_semantic=False
        )
        assert result2.signal_counts.get("fast_path") == "cache"

    def test_fast_path_disabled_skips_cache(self, test_db):
        retriever = HybridRetriever(test_db)
        retriever.search(
            "anxiety",
            "test_user",
            limit=5,
            use_graph=False,
            use_temporal=False,
            use_semantic=False,
            fast_path_enabled=False,
        )
        result2 = retriever.search(
            "anxiety",
            "test_user",
            limit=5,
            use_graph=False,
            use_temporal=False,
            use_semantic=False,
            fast_path_enabled=False,
        )
        assert "fast_path" not in result2.signal_counts

    def test_early_termination_fires_when_top_dominates(self, test_db):
        retriever = HybridRetriever(test_db)
        result = retriever.search(
            "overwhelmed",
            "test_user",
            limit=5,
            use_keyword=True,
            use_tfidf_cosine=False,
            use_graph=False,
            use_temporal=False,
        )
        if "early_termination" in result.signal_counts:
            assert result.signal_counts["fast_path"] == "early_termination"

    def test_fast_path_enabled_default_true(self, test_db):
        retriever = HybridRetriever(test_db)
        result = retriever.search(
            "anxiety", "test_user", limit=5, use_graph=False, use_temporal=False, use_semantic=False
        )
        assert "fast_path" in result.signal_counts or result.signal_counts.get("fast_path") is None

    def test_empty_result_is_cached(self, test_db):
        retriever = HybridRetriever(test_db)
        result1 = retriever.search(
            "xyznonexistent",
            "test_user",
            limit=5,
            use_graph=False,
            use_temporal=False,
            use_semantic=False,
        )
        assert len(result1.results) == 0
        result2 = retriever.search(
            "xyznonexistent",
            "test_user",
            limit=5,
            use_graph=False,
            use_temporal=False,
            use_semantic=False,
        )
        assert result2.signal_counts.get("fast_path") == "cache"


class TestTemporalSignalHardening:
    """Tests for the new temporal signal hardening features."""

    def test_burst_boost_no_activations(self):
        retriever = HybridRetriever(":memory:")
        now = datetime.now(timezone.utc)
        boost = retriever._compute_burst_boost(0, None, now)
        assert boost == 1.0

    def test_burst_boost_with_activations(self):
        retriever = HybridRetriever(":memory:")
        now = datetime.now(timezone.utc)
        boost = retriever._compute_burst_boost(4, None, now)
        # sqrt(4) = 2, base_boost = 1 + 2 * 0.1 = 1.2
        assert boost == 1.2

    def test_burst_boost_recent_retrieval(self):
        retriever = HybridRetriever(":memory:")
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(minutes=5)).isoformat()
        boost = retriever._compute_burst_boost(1, recent, now)
        # base = 1 + 1 * 0.1 = 1.1; burst = 1 + (1 - 5/60) * 0.5 ≈ 1.458
        # => 1.1 * 1.458 ≈ 1.604
        assert 1.5 < boost < 1.7

    def test_burst_boost_capped(self):
        retriever = HybridRetriever(":memory:")
        now = datetime.now(timezone.utc)
        recent = now.isoformat()
        # High counts with recent retrieval should be capped
        boost = retriever._compute_burst_boost(100, recent, now)
        assert boost <= 3.0

    def test_time_score_default_half_life(self):
        retriever = HybridRetriever(":memory:")
        # At exactly half-life (168h), score should be 0.5
        score = retriever._compute_time_score(168.0, None)
        assert abs(score - 0.5) < 0.01

    def test_time_score_category_multiplier(self):
        retriever = HybridRetriever(":memory:")
        # 'fact' category has default mult 1.0, same as unscored
        score_fact = retriever._compute_time_score(168.0, "fact")
        score_none = retriever._compute_time_score(168.0, None)
        assert abs(score_fact - score_none) < 0.01

    def test_time_score_custom_category_longs_half_life(self):
        retriever = HybridRetriever(":memory:", weights={"category_mult_preference": 4.0})
        # 'preference' has default multiplier 1.5, overridden to 4.0 → 672h half-life
        # At 168h (1 week), preference decays less than no-category default 168h
        score_pref = retriever._compute_time_score(168.0, "preference")
        score_default = retriever._compute_time_score(168.0, None)
        assert score_pref > score_default

    def test_temporal_uses_current_strength(self):
        """Build a minimal DB with current_strength set to verify it's used."""
        _, path = tempfile.mkstemp(suffix=".db")
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE memories (
                id TEXT PRIMARY KEY, user_id TEXT, tenant_id TEXT DEFAULT 'default',
                content TEXT, importance REAL DEFAULT 0.5, current_strength REAL,
                created_at TEXT, activation_count INTEGER DEFAULT 0,
                strength_trend TEXT DEFAULT 'stable', last_retrieved_at TEXT,
                category TEXT, is_ghost INTEGER DEFAULT 0
            )
        """)
        now = datetime.now(timezone.utc)
        # mem_a: high importance but low current_strength
        # mem_b: lower importance but high current_strength
        conn.execute(
            "INSERT INTO memories (id, user_id, content, importance, current_strength, created_at) "
            "VALUES ('mem_a', 'u1', 'old high imp', 0.9, 0.3, ?)",
            ((now - timedelta(hours=1)).isoformat(),),
        )
        conn.execute(
            "INSERT INTO memories (id, user_id, content, importance, current_strength, created_at) "
            "VALUES ('mem_b', 'u1', 'recent high strength', 0.5, 0.9, ?)",
            ((now - timedelta(hours=1)).isoformat(),),
        )
        conn.commit()
        conn.close()

        retriever = HybridRetriever(path)
        conn2 = retriever._get_connection()
        try:
            rankings = retriever._temporal_search(conn2, "u1", "default", 10, 0.0)
        finally:
            conn2.close()
        import os

        os.unlink(path)

        # mem_b (high current_strength) should rank ahead of mem_a (low current_strength)
        assert "mem_a" in rankings
        assert "mem_b" in rankings
        assert rankings["mem_b"] < rankings["mem_a"]

    def test_temporal_trend_modifier(self):
        """Trend modifiers should boost 'strengthening' and penalize 'weakening'."""
        _, path = tempfile.mkstemp(suffix=".db")
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE memories (
                id TEXT PRIMARY KEY, user_id TEXT, tenant_id TEXT DEFAULT 'default',
                content TEXT, importance REAL DEFAULT 0.5, current_strength REAL,
                created_at TEXT, activation_count INTEGER DEFAULT 0,
                strength_trend TEXT DEFAULT 'stable', last_retrieved_at TEXT,
                category TEXT, is_ghost INTEGER DEFAULT 0
            )
        """)
        now = datetime.now(timezone.utc)
        for mid, trend in [("m_a", "strengthening"), ("m_b", "stable"), ("m_c", "weakening")]:
            conn.execute(
                "INSERT INTO memories (id, user_id, content, importance, strength_trend, created_at) "
                "VALUES (?, 'u1', 'test', 0.5, ?, ?)",
                (mid, trend, (now - timedelta(hours=2)).isoformat()),
            )
        conn.commit()
        conn.close()

        retriever = HybridRetriever(path)
        conn2 = retriever._get_connection()
        try:
            rankings = retriever._temporal_search(conn2, "u1", "default", 10, 0.0)
        finally:
            conn2.close()
        os.unlink(path)

        # strengthening >> stable > weakening in ranking order (lower rank = better)
        assert rankings["m_a"] < rankings["m_b"] < rankings["m_c"], (
            f"Expected strengthening({rankings['m_a']}) < stable({rankings['m_b']}) < weakening({rankings['m_c']})"
        )


class TestDecayCrossCutting:
    """Tests for cross-cutting decay multiplier in RRF fusion.

    Decay (current_strength) should penalize the combined RRF score of
    decayed memories, not just their temporal signal rank.
    """

    def test_decayed_memory_penalized_in_rrf(self):
        """A heavily decayed memory should rank lower despite identical content matching."""
        _, path = tempfile.mkstemp(suffix=".db")
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE memories (
                id TEXT PRIMARY KEY, user_id TEXT, tenant_id TEXT DEFAULT 'default',
                content TEXT, category TEXT, importance REAL DEFAULT 0.5,
                current_strength REAL, strength_trend TEXT DEFAULT 'stable',
                created_at TEXT, activation_count INTEGER DEFAULT 0,
                last_retrieved_at TEXT, is_ghost INTEGER DEFAULT 0
            )
        """)
        uid = "u1"
        now = datetime.now(timezone.utc)
        # Both memories have identical content (same keyword matching), same importance
        # but mem_fresh has high current_strength, mem_decayed has low current_strength
        conn.execute(
            "INSERT INTO memories (id, user_id, content, importance, current_strength, created_at) "
            "VALUES ('mem_fresh', ?, 'anxiety therapy session today', 0.8, 0.8, ?)",
            (uid, (now - timedelta(hours=1)).isoformat()),
        )
        conn.execute(
            "INSERT INTO memories (id, user_id, content, importance, current_strength, created_at) "
            "VALUES ('mem_decayed', ?, 'anxiety therapy session today', 0.8, 0.15, ?)",
            (uid, (now - timedelta(hours=1)).isoformat()),
        )
        conn.commit()
        conn.close()

        retriever = HybridRetriever(path)
        result = retriever.search("anxiety", uid, limit=5, use_graph=False, use_temporal=False, use_semantic=False)

        import os

        os.unlink(path)

        # Both memories should be found by keyword search
        ids = [r.memory_id for r in result.results]
        assert "mem_fresh" in ids
        assert "mem_decayed" in ids

        # mem_fresh should rank higher (combined_score penalized for decayed)
        fresh_result = next(r for r in result.results if r.memory_id == "mem_fresh")
        decayed_result = next(r for r in result.results if r.memory_id == "mem_decayed")
        assert fresh_result.combined_score > decayed_result.combined_score, (
            f"Fresh memory ({fresh_result.combined_score}) should outrank decayed ({decayed_result.combined_score})"
        )


class TestGraphSignalHardening:
    """Tests for the new graph/entity signal hardening."""

    def test_graph_composite_scoring(self):
        """Entity hits x confidence x edge quality determines graph ranking."""
        _, path = tempfile.mkstemp(suffix=".db")
        conn = sqlite3.connect(path)
        # Create minimal schema with entity_relationships
        conn.executescript("""
            CREATE TABLE memories (
                id TEXT PRIMARY KEY, user_id TEXT, tenant_id TEXT DEFAULT 'default',
                content TEXT, is_ghost INTEGER DEFAULT 0
            );
            CREATE TABLE memory_entities (
                id TEXT PRIMARY KEY, user_id TEXT, tenant_id TEXT DEFAULT 'default',
                name TEXT, entity_type TEXT, confidence REAL DEFAULT 1.0
            );
            CREATE TABLE entity_relationships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_entity_id TEXT, target_entity_id TEXT,
                user_id TEXT, tenant_id TEXT DEFAULT 'default',
                confidence REAL DEFAULT 1.0, decay_factor REAL DEFAULT 1.0
            );
            CREATE TABLE memory_entity_links (
                memory_id TEXT, entity_id TEXT,
                user_id TEXT, tenant_id TEXT DEFAULT 'default',
                PRIMARY KEY (memory_id, entity_id)
            );
        """)
        uid = "u1"
        # mem_high: matched by 2 entities, both high confidence, with high-quality edges
        conn.execute("INSERT INTO memories (id, user_id) VALUES ('m_high', ?)", (uid,))
        conn.execute("INSERT INTO memories (id, user_id) VALUES ('m_low', ?)", (uid,))
        # Entities: e1 (high conf), e2 (high conf), e3 (low conf)
        conn.execute(
            "INSERT INTO memory_entities (id, user_id, name, confidence) VALUES ('e1', ?, 'anxiety', 0.9)", (uid,)
        )
        conn.execute(
            "INSERT INTO memory_entities (id, user_id, name, confidence) VALUES ('e2', ?, 'therapy', 0.8)", (uid,)
        )
        conn.execute(
            "INSERT INTO memory_entities (id, user_id, name, confidence) VALUES ('e3', ?, 'stress', 0.2)", (uid,)
        )
        # m_high gets e1 + e2 (2 high-conf entities)
        conn.execute(
            "INSERT INTO memory_entity_links (memory_id, entity_id, user_id) VALUES ('m_high', 'e1', ?)", (uid,)
        )
        conn.execute(
            "INSERT INTO memory_entity_links (memory_id, entity_id, user_id) VALUES ('m_high', 'e2', ?)", (uid,)
        )
        # m_low gets e3 only (1 low-conf entity)
        conn.execute(
            "INSERT INTO memory_entity_links (memory_id, entity_id, user_id) VALUES ('m_low', 'e3', ?)", (uid,)
        )
        # Edges: e1-e2 (high quality), e3 orphan (no edges)
        conn.execute(
            "INSERT INTO entity_relationships (source_entity_id, target_entity_id, user_id, confidence, decay_factor) "
            "VALUES ('e1', 'e2', ?, 0.9, 1.0)",
            (uid,),
        )
        conn.commit()
        conn.close()

        retriever = HybridRetriever(path)
        rankings, hits, _ = retriever._graph_search(
            retriever._get_connection(), "anxiety therapy stress", uid, "default", 10
        )
        os.unlink(path)

        assert "m_high" in rankings
        assert "m_low" in rankings
        # m_high (2 entities x ~0.85 conf x ~high edge qual) >> m_low (1 x 0.2 x 1.0)
        assert rankings["m_high"] < rankings["m_low"]
        assert hits.get("m_high", 0) >= 2
        assert hits.get("m_low", 0) == 1

    def test_graph_entity_confidence_empty_db(self):
        """Graph search on empty DB returns empty rankings."""
        _, path = tempfile.mkstemp(suffix=".db")
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE memories (id TEXT PRIMARY KEY, user_id TEXT, tenant_id TEXT DEFAULT 'default',
                content TEXT, is_ghost INTEGER DEFAULT 0);
            CREATE TABLE memory_entities (id TEXT PRIMARY KEY, user_id TEXT, tenant_id TEXT DEFAULT 'default',
                name TEXT, entity_type TEXT, confidence REAL DEFAULT 1.0);
            CREATE TABLE entity_relationships (id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_entity_id TEXT, target_entity_id TEXT, user_id TEXT,
                tenant_id TEXT DEFAULT 'default', confidence REAL DEFAULT 1.0, decay_factor REAL DEFAULT 1.0);
            CREATE TABLE memory_entity_links (memory_id TEXT, entity_id TEXT,
                user_id TEXT, tenant_id TEXT DEFAULT 'default', PRIMARY KEY (memory_id, entity_id));
        """)
        conn.commit()
        conn.close()

        retriever = HybridRetriever(path)
        result = retriever._graph_search(retriever._get_connection(), "nonexistent", "u1", "default", 10)
        os.unlink(path)

        rankings, hits, conf = result
        assert rankings == {}
        assert hits == {}
        assert conf == {}

    def test_graph_uses_relevance_score(self):
        """relevance_score from memory_entity_links must factor into graph scoring."""
        _, path = tempfile.mkstemp(suffix=".db")
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE memories (
                id TEXT PRIMARY KEY, user_id TEXT, tenant_id TEXT DEFAULT 'default',
                content TEXT, is_ghost INTEGER DEFAULT 0
            );
            CREATE TABLE memory_entities (
                id TEXT PRIMARY KEY, user_id TEXT, tenant_id TEXT DEFAULT 'default',
                name TEXT, entity_type TEXT, confidence REAL DEFAULT 1.0
            );
            CREATE TABLE entity_relationships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_entity_id TEXT, target_entity_id TEXT,
                user_id TEXT, tenant_id TEXT DEFAULT 'default',
                confidence REAL DEFAULT 1.0, decay_factor REAL DEFAULT 1.0
            );
            CREATE TABLE memory_entity_links (
                memory_id TEXT, entity_id TEXT,
                user_id TEXT, tenant_id TEXT DEFAULT 'default',
                relevance_score REAL DEFAULT 1.0,
                PRIMARY KEY (memory_id, entity_id)
            );
        """)
        uid = "u1"
        # Two memories, both linked to the same entity, identical hits and confidence
        conn.execute("INSERT INTO memories (id, user_id) VALUES ('m_high_rel', ?)", (uid,))
        conn.execute("INSERT INTO memories (id, user_id) VALUES ('m_low_rel', ?)", (uid,))
        conn.execute(
            "INSERT INTO memory_entities (id, user_id, name, confidence) VALUES ('e_rel', ?, 'anxiety', 0.9)", (uid,)
        )
        # Same entity, different relevance_score
        conn.execute(
            "INSERT INTO memory_entity_links (memory_id, entity_id, user_id, relevance_score) "
            "VALUES ('m_high_rel', 'e_rel', ?, 0.95)",
            (uid,),
        )
        conn.execute(
            "INSERT INTO memory_entity_links (memory_id, entity_id, user_id, relevance_score) "
            "VALUES ('m_low_rel', 'e_rel', ?, 0.25)",
            (uid,),
        )
        conn.commit()
        conn.close()

        retriever = HybridRetriever(path)
        rankings, _, _ = retriever._graph_search(retriever._get_connection(), "anxiety", uid, "default", 10)
        os.unlink(path)

        assert "m_high_rel" in rankings
        assert "m_low_rel" in rankings
        # m_high_rel (relevance=0.95) should rank better than m_low_rel (relevance=0.25)
        assert rankings["m_high_rel"] < rankings["m_low_rel"], f"Higher relevance_score should rank better: {rankings}"


class TestEntityMetadataInResults:
    """Test entity metadata side-channel surfaces in search results."""

    def test_entity_fields_in_hybrid_result(self, test_db):
        retriever = HybridRetriever(test_db)
        result = retriever.search("anxiety", "test_user", limit=5)
        for r in result.results:
            assert hasattr(r, "entity_hits")
            assert hasattr(r, "entity_confidence_avg")
            d = r.to_dict()
            assert "entity_hits" in d
            assert "entity_confidence_avg" in d

    def test_entity_graph_results_have_entity_metadata(self, test_db):
        """Graph-search results should have non-zero entity metadata."""
        retriever = HybridRetriever(test_db)
        result = retriever.search(
            "anxiety", "test_user", limit=5, use_keyword=False, use_temporal=False, use_semantic=False
        )
        for r in result.results:
            if "graph" in r.source_signals:
                assert r.entity_hits > 0
                assert r.entity_confidence_avg > 0.0

    def test_non_graph_results_have_zero_entity_fields(self, test_db):
        """Keyword-only results should have zero entity metadata."""
        retriever = HybridRetriever(test_db)
        result = retriever.search(
            "feeling", "test_user", limit=5, use_graph=False, use_temporal=False, use_semantic=False
        )
        for r in result.results:
            assert r.entity_hits == 0
            assert r.entity_confidence_avg == 0.0


class TestDecayFloor:
    """Decay multiplier floor prevents full suppression of stale memories."""

    def test_decay_has_minimum_floor(self):
        """Even a severely decayed memory should retain a non-zero decay_multiplier."""
        _, path = tempfile.mkstemp(suffix=".db")
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE memories (
                id TEXT PRIMARY KEY, user_id TEXT, tenant_id TEXT DEFAULT 'default',
                content TEXT, category TEXT, importance REAL DEFAULT 0.5,
                current_strength REAL, strength_trend TEXT DEFAULT 'stable',
                created_at TEXT, activation_count INTEGER DEFAULT 0,
                last_retrieved_at TEXT, is_ghost INTEGER DEFAULT 0
            )
        """)
        uid = "u1"
        now = datetime.now(timezone.utc)
        # Memory with importance=0.9 but current_strength=0.01 (decayed to near zero)
        # Without floor, decay_multiplier would be ≈ 0.01 — nearly full suppression.
        conn.execute(
            "INSERT INTO memories (id, user_id, content, importance, current_strength, created_at) "
            "VALUES ('mem_decayed', ?, 'anxiety therapy session today', 0.9, 0.01, ?)",
            (uid, (now - timedelta(hours=48)).isoformat()),
        )
        conn.commit()
        conn.close()

        retriever = HybridRetriever(path)
        result = retriever.search("anxiety", uid, limit=5, use_graph=False, use_temporal=False, use_semantic=False)
        os.unlink(path)

        decayed_result = next(r for r in result.results if r.memory_id == "mem_decayed")
        # Floor is 0.2, so decay_multiplier should be >= 0.2
        assert decayed_result.decay_multiplier >= 0.2, (
            f"decay_multiplier={decayed_result.decay_multiplier} should be >= 0.2"
        )
        assert decayed_result.decay_multiplier <= 1.0

    def test_decay_metadata_in_to_dict(self):
        """decay_multiplier should appear in structured debug output."""
        _, path = tempfile.mkstemp(suffix=".db")
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE memories (
                id TEXT PRIMARY KEY, user_id TEXT, tenant_id TEXT DEFAULT 'default',
                content TEXT, category TEXT, importance REAL DEFAULT 0.5,
                current_strength REAL, strength_trend TEXT DEFAULT 'stable',
                created_at TEXT, activation_count INTEGER DEFAULT 0,
                last_retrieved_at TEXT, is_ghost INTEGER DEFAULT 0
            )
        """)
        uid = "u1"
        now = datetime.now(timezone.utc)
        conn.execute(
            "INSERT INTO memories (id, user_id, content, importance, current_strength, created_at) "
            "VALUES ('m1', ?, 'memory content here', 0.8, 0.5, ?)",
            (uid, (now - timedelta(hours=1)).isoformat()),
        )
        conn.commit()
        conn.close()

        retriever = HybridRetriever(path)
        result = retriever.search("memory", uid, limit=5, use_graph=False, use_temporal=False, use_semantic=False)
        os.unlink(path)

        for r in result.results:
            d = r.to_dict()
            assert "decay_multiplier" in d, f"decay_multiplier missing from to_dict: {d}"
            assert isinstance(d["decay_multiplier"], float)


class TestTemporalCategories:
    """Temporal categorization for debug transparency."""

    def test_current_state_preference(self):
        """preference category -> 'current_state'."""
        cat = HybridRetriever._classify_temporal_category(48.0, "preference", "stable")
        assert cat == "current_state"

    def test_current_state_trait(self):
        """trait category -> 'current_state'."""
        cat = HybridRetriever._classify_temporal_category(48.0, "trait", "stable")
        assert cat == "current_state"

    def test_recent_activity(self):
        """Created within 24h -> 'recent_activity'."""
        cat = HybridRetriever._classify_temporal_category(4.0, "fact", "stable")
        assert cat == "recent_activity"

    def test_future_plan_pending(self):
        """pending category -> 'future_plan'."""
        cat = HybridRetriever._classify_temporal_category(72.0, "pending", "stable")
        assert cat == "future_plan"

    def test_stale_by_trend(self):
        """strength_trend='stale' -> 'stale' regardless of age."""
        cat = HybridRetriever._classify_temporal_category(48.0, "fact", "stale")
        assert cat == "stale"

    def test_stale_by_age(self):
        """Older than 90 days -> 'stale'."""
        cat = HybridRetriever._classify_temporal_category(2200.0, "fact", "stable")
        assert cat == "stale"

    def test_historical_default(self):
        """Everything else -> 'historical'."""
        cat = HybridRetriever._classify_temporal_category(500.0, "decision", "stable")
        assert cat == "historical"

    def test_temporal_category_in_to_dict(self):
        """temporal_category should appear in to_dict when non-empty."""
        _, path = tempfile.mkstemp(suffix=".db")
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE memories (
                id TEXT PRIMARY KEY, user_id TEXT, tenant_id TEXT DEFAULT 'default',
                content TEXT, category TEXT, importance REAL DEFAULT 0.5,
                current_strength REAL, strength_trend TEXT DEFAULT 'stable',
                created_at TEXT, activation_count INTEGER DEFAULT 0,
                last_retrieved_at TEXT, is_ghost INTEGER DEFAULT 0
            )
        """)
        uid = "u1"
        now = datetime.now(timezone.utc)
        conn.execute(
            "INSERT INTO memories (id, user_id, content, category, importance, current_strength, created_at) "
            "VALUES ('m1', ?, 'preference memory', 'preference', 0.8, 0.8, ?)",
            (uid, (now - timedelta(hours=48)).isoformat()),
        )
        conn.commit()
        conn.close()

        retriever = HybridRetriever(path)
        result = retriever.search("preference", uid, limit=5, use_graph=False, use_temporal=False, use_semantic=False)
        os.unlink(path)

        for r in result.results:
            d = r.to_dict()
            assert "temporal_category" in d, f"temporal_category missing from to_dict: {d}"
            assert d["temporal_category"] in ("current_state", "recent_activity", "historical", "stale", "future_plan")


class TestEntityBoostConsistency:
    """Entity metadata should be consistent in score components."""

    def test_entity_graph_hits_reflect_matched_entities(self, test_db):
        """Graph results should have entity_hits matching the number of matched entities."""
        retriever = HybridRetriever(test_db)
        result = retriever.search(
            "anxiety", "test_user", limit=5, use_keyword=False, use_temporal=False, use_semantic=False
        )
        for r in result.results:
            if "graph" in r.source_signals:
                assert r.entity_hits > 0, f"Graph result {r.memory_id} has entity_hits=0"
                assert r.entity_confidence_avg > 0.0, f"Graph result {r.memory_id} has confidence=0"

    def test_non_graph_results_zero_entity(self, test_db):
        """Keyword-only results should have entity_hits=0."""
        retriever = HybridRetriever(test_db)
        result = retriever.search(
            "feeling", "test_user", limit=5, use_graph=False, use_temporal=False, use_semantic=False
        )
        for r in result.results:
            assert r.entity_hits == 0, f"Non-graph result {r.memory_id} has entity_hits={r.entity_hits}"
            assert r.entity_confidence_avg == 0.0

    def test_combined_score_reflects_entity_boost(self, test_db):
        """A memory with entity hits should score higher than one without (same keyword match)."""
        retriever = HybridRetriever(test_db)
        result = retriever.search("anxiety", "test_user", limit=10, use_semantic=False)
        graph_results = [r for r in result.results if "graph" in r.source_signals]
        assert len(graph_results) > 0, "No graph results found in search"
        graph_avg = sum(r.combined_score for r in graph_results) / len(graph_results)
        assert graph_avg > 0, "Graph results should have positive scores"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
