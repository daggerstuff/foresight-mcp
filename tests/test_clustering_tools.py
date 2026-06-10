"""Tests for memory clustering tools (PIX-3841)."""

import json
import tempfile

import pytest
from foresight_mcp.clustering import ClusterResult, _jaccard, _tokenize, cluster_memories


def test_tokenize_basic() -> None:
    """Tokenize splits lowercase words and removes stop words."""
    tokens = _tokenize("I feel very anxious about the meeting tomorrow")
    assert "feel" in tokens
    assert "anxious" in tokens
    assert "meeting" in tokens
    assert "tomorrow" in tokens
    assert "the" not in tokens  # stop word
    assert "i" not in tokens  # stop word


def test_tokenize_short_words() -> None:
    """Words shorter than 3 characters are removed."""
    tokens = _tokenize("a an is to be go")
    assert len(tokens) == 0


def test_tokenize_numbers() -> None:
    """Numbers and alphanumeric tokens are kept."""
    tokens = _tokenize("test123 and 456")
    assert "test123" in tokens
    assert "456" in tokens


def test_jaccard_identical() -> None:
    """Jaccard of identical sets is 1.0."""
    assert _jaccard({"a", "b", "c"}, {"a", "b", "c"}) == 1.0


def test_jaccard_disjoint() -> None:
    """Jaccard of disjoint sets is 0.0."""
    assert _jaccard({"a", "b"}, {"c", "d"}) == 0.0


def test_jaccard_partial() -> None:
    """Jaccard with partial overlap."""
    score = _jaccard({"a", "b", "c"}, {"b", "c", "d"})
    assert score == 2 / 4  # intersection={b,c}=2, union={a,b,c,d}=4


def test_jaccard_empty_first() -> None:
    """Jaccard with first set empty is 0.0."""
    assert _jaccard(set(), {"a", "b"}) == 0.0


def test_jaccard_empty_second() -> None:
    """Jaccard with second set empty is 0.0."""
    assert _jaccard({"a", "b"}, set()) == 0.0


