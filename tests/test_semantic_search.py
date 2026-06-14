"""Tests for MEM-5: Semantic Vector Search."""

from __future__ import annotations

import math
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest
from foresight_mcp import semantic_search as sem_mod
from foresight_mcp.semantic_search import (
    DEFAULT_PROVIDER,
    LOCAL_HASH_DIM,
    VALID_PROVIDERS,
    LocalHashEmbedder,
    SemanticMatch,
    SemanticSearch,
    SemanticSearchError,
    SemanticSearchResult,
    cosine_similarity,
    deserialize_vector,
    get_embedder,
    get_semantic_search,
    reset_semantic_search,
    serialize_vector,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_db() -> str:
    """Create a temporary DB with memory_embeddings table."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS memory_embeddings (
            memory_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            dimension INTEGER NOT NULL,
            vector BLOB NOT NULL,
            model_version TEXT DEFAULT '1',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id, user_id, memory_id, provider)
        )"""
    )
    conn.commit()
    conn.close()
    return db_path


@contextmanager
def _patched_db(db_path: str) -> Iterator[SemanticSearch]:
    """Patch DB_PATH for the store and reset the singleton."""
    reset_semantic_search()
    with (
        patch.object(sem_mod, "DB_PATH", db_path),
        patch("foresight_mcp.config.DB_PATH", db_path),
        patch("foresight_mcp.semantic_search.DB_PATH", db_path),
    ):
        store = SemanticSearch(db_path)
        try:
            yield store
        finally:
            reset_semantic_search()


# ---------------------------------------------------------------------------
# Embedder contract
# ---------------------------------------------------------------------------


def test_default_provider_is_local_hash():
    assert DEFAULT_PROVIDER == "local-hash"
    assert DEFAULT_PROVIDER in VALID_PROVIDERS


def test_get_embedder_returns_local_hash():
    embedder = get_embedder()
    assert isinstance(embedder, LocalHashEmbedder)
    assert embedder.dimension == LOCAL_HASH_DIM
    assert embedder.provider_name == DEFAULT_PROVIDER


def test_get_embedder_rejects_unknown_provider():
    with pytest.raises(SemanticSearchError, match="unknown embedder provider"):
        get_embedder("openai-mega")


def test_local_hash_embedder_is_deterministic():
    embedder = LocalHashEmbedder()
    v1 = embedder.embed("the quick brown fox")
    v2 = embedder.embed("the quick brown fox")
    assert v1 == v2


def test_local_hash_embedder_produces_unit_vectors():
    embedder = LocalHashEmbedder()
    v = embedder.embed("hello world")
    assert len(v) == LOCAL_HASH_DIM
    norm = math.sqrt(sum(x * x for x in v))
    assert abs(norm - 1.0) < 1e-9


def test_local_hash_embedder_similar_texts_have_higher_similarity():
    embedder = LocalHashEmbedder()
    v_cat = embedder.embed("cat kitten feline pet")
    v_dog = embedder.embed("dog puppy canine pet")
    v_car = embedder.embed("car engine vehicle highway")
    s_cat_dog = cosine_similarity(v_cat, v_dog)
    s_cat_car = cosine_similarity(v_cat, v_car)
    assert s_cat_dog > s_cat_car


def test_local_hash_embedder_rejects_non_string():
    embedder = LocalHashEmbedder()
    bad: Any = 123
    with pytest.raises(SemanticSearchError, match="text must be a string"):
        embedder.embed(bad)


def test_local_hash_embedder_rejects_oversized_text():
    embedder = LocalHashEmbedder()
    with pytest.raises(SemanticSearchError, match="exceeds"):
        embedder.embed("a" * 200_000)


def test_cosine_similarity_handles_zero_vectors():
    assert cosine_similarity([0.0, 0.0], [0.0, 0.0]) == 0.0
    assert cosine_similarity([0.0, 1.0], [0.0, 0.0]) == 0.0


