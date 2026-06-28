"""Tests for graph store tenant isolation."""

import os
import sqlite3
import tempfile

from foresight_mcp.entity_extractor import Entity, Relationship
from foresight_mcp.graph_store import GraphStore
from foresight_mcp.tenant_context import reset_tenant_context, set_current_tenant_id


def _fresh_store():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = GraphStore(db_path)
    return store, db_path


def test_entity_has_tenant_id():
    store, db_path = _fresh_store()
    try:
        entity = Entity(id="e1", name="Alice", entity_type="person")
        store.upsert_entity(entity, user_id="u1", tenant_id="acme-corp")

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT tenant_id FROM memory_entities WHERE id = ?", ("e1",)).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "acme-corp"
    finally:
        os.unlink(db_path)


def test_entity_isolated_by_tenant():
    store, db_path = _fresh_store()
    try:
        entity = Entity(id="e1", name="Alice", entity_type="person")
        store.upsert_entity(entity, user_id="u1", tenant_id="acme-corp")

        result = store.get_entity("e1", user_id="u1", tenant_id="other-corp")
        assert result is None

        # Same tenant should find it
        result = store.get_entity("e1", user_id="u1", tenant_id="acme-corp")
        assert result is not None
        assert result.name == "Alice"
    finally:
        os.unlink(db_path)


def test_entities_by_type_scoped_to_tenant():
    store, db_path = _fresh_store()
    try:
        e1 = Entity(id="e1", name="Alice", entity_type="person")
        e2 = Entity(id="e2", name="Bob", entity_type="person")
        store.upsert_entity(e1, user_id="u1", tenant_id="tenant-a")
        store.upsert_entity(e2, user_id="u1", tenant_id="tenant-b")

        results_a = store.get_entities_by_type("u1", "person", tenant_id="tenant-a")
        assert len(results_a) == 1
        assert results_a[0].name == "Alice"

        results_b = store.get_entities_by_type("u1", "person", tenant_id="tenant-b")
        assert len(results_b) == 1
        assert results_b[0].name == "Bob"
    finally:
        os.unlink(db_path)


def test_relationship_scoped_to_tenant():
    store, db_path = _fresh_store()
    try:
        e1 = Entity(id="e1", name="Alice", entity_type="person")
        e2 = Entity(id="e2", name="anxiety", entity_type="emotion")
        store.upsert_entity(e1, user_id="u1", tenant_id="tenant-a")
        store.upsert_entity(e2, user_id="u1", tenant_id="tenant-a")

        rel = Relationship(
            source_entity_id="e1", target_entity_id="e2", relationship_type="experienced", confidence=0.9
        )
        store.add_relationship(rel, user_id="u1", tenant_id="tenant-a")

        rels_a = store.get_relationships("e1", user_id="u1", tenant_id="tenant-a")
        assert len(rels_a) == 1

        rels_b = store.get_relationships("e1", user_id="u1", tenant_id="tenant-b")
        assert len(rels_b) == 0
    finally:
        os.unlink(db_path)


def test_memory_entity_link_scoped_to_tenant():
    store, db_path = _fresh_store()
    try:
        store.link_memory_to_entities("mem1", ["e1"], user_id="u1", tenant_id="tenant-a")

        results_a = store.get_memories_for_entity("e1", user_id="u1", tenant_id="tenant-a")
        assert "mem1" in results_a

        results_b = store.get_memories_for_entity("e1", user_id="u1", tenant_id="tenant-b")
        assert "mem1" not in results_b
    finally:
        os.unlink(db_path)


def test_contextvar_used_as_default_tenant():
    reset_tenant_context()
    set_current_tenant_id("from-contextvar")
    store, db_path = _fresh_store()
    try:
        entity = Entity(id="e1", name="Alice", entity_type="person")
        store.upsert_entity(entity, user_id="u1")  # No explicit tenant_id

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT tenant_id FROM memory_entities WHERE id = ?", ("e1",)).fetchone()
        conn.close()
        assert row[0] == "from-contextvar"
    finally:
        reset_tenant_context()
        os.unlink(db_path)


def test_migration_adds_tenant_id_to_existing_db():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        # Create old schema without tenant_id
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE memory_entities (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                description TEXT,
                properties TEXT DEFAULT '{}',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, name, entity_type)
            )
        """)
        conn.commit()
        conn.close()

        # Run migration
        from foresight_mcp.backend import SqliteBackend
        from foresight_mcp.migrations import run_migrations

        backend = SqliteBackend(db_path=db_path)
        backend.connect()
        try:
            run_migrations(backend)
        finally:
            backend.close()

        # Verify column exists
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(memory_entities)")
        columns = [row[1] for row in cursor.fetchall()]
        conn.close()
        assert "tenant_id" in columns

        # Verify index exists
        conn = sqlite3.connect(db_path)
        indexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='memory_entities'"
        ).fetchall()
        conn.close()
        index_names = [i[0] for i in indexes]
        assert "idx_memory_entities_tenant" in index_names
    finally:
        os.unlink(db_path)