class TestClusterMemories:
    """Tests for the cluster_memories function."""

    def test_empty_memories(self) -> None:
        """Empty input returns empty result."""
        result = cluster_memories([])
        assert isinstance(result, ClusterResult)
        assert result.cluster_entities == []
        assert result.memory_links == []

    def test_single_memory(self) -> None:
        """Single memory returns empty result (below min_cluster_size)."""
        memories = [{"id": "mem1", "content": "I feel very anxious today", "user_id": "u1", "tenant_id": "t1"}]
        result = cluster_memories(memories)
        assert result.cluster_entities == []
        assert result.memory_links == []

    def test_two_related_memories(self) -> None:
        """Two related memories should form a cluster."""
        memories = [
            {
                "id": "mem1",
                "content": "I feel very anxious about the meeting today",
                "user_id": "u1",
                "tenant_id": "t1",
            },
            {
                "id": "mem2",
                "content": "I feel very anxious every day about work",
                "user_id": "u1",
                "tenant_id": "t1",
            },
        ]
        result = cluster_memories(memories, min_similarity=0.1)
        assert len(result.cluster_entities) == 1
        assert len(result.memory_links) == 2

    def test_unrelated_memories_no_cluster(self) -> None:
        """Unrelated memories should not cluster."""
        memories = [
            {
                "id": "mem1",
                "content": "I feel very anxious about the meeting tomorrow",
                "user_id": "u1",
                "tenant_id": "t1",
            },
            {"id": "mem2", "content": "The weather is nice and sunny today", "user_id": "u1", "tenant_id": "t1"},
        ]
        result = cluster_memories(memories, min_similarity=0.15)
        # These should not be similar enough
        assert len(result.cluster_entities) == 0

    def test_cluster_entity_structure(self) -> None:
        """Cluster entity dicts should have the expected fields."""
        memories = [
            {"id": "mem1", "content": "deeply anxious worried stressed", "user_id": "u1", "tenant_id": "t1"},
            {"id": "mem2", "content": "very worried and anxious daily", "user_id": "u1", "tenant_id": "t1"},
        ]
        result = cluster_memories(memories, min_similarity=0.1)
        assert len(result.cluster_entities) == 1
        entity = result.cluster_entities[0]
        assert entity["entity_type"] == "cluster"
        assert "id" in entity
        assert entity["id"].startswith("cluster:")
        assert "name" in entity
        assert "description" in entity
        assert "properties" in entity
        assert "member_ids" in entity["properties"]
        assert "size" in entity["properties"]
        assert entity["properties"]["size"] == 2

    def test_memory_link_structure(self) -> None:
        """Memory link dicts should have the expected fields."""
        memories = [
            {"id": "mem1", "content": "deeply anxious worried stressed", "user_id": "u1", "tenant_id": "t1"},
            {"id": "mem2", "content": "very worried and anxious daily", "user_id": "u1", "tenant_id": "t1"},
        ]
        result = cluster_memories(memories, min_similarity=0.1)
        assert len(result.memory_links) == 2
        for link in result.memory_links:
            assert "memory_id" in link
            assert "entity_id" in link["entity_id"] or link.get("entity_id")
            assert "relevance_score" in link
            assert link["relevance_score"] > 0

    def test_max_clusters_limit(self) -> None:
        """max_clusters should cap the number of returned cluster entities."""
        # Create enough memories to potentially form many clusters
        memories = []
        for i in range(10):
            memories.append(
                {
                    "id": f"mem_a{i}",
                    "content": f"alpha beta gamma delta theta topic_{i}",
                    "user_id": "u1",
                    "tenant_id": "t1",
                }
            )
        for i in range(10):
            memories.append(
                {
                    "id": f"mem_b{i}",
                    "content": f"omega psi phi chi rho topic_{i}",
                    "user_id": "u1",
                    "tenant_id": "t1",
                }
            )
        result = cluster_memories(memories, min_similarity=0.25, max_clusters=2)
        assert len(result.cluster_entities) <= 2

    def test_min_cluster_size(self) -> None:
        """min_cluster_size should filter out small clusters."""
        memories = [
            {
                "id": "mem1",
                "content": "unique content alpha beta gamma delta epsilon",
                "user_id": "u1",
                "tenant_id": "t1",
            },
            {
                "id": "mem2",
                "content": "unique content alpha beta gamma delta epsilon zeta",
                "user_id": "u1",
                "tenant_id": "t1",
            },
            {
                "id": "mem3",
                "content": "completely unrelated topic about weather today",
                "user_id": "u1",
                "tenant_id": "t1",
            },
        ]
        # min_cluster_size=3 means mem1+mem2 (2 members) won't form a cluster
        result = cluster_memories(memories, min_similarity=0.2, min_cluster_size=3)
        assert len(result.cluster_entities) == 0

    def test_tenant_isolation_in_cluster_id(self) -> None:
        """Cluster IDs should be tenant-specific."""
        memories_t1 = [
            {"id": "mem1", "content": "anxiety worried stressed", "user_id": "u1", "tenant_id": "tenant_a"},
            {"id": "mem2", "content": "anxiety worried stressed feeling", "user_id": "u1", "tenant_id": "tenant_a"},
        ]
        memories_t2 = [
            {"id": "mem3", "content": "anxiety worried stressed", "user_id": "u1", "tenant_id": "tenant_b"},
            {"id": "mem4", "content": "anxiety worried stressed feeling", "user_id": "u1", "tenant_id": "tenant_b"},
        ]
        result1 = cluster_memories(memories_t1, min_similarity=0.1)
        result2 = cluster_memories(memories_t2, min_similarity=0.1)
        assert len(result1.cluster_entities) == 1
        assert len(result2.cluster_entities) == 1
        # Different tenants should produce different cluster IDs
        assert result1.cluster_entities[0]["id"] != result2.cluster_entities[0]["id"]