def test_cosine_similarity_rejects_length_mismatch():
    with pytest.raises(SemanticSearchError, match="vector length mismatch"):
        cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0])


def test_serialize_deserialize_roundtrip():
    vec = [0.1, -0.2, 0.3, 0.4]
    blob = serialize_vector(vec)
    assert len(blob) == len(vec) * 4
    out = deserialize_vector(blob, len(vec))
    assert out == pytest.approx(vec, abs=1e-6)


# ---------------------------------------------------------------------------
# SemanticSearch CRUD
# ---------------------------------------------------------------------------


def test_index_memory_persists_vector():
    db = _make_test_db()
    with _patched_db(db) as store:
        dim = store.index_memory(memory_id="m1", text="user prefers morning meetings", user_id="u1")
    assert dim == LOCAL_HASH_DIM

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT memory_id, dimension, length(vector) AS blob_len FROM memory_embeddings WHERE memory_id = ?",
        ("m1",),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[1] == LOCAL_HASH_DIM
    assert row[2] == LOCAL_HASH_DIM * 4


def test_index_memory_upserts_on_conflict():
    db = _make_test_db()
    with _patched_db(db) as store:
        store.index_memory(memory_id="m1", text="old text", user_id="u1")
        store.index_memory(memory_id="m1", text="new text", user_id="u1")
    conn = sqlite3.connect(db)
    count = conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0]
    conn.close()
    assert count == 1


def test_index_memory_rejects_empty_text():
    with _patched_db(_make_test_db()) as store, pytest.raises(SemanticSearchError, match="non-empty"):
        store.index_memory(memory_id="m1", text="   ", user_id="u1")


def test_index_memory_rejects_empty_user_id():
    with _patched_db(_make_test_db()) as store, pytest.raises(SemanticSearchError, match="user_id"):
        store.index_memory(memory_id="m1", text="x", user_id="")


def test_delete_memory_embedding_returns_row_count():
    db = _make_test_db()
    with _patched_db(db) as store:
        store.index_memory(memory_id="m1", text="hello", user_id="u1")
        deleted = store.delete_memory_embedding(memory_id="m1", user_id="u1")
    assert deleted == 1


def test_delete_memory_embedding_returns_zero_when_missing():
    with _patched_db(_make_test_db()) as store:
        deleted = store.delete_memory_embedding(memory_id="ghost", user_id="u1")
    assert deleted == 0


# ---------------------------------------------------------------------------
# Semantic search
# ---------------------------------------------------------------------------


def test_search_ranks_relevant_memory_first():
    db = _make_test_db()
    with _patched_db(db) as store:
        store.index_memory(memory_id="m1", text="cat kitten feline pet animal", user_id="u1")
        store.index_memory(memory_id="m2", text="car engine vehicle highway", user_id="u1")
        store.index_memory(memory_id="m3", text="dog puppy canine pet animal", user_id="u1")
        result = store.search(query="feline pet", user_id="u1", limit=3)

    assert isinstance(result, SemanticSearchResult)
    assert result.query == "feline pet"
    assert result.dimension == LOCAL_HASH_DIM
    assert len(result.matches) == 3
    top_ids = [m.memory_id for m in result.matches]
    assert top_ids[0] == "m1"
    assert "m2" in top_ids


def test_search_respects_min_score_threshold():
    db = _make_test_db()
    with _patched_db(db) as store:
        store.index_memory(memory_id="m1", text="cat kitten", user_id="u1")
        store.index_memory(memory_id="m2", text="car engine", user_id="u1")
        result = store.search(query="cat", user_id="u1", limit=10, min_score=0.99)
    assert all(m.score >= 0.99 for m in result.matches)


def test_search_rejects_empty_query():
    with _patched_db(_make_test_db()) as store, pytest.raises(SemanticSearchError, match="non-empty"):
        store.search(query="", user_id="u1")


