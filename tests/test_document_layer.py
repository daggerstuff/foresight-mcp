"""Tests for MEM-7: Document Layer."""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from foresight_mcp import document_layer as doc_mod
from foresight_mcp.document_layer import (
    DEFAULT_CHUNK_CHAR_BUDGET,
    VALID_DOCUMENT_SOURCES,
    Document,
    DocumentChunk,
    DocumentLayerError,
    DocumentStore,
    chunk_text,
    content_hash,
    extract_memories_from_text,
    get_document_store,
    reset_document_store,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_db() -> str:
    """Create a temporary DB with documents + document_chunks tables."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            title TEXT NOT NULL,
            source TEXT NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            char_count INTEGER NOT NULL,
            chunk_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            metadata TEXT DEFAULT '{}',
            UNIQUE(tenant_id, user_id, content_hash)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS document_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            memory_id TEXT,
            chunk_index INTEGER NOT NULL,
            start_offset INTEGER NOT NULL,
            end_offset INTEGER NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(document_id, chunk_index),
            FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
        )"""
    )
    conn.commit()
    conn.close()
    return db_path


@contextmanager
def _patched_store(db_path: str) -> Iterator[DocumentStore]:
    reset_document_store()
    with (
        patch.object(doc_mod, "DB_PATH", db_path),
        patch("foresight_mcp.config.DB_PATH", db_path),
        patch("foresight_mcp.document_layer.DB_PATH", db_path),
    ):
        store = DocumentStore(db_path)
        try:
            yield store
        finally:
            reset_document_store()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_default_chunk_budget_in_range():
    assert DEFAULT_CHUNK_CHAR_BUDGET >= 100
    assert DEFAULT_CHUNK_CHAR_BUDGET <= 8_000


def test_valid_sources_contains_expected_set():
    assert frozenset({"transcript", "article", "journal", "note", "email", "other"}) == VALID_DOCUMENT_SOURCES


# ---------------------------------------------------------------------------
# content_hash
# ---------------------------------------------------------------------------


def test_content_hash_is_deterministic():
    assert content_hash("hello world") == content_hash("hello world")
    assert content_hash("a") != content_hash("b")


def test_content_hash_is_64_char_hex():
    h = content_hash("anything")
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# chunk_text
# ---------------------------------------------------------------------------


def test_chunk_text_empty_returns_empty():
    assert chunk_text("") == []


def test_chunk_text_splits_on_paragraph_breaks():
    text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
    chunks = chunk_text(text, char_budget=200)
    # All three short paragraphs pack into a single chunk under a 200-char budget.
    assert len(chunks) == 1
    assert chunks[0][2] == "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."


def test_chunk_text_splits_when_overflow():
    text = "A" * 50 + "\n\n" + "B" * 50 + "\n\n" + "C" * 50
    chunks = chunk_text(text, char_budget=105)
    assert len(chunks) == 2
    assert "A" * 50 in chunks[0][2]
    assert "B" * 50 in chunks[0][2]
    assert "C" * 50 in chunks[1][2]


def test_chunk_text_each_paragraph_alone_when_no_two_fit():
    text = "A" * 50 + "\n\n" + "B" * 50 + "\n\n" + "C" * 50
    chunks = chunk_text(text, char_budget=100)
    assert len(chunks) == 3
    assert [c[2] for c in chunks] == ["A" * 50, "B" * 50, "C" * 50]


def test_chunk_text_offsets_are_absolute():
    text = "alpha\n\nbeta\n\ngamma"
    chunks = chunk_text(text, char_budget=100)
    for start, end, chunk in chunks:
        assert text[start:end].rstrip() == chunk


def test_chunk_text_packs_short_paragraphs_into_one_chunk():
    text = "short one\n\nshort two\n\nshort three"
    chunks = chunk_text(text, char_budget=200)
    assert len(chunks) == 1
    assert "short one" in chunks[0][2]
    assert "short three" in chunks[0][2]


def test_chunk_text_emits_overflow_chunks_as_is():
    long = "x" * 2000
    chunks = chunk_text(long, char_budget=100)
    assert len(chunks) >= 1
    assert all(len(c[2]) >= 100 for c in chunks)


def test_chunk_text_rejects_out_of_range_budget():
    with pytest.raises(DocumentLayerError, match="chunk_char_budget"):
        chunk_text("anything", char_budget=50)
    with pytest.raises(DocumentLayerError, match="chunk_char_budget"):
        chunk_text("anything", char_budget=10_000)


# ---------------------------------------------------------------------------
# extract_memories_from_text (stub)
# ---------------------------------------------------------------------------


