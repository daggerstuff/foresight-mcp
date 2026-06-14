"""Tests for MEM-4: Memory Relationship Store."""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from foresight_mcp import memory_relationships as rel_mod
from foresight_mcp.memory_relationships import (
    VALID_RELATIONSHIP_TYPES,
    MemoryGraphTraversal,
    MemoryRelationship,
    MemoryRelationshipError,
    MemoryRelationshipStore,
    get_memory_relationship_store,
    reset_memory_relationship_store,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_db() -> str:
    """Create a temporary DB with memories + memory_relationships tables."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY, content TEXT NOT NULL,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT DEFAULT 'default',
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS memory_relationships (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            source_memory_id TEXT NOT NULL,
            target_memory_id TEXT NOT NULL,
            relationship_type TEXT NOT NULL
                CHECK(relationship_type IN (
                    'updates', 'extends', 'derives',
                    'contradicts', 'supports', 'related'
                )),
            confidence REAL DEFAULT 1.0 CHECK(confidence >= 0 AND confidence <= 1),
            metadata TEXT DEFAULT '{}',
            created_at TEXT NOT NULL,
            UNIQUE(tenant_id, user_id, source_memory_id, target_memory_id, relationship_type)
        )"""
    )
    conn.commit()
    conn.close()
    return db_path


def _seed_memories(db_path: str, ids: list[str], tenant_id: str = "t1", user_id: str = "u1") -> None:
    conn = sqlite3.connect(db_path)
    now = datetime.now(timezone.utc).isoformat()
    for mid in ids:
        conn.execute(
            "INSERT OR IGNORE INTO memories (id, content, tenant_id, user_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (mid, f"content-{mid}", tenant_id, user_id, now),
        )
    conn.commit()
    conn.close()


@contextmanager
def _patched_db(db_path: str) -> Iterator[MemoryRelationshipStore]:
    """Patch DB_PATH for get_pool + reset singleton, yield a fresh store."""
    reset_memory_relationship_store()
    with (
        patch.object(rel_mod, "DB_PATH", db_path),
        patch("foresight_mcp.config.DB_PATH", db_path),
        patch("foresight_mcp.memory_relationships.DB_PATH", db_path),
    ):
        store = MemoryRelationshipStore(db_path)
        try:
            yield store
        finally:
            reset_memory_relationship_store()


# ---------------------------------------------------------------------------
# Constants and validation
# ---------------------------------------------------------------------------


def test_valid_relationship_types_contains_expected_set():
    assert (
        frozenset({"updates", "extends", "derives", "contradicts", "supports", "related"}) == VALID_RELATIONSHIP_TYPES
    )


def test_link_rejects_invalid_relationship_type():
    with (
        _patched_db(_make_test_db()) as store,
        pytest.raises(MemoryRelationshipError, match="relationship_type must be one of"),
    ):
        store.link_memories(
            source_memory_id="a",
            target_memory_id="b",
            relationship_type="bogus",
            user_id="u1",
        )


def test_link_rejects_self_loop():
    with _patched_db(_make_test_db()) as store, pytest.raises(MemoryRelationshipError, match="must differ"):
        store.link_memories(
            source_memory_id="same",
            target_memory_id="same",
            relationship_type="updates",
            user_id="u1",
        )


def test_link_rejects_out_of_range_confidence():
    with _patched_db(_make_test_db()) as store, pytest.raises(MemoryRelationshipError, match="confidence must be in"):
        store.link_memories(
            source_memory_id="a",
            target_memory_id="b",
            relationship_type="updates",
            user_id="u1",
            confidence=1.5,
        )


def test_link_rejects_empty_user_id():
    with _patched_db(_make_test_db()) as store, pytest.raises(MemoryRelationshipError, match="user_id must be"):
        store.link_memories(
            source_memory_id="a",
            target_memory_id="b",
            relationship_type="updates",
            user_id="",
        )


def test_link_rejects_oversized_metadata():
    with _patched_db(_make_test_db()) as store:
        big = {"blob": "x" * 20_000}
        with pytest.raises(MemoryRelationshipError, match="metadata exceeds"):
            store.link_memories(
                source_memory_id="a",
                target_memory_id="b",
                relationship_type="updates",
                user_id="u1",
                metadata=big,
            )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_link_memories_creates_relationship():
    db = _make_test_db()
    _seed_memories(db, ["src", "dst"])
    with _patched_db(db) as store:
        rel = store.link_memories(
            source_memory_id="src",
            target_memory_id="dst",
            relationship_type="extends",
            user_id="u1",
            metadata={"reason": "additional detail"},
        )
    assert isinstance(rel, MemoryRelationship)
    assert rel.source_memory_id == "src"
    assert rel.target_memory_id == "dst"
    assert rel.relationship_type == "extends"
    assert rel.user_id == "u1"
    assert rel.confidence == 1.0
    assert rel.metadata == {"reason": "additional detail"}


def test_link_memories_upserts_on_conflict():
    db = _make_test_db()
    _seed_memories(db, ["src", "dst"])
    with _patched_db(db) as store:
        store.link_memories(
            source_memory_id="src",
            target_memory_id="dst",
            relationship_type="updates",
            user_id="u1",
            confidence=0.5,
        )
        rel2 = store.link_memories(
            source_memory_id="src",
            target_memory_id="dst",
            relationship_type="updates",
            user_id="u1",
            confidence=0.9,
            metadata={"v": 2},
        )
    rels = store.get_relationships_for_memory(
        memory_id="src", user_id="u1", direction="out", relationship_type="updates"
    )
    assert len(rels) == 1
    assert rels[0].confidence == 0.9
    assert rels[0].metadata == {"v": 2}
    assert rel2.id == rels[0].id