def test_search_rejects_bad_limit():
    with _patched_db(_make_test_db()) as store:
        with pytest.raises(SemanticSearchError, match="limit must be"):
            store.search(query="x", user_id="u1", limit=0)
        with pytest.raises(SemanticSearchError, match="limit must be"):
            store.search(query="x", user_id="u1", limit=5000)


def test_search_rejects_bad_min_score():
    with _patched_db(_make_test_db()) as store, pytest.raises(SemanticSearchError, match="min_score must be"):
        store.search(query="x", user_id="u1", min_score=2.0)


# ---------------------------------------------------------------------------
# Tenant + user isolation
# ---------------------------------------------------------------------------


def test_search_is_tenant_isolated():
    db = _make_test_db()
    with _patched_db(db) as store:
        store.index_memory(memory_id="m1", text="cat kitten", user_id="u1", tenant_id="tA")
        store.index_memory(memory_id="m1", text="car engine", user_id="u1", tenant_id="tB")
        a_result = store.search(query="cat", user_id="u1", tenant_id="tA")
        b_result = store.search(query="cat", user_id="u1", tenant_id="tB")
    assert len(a_result.matches) == 1
    assert len(b_result.matches) == 1
    a_vec = deserialize_vector(
        sqlite3.connect(db).execute("SELECT vector FROM memory_embeddings WHERE tenant_id='tA'").fetchone()[0],
        LOCAL_HASH_DIM,
    )
    assert a_vec[0] != 0.0 or any(v != 0.0 for v in a_vec)


def test_search_is_user_isolated():
    db = _make_test_db()
    with _patched_db(db) as store:
        store.index_memory(memory_id="m1", text="cat kitten", user_id="alice")
        store.index_memory(memory_id="m1", text="dog puppy", user_id="bob")
        alice = store.search(query="cat", user_id="alice")
        bob = store.search(query="cat", user_id="bob")
    assert {m.memory_id for m in alice.matches} == {"m1"}
    assert {m.memory_id for m in bob.matches} == {"m1"}
    assert alice.matches[0].score != bob.matches[0].score


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


def test_singleton_returns_same_instance(monkeypatch):
    db = _make_test_db()
    monkeypatch.setattr("foresight_mcp.config.DB_PATH", db)
    monkeypatch.setattr(sem_mod, "DB_PATH", db)
    reset_semantic_search()
    a = get_semantic_search()
    b = get_semantic_search()
    assert a is b
    reset_semantic_search()
    c = get_semantic_search()
    assert c is not a


# ---------------------------------------------------------------------------
# Dimension mismatch safety
# ---------------------------------------------------------------------------


def test_search_skips_dim_mismatched_rows(monkeypatch):
    """Embeddings stored with a different dimension must be ignored, not crash."""
    db = _make_test_db()
    with _patched_db(db) as store:
        store.index_memory(memory_id="m1", text="cat", user_id="u1")
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE memory_embeddings SET dimension = 16, vector = ? WHERE memory_id = 'm1'",
            (serialize_vector([0.1] * 16),),
        )
        conn.commit()
        conn.close()
        result = store.search(query="cat", user_id="u1", limit=10)
    assert result.matches == []


# ---------------------------------------------------------------------------
# To-dict shape
# ---------------------------------------------------------------------------


def test_match_and_result_to_dict_shapes():
    m = SemanticMatch(memory_id="m1", score=0.87, provider=DEFAULT_PROVIDER, dimension=384)
    d = m.to_dict()
    assert d == {
        "memory_id": "m1",
        "score": 0.87,
        "provider": DEFAULT_PROVIDER,
        "dimension": 384,
    }

    r = SemanticSearchResult(query="q", provider=DEFAULT_PROVIDER, dimension=384, matches=[m])
    rd = r.to_dict()
    assert rd["query"] == "q"
    assert rd["provider"] == DEFAULT_PROVIDER
    assert rd["dimension"] == 384
    assert rd["matches"] == [d]