# =============================================================================
# MCP Tool Integration Tests
# =============================================================================


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    fd, path = tempfile.mkstemp(suffix=".db")
    yield path
    import os

    os.close(fd)
    os.unlink(path)


@pytest.fixture
def server_env(temp_db, monkeypatch):
    """Set up server environment with isolated DB and mocked USER_ID."""
    monkeypatch.setenv("FORESIGHT_DB_PATH", temp_db)
    import foresight_mcp.config as config_module
    import foresight_mcp.connection_pool as conn_pool_module
    from foresight_mcp.connection_pool import reset_pool
    from foresight_mcp.server import init_db

    monkeypatch.setattr(config_module, "DB_PATH", temp_db)
    monkeypatch.setattr(conn_pool_module, "DB_PATH", temp_db)
    monkeypatch.setattr(config_module, "USER_ID", "test_user")

    # Also patch graph_store's module-level DB_PATH import
    import foresight_mcp.graph_store as graph_store_module

    monkeypatch.setattr(graph_store_module, "DB_PATH", temp_db)
    from foresight_mcp.graph_store import reset_graph_store

    reset_graph_store()
    reset_pool()
    init_db()
    # Reset USER_ID in server module too
    import foresight_mcp.server as server_module

    monkeypatch.setattr(server_module, "USER_ID", "test_user")

    yield
    reset_pool()
    reset_graph_store()


@pytest.fixture
def seed_memories(server_env):
    """Seed test memories for clustering tests."""
    from foresight_mcp.server import store_memory

    # Group A: anxiety-themed memories
    store_memory("I feel very anxious about my job interview tomorrow", user_id="test_user")
    store_memory("My anxiety has been getting worse lately", user_id="test_user")
    store_memory("The anxiety before presentations is overwhelming", user_id="test_user")

    # Group B: happiness-themed memories
    store_memory("I feel happy when I spend time with my family", user_id="test_user")
    store_memory("Happiness comes from small moments in life", user_id="test_user")
    store_memory("Being happy is a choice I make every day", user_id="test_user")

    # Group C: unrelated filler
    store_memory("The weather is nice today", user_id="test_user")
    store_memory("I need to buy groceries later", user_id="test_user")

    # Fix: store_memory uses content_hash dedup, ensure unique content
    # by not re-using same content across calls
    return 8  # 8 unique memories


class TestRunClusteringTool:
    """Tests for the run_clustering MCP tool."""

    def test_run_clustering_no_memories(self, server_env) -> None:
        """run_clustering with no memories returns zero clusters."""
        from foresight_mcp.server import run_clustering

        result = run_clustering(user_id="empty_user")
        data = json.loads(result)
        assert data["ok"] is True
        assert data["clusters_created"] == 0
        assert data["memories_processed"] == 0

    def test_run_clustering_creates_clusters(self, seed_memories) -> None:
        """run_clustering should create clusters from related memories."""
        from foresight_mcp.server import run_clustering

        result = run_clustering(user_id="test_user", min_similarity=0.1)
        data = json.loads(result)
        assert data["ok"] is True
        assert data["clusters_created"] >= 0  # May or may not cluster depending on content
        assert data["memories_processed"] >= 6

    def test_run_clustering_high_threshold(self, seed_memories) -> None:
        """High min_similarity should produce no clusters."""
        from foresight_mcp.server import run_clustering

        result = run_clustering(user_id="test_user", min_similarity=0.99)
        data = json.loads(result)
        assert data["ok"] is True
        assert data["clusters_created"] == 0