def test_extract_memories_returns_pending_chunks():
    text = "para one\n\n" + "x" * 100 + "\n\n" + "y" * 100
    chunks = extract_memories_from_text(text, char_budget=200)
    assert all(isinstance(c, DocumentChunk) for c in chunks)
    assert all(c.document_id == "pending" for c in chunks)
    assert all(c.memory_id == "pending" for c in chunks)
    assert len(chunks) == 2
    assert "para one" in chunks[0].text
    assert "x" * 100 in chunks[0].text
    assert chunks[1].text == "y" * 100


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_create_document_persists_doc_and_chunks():
    db = _make_test_db()
    with _patched_store(db) as store:
        text = "alpha paragraph.\n\n" + "b" * 200 + "\n\ngamma paragraph."
        doc, chunks = store.create_document(
            title="My Notes",
            content=text,
            user_id="u1",
            source="note",
            char_budget=220,
        )
    assert isinstance(doc, Document)
    assert doc.title == "My Notes"
    assert doc.source == "note"
    assert doc.content == text
    assert doc.char_count == len(text)
    assert doc.content_hash == content_hash(text)
    assert len(chunks) == 2
    assert all(isinstance(c, DocumentChunk) for c in chunks)
    assert all(c.memory_id == "" for c in chunks)
    assert "alpha paragraph." in chunks[0].text
    assert "b" * 200 in chunks[0].text
    assert chunks[1].text == "gamma paragraph."


def test_create_document_with_string_memory_id_for_chunk():
    db = _make_test_db()
    with _patched_store(db) as store:
        _doc, chunks = store.create_document(
            title="t",
            content="a\n\n" + "b" * 300,
            user_id="u1",
            memory_id_for_chunk="mem_1",
        )
    assert all(c.memory_id == "mem_1" for c in chunks)


def test_create_document_with_callable_memory_id_for_chunk():
    db = _make_test_db()
    with _patched_store(db) as store:

        def _id_fn(idx: int, text: str) -> str:
            return f"mem_{idx}_{len(text)}"

        _doc, chunks = store.create_document(
            title="t",
            content="alpha\n\n" + "b" * 300 + "\n\ngamma",
            user_id="u1",
            memory_id_for_chunk=_id_fn,
            char_budget=310,
        )
    assert len(chunks) == 2
    assert chunks[0].memory_id == "mem_0_307"
    assert chunks[1].memory_id == "mem_1_5"


def test_create_document_rejects_duplicate_content():
    db = _make_test_db()
    with _patched_store(db) as store:
        store.create_document(title="t", content="same content", user_id="u1")
        with pytest.raises(DocumentLayerError, match="already exists"):
            store.create_document(title="t2", content="same content", user_id="u1")


def test_create_document_rejects_invalid_source():
    with _patched_store(_make_test_db()) as store, pytest.raises(DocumentLayerError, match="source must be"):
        store.create_document(title="t", content="x", user_id="u1", source="bogus")


def test_create_document_rejects_empty_title():
    with _patched_store(_make_test_db()) as store, pytest.raises(DocumentLayerError, match="title"):
        store.create_document(title="", content="x", user_id="u1")


def test_create_document_rejects_empty_content():
    with _patched_store(_make_test_db()) as store, pytest.raises(DocumentLayerError, match="content"):
        store.create_document(title="t", content="", user_id="u1")


def test_create_document_rejects_oversized_content():
    with _patched_store(_make_test_db()) as store, pytest.raises(DocumentLayerError, match="exceeds"):
        store.create_document(title="t", content="a" * 300_000, user_id="u1")


def test_create_document_rejects_empty_user_id():
    with _patched_store(_make_test_db()) as store, pytest.raises(DocumentLayerError, match="user_id"):
        store.create_document(title="t", content="x", user_id="")


def test_create_document_rejects_out_of_range_budget():
    with _patched_store(_make_test_db()) as store, pytest.raises(DocumentLayerError, match="chunk_char_budget"):
        store.create_document(title="t", content="x", user_id="u1", char_budget=10)


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def test_get_document_returns_none_for_missing():
    with _patched_store(_make_test_db()) as store:
        assert store.get_document(document_id="nope", user_id="u1") is None


def test_get_document_round_trip():
    db = _make_test_db()
    with _patched_store(db) as store:
        doc, _ = store.create_document(title="t", content="hello", user_id="u1", metadata={"k": "v"})
        fetched = store.get_document(document_id=doc.id, user_id="u1")
    assert fetched is not None
    assert fetched.id == doc.id
    assert fetched.title == "t"
    assert fetched.metadata == {"k": "v"}


def test_list_chunks_returns_ordered_chunks():
    db = _make_test_db()
    with _patched_store(db) as store:
        doc, _ = store.create_document(
            title="t",
            content="a" * 500 + "\n\n" + "b" * 500 + "\n\n" + "c" * 500 + "\n\n" + "d" * 500,
            user_id="u1",
        )
        chunks = store.list_chunks(document_id=doc.id, user_id="u1")
    assert [c.chunk_index for c in chunks] == [0, 1, 2, 3]
    assert chunks[0].text.startswith("a")
    assert chunks[1].text.startswith("b")
    assert chunks[2].text.startswith("c")
    assert chunks[3].text.startswith("d")


