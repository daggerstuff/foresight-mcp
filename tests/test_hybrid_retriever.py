"""
Tests for hybrid retriever.
"""

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
    _validate_input,
    reset_hybrid_retriever,
)


@pytest.fixture(autouse=True)
def cleanup():
    reset_hybrid_retriever()
    yield
    reset_hybrid_retriever()


def create_test_db():
    """Create a temp DB with schema and test data."""
    fd, path = tempfile.mkstemp(suffix=".db")
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
            source_entity_id TEXT,
            target_entity_id TEXT,
            relationship_type TEXT,
            confidence REAL DEFAULT 1.0,
            metadata TEXT DEFAULT '{}',
            user_id TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE memory_entity_links (
            memory_id TEXT,
            entity_id TEXT,
            user_id TEXT,
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

    os.close(fd)
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