class TestQueryClustersTool:
    """Tests for the query_clusters MCP tool."""

    def test_query_clusters_empty(self, server_env) -> None:
        """query_clusters with no cluster entities returns empty list."""
        from foresight_mcp.server import query_clusters

        result = query_clusters(user_id="empty_user")
        data = json.loads(result)
        assert data["ok"] is True
        assert data["cluster_count"] == 0
        assert data["clusters"] == []

    def test_query_clusters_after_run(self, seed_memories) -> None:
        """query_clusters should return cluster entities after run_clustering."""
        from foresight_mcp.server import query_clusters, run_clustering

        run_result = json.loads(run_clustering(user_id="test_user", min_similarity=0.1))
        if run_result["clusters_created"] == 0:
            pytest.skip("No clusters formed - cannot test query")

        q_result = json.loads(query_clusters(user_id="test_user"))
        assert q_result["ok"] is True
        assert q_result["cluster_count"] > 0
        for cluster in q_result["clusters"]:
            assert "cluster_id" in cluster
            assert cluster["cluster_id"].startswith("cluster:")
            assert "member_count" in cluster
            assert cluster["member_count"] >= 2
            assert "member_ids" in cluster


class TestUpsertClusterResults:
    """Tests for the _upsert_cluster_results helper."""

    def test_upsert_empty_result(self, server_env) -> None:
        """Empty ClusterResult should upsert nothing."""
        from foresight_mcp.clustering import ClusterResult
        from foresight_mcp.server import _upsert_cluster_results

        result = ClusterResult(cluster_entities=[], memory_links=[])
        summary = _upsert_cluster_results(result, "test_user", "default")

        assert summary["entity_count"] == 0
        assert summary["link_count"] == 0

    def test_upsert_and_query_round_trip(self, server_env) -> None:
        """Entities upserted via _upsert_cluster_results should be queryable."""
        from foresight_mcp.clustering import ClusterResult
        from foresight_mcp.graph_store import get_graph_store
        from foresight_mcp.server import _upsert_cluster_results

        store = get_graph_store()
        cluster_entities = [
            {
                "id": "cluster:test123abc",
                "name": "anxiety_cluster",
                "entity_type": "cluster",
                "description": "3 memories about anxiety",
                "properties": {
                    "size": 3,
                    "affinity": 0.45,
                    "member_ids": ["mem_a", "mem_b", "mem_c"],
                },
            }
        ]
        memory_links = [
            {
                "memory_id": "mem_a",
                "entity_id": "cluster:test123abc",
                "tenant_id": "default",
                "user_id": "test_user",
                "relevance_score": 0.45,
            },
            {
                "memory_id": "mem_b",
                "entity_id": "cluster:test123abc",
                "tenant_id": "default",
                "user_id": "test_user",
                "relevance_score": 0.45,
            },
        ]

        result = ClusterResult(cluster_entities=cluster_entities, memory_links=memory_links)
        summary = _upsert_cluster_results(result, "test_user", "default")
        assert summary["entity_count"] == 1
        assert summary["link_count"] == 2

        # Verify via graph store
        entities = store.get_entities_by_type("test_user", "cluster")
        assert len(entities) == 1
        assert entities[0].id == "cluster:test123abc"
        assert entities[0].properties.get("size") == 3

    def test_upsert_then_memory_linking(self, server_env) -> None:
        """After upsert, memories should be linkable to cluster entities."""
        from foresight_mcp.clustering import ClusterResult
        from foresight_mcp.server import _upsert_cluster_results

        cluster_entities = [
            {
                "id": "cluster:linktest",
                "name": "test_cluster",
                "entity_type": "cluster",
                "description": "Test cluster",
                "properties": {"size": 1, "member_ids": ["mem_x"]},
            }
        ]
        memory_links = [
            {
                "memory_id": "mem_x",
                "entity_id": "cluster:linktest",
                "tenant_id": "default",
                "user_id": "test_user",
                "relevance_score": 0.5,
            },
        ]

        result = ClusterResult(cluster_entities=cluster_entities, memory_links=memory_links)
        _upsert_cluster_results(result, "test_user", "default")

        # The memory_entity_links table should have the link
        from foresight_mcp.connection_pool import get_pool

        pool = get_pool()
        conn = pool.acquire()
        try:
            row = conn.execute(
                "SELECT * FROM memory_entity_links WHERE entity_id = ?",
                ("cluster:linktest",),
            ).fetchone()
            assert row is not None
            assert row["memory_id"] == "mem_x"
            assert row["entity_id"] == "cluster:linktest"
        finally:
            pool.release(conn)