def test_get_memory_source_reverse_lookup():
    db = _make_test_db()
    with _patched_store(db) as store:

        def _id_fn(idx: int, text: str) -> str:
            return f"mem_{idx}"

        doc, _chunks = store.create_document(
            title="t",
            content="alpha content\n\n" + "b" * 500,
            user_id="u1",
            memory_id_for_chunk=_id_fn,
            char_budget=200,
        )
        result = store.get_memory_source(memory_id="mem_1", user_id="u1")
    assert result is not None
    fetched_doc, fetched_chunk = result
    assert fetched_doc.id == doc.id
    assert fetched_chunk.memory_id == "mem_1"
    assert "b" * 100 in fetched_chunk.text


def test_get_memory_source_returns_none_for_unknown_memory():
    with _patched_store(_make_test_db()) as store:
        assert store.get_memory_source(memory_id="ghost", user_id="u1") is None


# ---------------------------------------------------------------------------
# Tenant + user isolation
# ---------------------------------------------------------------------------


def test_documents_are_tenant_isolated():
    db = _make_test_db()
    with _patched_store(db) as store:
        doc_a, _ = store.create_document(title="t", content="hello", user_id="u1", tenant_id="tA")
        doc_b, _ = store.create_document(title="t", content="hello", user_id="u1", tenant_id="tB")
        same_hash_docs = [d for d in (doc_a, doc_b) if d is not None]
    assert len(same_hash_docs) == 2
    assert doc_a.id != doc_b.id
    a_again = store.get_document(document_id=doc_a.id, user_id="u1", tenant_id="tA")
    assert a_again is not None
    assert a_again.tenant_id == "tA"
    b_again = store.get_document(document_id=doc_b.id, user_id="u1", tenant_id="tB")
    assert b_again is not None
    assert b_again.tenant_id == "tB"


def test_documents_are_user_isolated():
    db = _make_test_db()
    with _patched_store(db) as store:
        doc_alice, _ = store.create_document(title="alice notes", content="hello", user_id="alice")
        doc_bob, _ = store.create_document(title="bob notes", content="hello", user_id="bob")
        alice_doc = store.get_document(document_id=doc_alice.id, user_id="alice")
        alice_via_bob = store.get_document(document_id=doc_bob.id, user_id="alice")
    assert alice_doc is not None
    assert alice_doc.user_id == "alice"
    assert alice_via_bob is None


def test_list_chunks_rejects_other_tenant():
    db = _make_test_db()
    with _patched_store(db) as store:
        doc, _ = store.create_document(title="t", content="a\n\nb", user_id="u1", tenant_id="tA")
        chunks = store.list_chunks(document_id=doc.id, user_id="u1", tenant_id="tB")
    assert chunks == []


# ---------------------------------------------------------------------------
# Delete + cascade
# ---------------------------------------------------------------------------


def test_delete_document_cascades_to_chunks():
    db = _make_test_db()
    with _patched_store(db) as store:
        doc, _ = store.create_document(title="t", content="a\n\nb\n\nc", user_id="u1")
        deleted = store.delete_document(document_id=doc.id, user_id="u1")
    assert deleted == 1
    conn = sqlite3.connect(db)
    chunk_count = conn.execute(
        "SELECT COUNT(*) FROM document_chunks WHERE document_id = ?",
        (doc.id,),
    ).fetchone()[0]
    conn.close()
    assert chunk_count == 0


def test_delete_document_returns_zero_when_missing():
    with _patched_store(_make_test_db()) as store:
        assert store.delete_document(document_id="nope", user_id="u1") == 0


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


def test_singleton_returns_same_instance(monkeypatch):
    db = _make_test_db()
    monkeypatch.setattr("foresight_mcp.config.DB_PATH", db)
    monkeypatch.setattr(doc_mod, "DB_PATH", db)
    reset_document_store()
    a = get_document_store()
    b = get_document_store()
    assert a is b
    reset_document_store()
    c = get_document_store()
    assert c is not a


# ---------------------------------------------------------------------------
# to_dict shapes
# ---------------------------------------------------------------------------


def test_document_to_dict_shape():
    d = Document(
        id="d1",
        tenant_id="t1",
        user_id="u1",
        title="T",
        source="note",
        content="x",
        content_hash="abc",
        char_count=1,
        chunk_count=1,
        created_at="2024-01-01T00:00:00",
        updated_at="2024-01-01T00:00:00",
        metadata={"k": "v"},
    )
    out = d.to_dict()
    assert out["id"] == "d1"
    assert out["metadata"] == {"k": "v"}
    assert out["char_count"] == 1


def test_chunk_to_dict_shape():
    c = DocumentChunk(
        document_id="d1",
        memory_id="m1",
        start_offset=0,
        end_offset=5,
        text="hello",
        chunk_index=0,
    )
    out = c.to_dict()
    assert out == {
        "document_id": "d1",
        "memory_id": "m1",
        "start_offset": 0,
        "end_offset": 5,
        "text": "hello",
        "chunk_index": 0,
    }