def test_get_relationships_filters_by_direction():
    db = _make_test_db()
    _seed_memories(db, ["a", "b", "c"])
    with _patched_db(db) as store:
        store.link_memories("a", "b", "extends", user_id="u1")
        store.link_memories("c", "a", "supports", user_id="u1")

        out = store.get_relationships_for_memory("a", "u1", direction="out")
        inc = store.get_relationships_for_memory("a", "u1", direction="in")
        both = store.get_relationships_for_memory("a", "u1", direction="both")

    assert len(out) == 1
    assert out[0].target_memory_id == "b"
    assert len(inc) == 1
    assert inc[0].source_memory_id == "c"
    assert len(both) == 2


def test_get_relationships_rejects_bad_direction():
    with _patched_db(_make_test_db()) as store, pytest.raises(MemoryRelationshipError, match="direction must be"):
        store.get_relationships_for_memory("a", "u1", direction="sideways")


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


def test_relationships_are_tenant_isolated():
    db = _make_test_db()
    _seed_memories(db, ["m1", "m2"], tenant_id="tA", user_id="u1")
    _seed_memories(db, ["m1", "m2"], tenant_id="tB", user_id="u1")
    with _patched_db(db) as store:
        store.link_memories("m1", "m2", "extends", user_id="u1", tenant_id="tA")

        a_rels = store.get_relationships_for_memory("m1", "u1", tenant_id="tA", direction="out")
        b_rels = store.get_relationships_for_memory("m1", "u1", tenant_id="tB", direction="out")

    assert len(a_rels) == 1
    assert b_rels == []


def test_relationships_are_user_isolated():
    db = _make_test_db()
    _seed_memories(db, ["m1", "m2"], user_id="alice")
    _seed_memories(db, ["m1", "m2"], user_id="bob")
    with _patched_db(db) as store:
        store.link_memories("m1", "m2", "extends", user_id="alice", tenant_id="t1")
        store.link_memories("m1", "m2", "extends", user_id="bob", tenant_id="t1")

        alice_rels = store.get_relationships_for_memory("m1", "alice", tenant_id="t1", direction="out")
        bob_rels = store.get_relationships_for_memory("m1", "bob", tenant_id="t1", direction="out")

    assert len(alice_rels) == 1
    assert len(bob_rels) == 1
    assert alice_rels[0].id != bob_rels[0].id


# ---------------------------------------------------------------------------
# Graph traversal
# ---------------------------------------------------------------------------


def test_traverse_walks_chain_in_both_directions():
    db = _make_test_db()
    _seed_memories(db, ["a", "b", "c", "d"])
    with _patched_db(db) as store:
        store.link_memories("a", "b", "extends", user_id="u1")
        store.link_memories("b", "c", "derives", user_id="u1")
        store.link_memories("c", "d", "extends", user_id="u1")
        result = store.traverse_memory_graph("a", "u1", max_depth=2)

    assert isinstance(result, MemoryGraphTraversal)
    node_ids = {n["memory_id"] for n in result.nodes}
    assert {"a", "b", "c"}.issubset(node_ids)
    assert "d" not in node_ids
    assert len(result.edges) >= 2


def test_traverse_depth_zero_returns_only_root():
    db = _make_test_db()
    _seed_memories(db, ["a", "b"])
    with _patched_db(db) as store:
        store.link_memories("a", "b", "extends", user_id="u1")
        result = store.traverse_memory_graph("a", "u1", max_depth=0)

    assert {n["memory_id"] for n in result.nodes} == {"a"}


def test_traverse_rejects_out_of_range_depth():
    with _patched_db(_make_test_db()) as store, pytest.raises(MemoryRelationshipError, match="max_depth"):
        store.traverse_memory_graph("a", "u1", max_depth=10)


def test_traverse_rejects_oversized_limit():
    with _patched_db(_make_test_db()) as store, pytest.raises(MemoryRelationshipError, match="limit must be"):
        store.traverse_memory_graph("a", "u1", limit=10_000)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def test_singleton_returns_same_instance(monkeypatch):
    db = _make_test_db()
    monkeypatch.setattr("foresight_mcp.config.DB_PATH", db)
    monkeypatch.setattr(rel_mod, "DB_PATH", db)
    reset_memory_relationship_store()
    a = get_memory_relationship_store()
    b = get_memory_relationship_store()
    assert a is b
    reset_memory_relationship_store()
    c = get_memory_relationship_store()
    assert c is not a


# ---------------------------------------------------------------------------
# Schema field persistence (UnifiedMemory)
# ---------------------------------------------------------------------------


def test_unified_memory_round_trips_relationship_fields():
    from foresight_mcp.schema import UnifiedMemory

    m = UnifiedMemory.create(content="x", user_id="u1", relation_type="extends", related_memory_id="abc")
    assert m.relation_type == "extends"
    assert m.related_memory_id == "abc"

    row = m.to_sqlite_row()
    assert row["relation_type"] == "extends"
    assert row["related_memory_id"] == "abc"

    m2 = UnifiedMemory.from_sqlite_row({**row, "emotional_context": "{}", "metrics": "{}"})
    assert m2.relation_type == "extends"
    assert m2.related_memory_id == "abc"


def test_unified_memory_defaults_relationship_fields_to_none():
    from foresight_mcp.schema import UnifiedMemory

    m = UnifiedMemory.create(content="x", user_id="u1")
    assert m.relation_type is None
    assert m.related_memory_id is None
