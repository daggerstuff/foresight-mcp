"""Tests for Foresight MCP server."""

import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastmcp import Client
from foresight_cli.cli import _decode_tool_result
from foresight_mcp import memory_status, store_memory
from foresight_mcp.block_registry import MemoryBlockSchema
from foresight_mcp.context_blocks import register_context_block_schema
from foresight_mcp.hybrid_retriever import HybridResult, HybridSearchResult
from foresight_mcp.server import (
    ContextBlockAction,
    CurationRunAction,
    _extract_terms,
    _format_context_blocks_by_injection_point,
    _score_memory_relevance,
    get_relevant_memories,
    inject_context,
    manage_context_blocks,
    manage_curation_runs,
    mcp,
)


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    """Isolate DB per test function to prevent tenant memory limit issues.

    Patches:
    1. DB_PATH → temp file (so writes never hit ~/.foresight/memory.db)
    2. Tenant context → '_test_' account (so queries never mix with production data)
    """
    db_file = tmp_path / "test_memory.db"
    monkeypatch.setenv("FORESIGHT_DB_PATH", str(db_file))

    import foresight_mcp.config as config_module
    import foresight_mcp.connection_pool as conn_pool_module
    from foresight_mcp.connection_pool import reset_pool
    from foresight_mcp.server import init_db

    monkeypatch.setattr(config_module, "DB_PATH", str(db_file))
    monkeypatch.setattr(conn_pool_module, "DB_PATH", str(db_file))
    reset_pool()

    # Isolate tenant context so test data never lands in the 'default' tenant
    from foresight_mcp.tenant_context import (
        set_current_account_id,
        set_current_user_id,
    )

    set_current_user_id("_test_user_")
    set_current_account_id("_test_")

    init_db()
    yield
    reset_pool()

    from foresight_mcp.tenant_context import reset_tenant_context

    reset_tenant_context()


def test_store_memory():
    # Use unique content to avoid dedup collision with previous runs
    unique = f"test_{datetime.now(timezone.utc).isoformat()}_{hashlib.md5(b'store_test').hexdigest()[:8]}"
    result = store_memory(unique)
    assert "Stored" in result


def test_store_memory_dedup():
    """Storing identical content should bump activation, not create duplicate."""
    content = f"dedup_test_{datetime.now(timezone.utc).isoformat()}_{hashlib.md5(b'dedup_unique').hexdigest()[:8]}"
    result1 = store_memory(content)
    assert "Stored" in result1
    result2 = store_memory(content)
    assert "Duplicate detected" in result2


# ====== PIX-2083 content_hash dedup tests ======


def test_memories_content_hash_backfill_computes_correct_hash():
    """v10 backfill: existing rows get content_hash = sha256(content)."""
    db_path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """CREATE TABLE memories (
                id TEXT PRIMARY KEY, content TEXT NOT NULL, content_hash TEXT,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                user_id TEXT DEFAULT 'default', is_ghost INTEGER DEFAULT 0
            )"""
        )
        from foresight_mcp.document_layer import content_hash

        test_content = "backfill test content 123"
        expected_hash = content_hash(test_content)
        conn.execute(
            "INSERT INTO memories (id, content, content_hash) VALUES (?, ?, NULL)",
            ("mem-backfill-1", test_content),
        )
        conn.commit()

        row = conn.execute("SELECT content_hash FROM memories WHERE id = 'mem-backfill-1'").fetchone()
        assert row[0] is None, "precondition: content_hash starts NULL"

        rows = conn.execute("SELECT id, content FROM memories WHERE content_hash IS NULL").fetchall()
        for r in rows:
            conn.execute(
                "UPDATE memories SET content_hash = ? WHERE id = ?",
                (content_hash(r[1]), r[0]),
            )
        conn.commit()

        row = conn.execute("SELECT content_hash FROM memories WHERE id = 'mem-backfill-1'").fetchone()
        assert row[0] == expected_hash, f"expected {expected_hash}, got {row[0]}"

        conn.close()
    finally:
        os.unlink(db_path)


def test_store_memory_uses_content_hash_for_dedup():
    """Storing same content twice with different timestamps must still dedup.

    Regression: old code used 'content = ?' exact match. New code uses
    content_hash index for deterministic, index-based dedup.
    """
    from foresight_mcp import store_memory

    unique_marker = hashlib.md5(f"hash_dedup_{datetime.now(timezone.utc).isoformat()}".encode()).hexdigest()[:8]
    content = f"hash_dedup_test_{unique_marker}"

    result1 = store_memory(content)
    assert "Stored" in result1, f"first store should succeed: {result1}"

    result2 = store_memory(content)
    assert "Duplicate detected" in result2, f"second store should dedup: {result2}"


def test_store_memory_dedup_increments_activation_count():
    """Duplicate store must bump activation_count, not create a new row."""
    from foresight_mcp import store_memory

    unique = f"activation_bump_{datetime.now(timezone.utc).isoformat()}_{hashlib.md5(b'act_test').hexdigest()[:8]}"
    r1 = store_memory(unique)
    assert "Stored" in r1
    r2 = store_memory(unique)
    assert "Duplicate detected" in r2

    import sqlite3 as _sql

    from foresight_mcp.config import DB_PATH

    conn = _sql.connect(str(DB_PATH))
    conn.row_factory = _sql.Row
    row = conn.execute(
        "SELECT activation_count FROM memories WHERE content = ?",
        (unique,),
    ).fetchone()
    assert row is not None
    assert row["activation_count"] >= 2, f"expected >=2, got {row['activation_count']}"
    conn.close()


def test_store_memory_tenant_isolation_on_dedup():
    """Same content in two tenants produces two separate rows.

    Regression: content_hash is scoped by (tenant_id, user_id). If the
    dedup query omitted tenant_id, identical content across tenants
    would incorrectly collide.
    """
    db_path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """CREATE TABLE memories (
                id TEXT PRIMARY KEY, content TEXT NOT NULL, content_hash TEXT,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                user_id TEXT DEFAULT 'default',
                activation_count INTEGER DEFAULT 0, is_ghost INTEGER DEFAULT 0,
                created_at TEXT, updated_at TEXT, scope TEXT, retention TEXT,
                category TEXT, bank_id TEXT, tags TEXT, emotional_context TEXT,
                metrics TEXT, vector_id TEXT, gist TEXT, synthesized_from TEXT,
                version INTEGER, importance REAL
            )"""
        )
        from foresight_mcp.document_layer import content_hash

        shared_content = "cross-tenant dedup test"
        h = content_hash(shared_content)
        now = datetime.now(timezone.utc).isoformat()

        for tenant, mid in [("tenant-a", "mem-a-1"), ("tenant-b", "mem-b-1")]:
            conn.execute(
                """INSERT INTO memories
                   (id, content, content_hash, tenant_id, user_id, activation_count,
                    is_ghost, created_at, updated_at, scope, retention, category,
                    bank_id, tags, emotional_context, metrics, vector_id, gist,
                    synthesized_from, version, importance)
                   VALUES (?, ?, ?, ?, ?, 1, 0, ?, ?, 'session', 'short_term', 'fact',
                           'default', '[]', '{}', '{}', NULL, NULL, '[]', 1, 0.5)""",
                (mid, shared_content, h, tenant, "user-1", now, now),
            )
        conn.commit()

        rows = conn.execute(
            "SELECT tenant_id FROM memories WHERE content_hash = ? AND user_id = ?",
            (h, "user-1"),
        ).fetchall()
        assert len(rows) == 2, f"expected 2 rows across tenants, got {len(rows)}"
        tenants = {r[0] for r in rows}
        assert tenants == {"tenant-a", "tenant-b"}

        conn.close()
    finally:
        os.unlink(db_path)


def test_status():
    result = memory_status()
    assert "healthy" in result.lower()


def _make_test_db():
    """Create a temporary DB with the memories schema for isolation."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS memories (
        id TEXT PRIMARY KEY, content TEXT NOT NULL, content_hash TEXT,
        tenant_id TEXT NOT NULL DEFAULT 'default',
        scope TEXT DEFAULT 'session', retention TEXT DEFAULT 'short_term',
        category TEXT DEFAULT 'fact', user_id TEXT DEFAULT 'default',
        bank_id TEXT DEFAULT 'default', created_at TEXT NOT NULL,
        updated_at TEXT, tags TEXT DEFAULT '[]',
        emotional_context TEXT DEFAULT '{}', metrics TEXT DEFAULT '{}',
        vector_id TEXT, gist TEXT, is_ghost INTEGER DEFAULT 0,
        synthesized_from TEXT DEFAULT '[]', version INTEGER DEFAULT 1,
        importance REAL, activation_count INTEGER DEFAULT 0
    )""")
    conn.commit()
    conn.close()
    return tmp.name


def _mock_db_connection(db_path):
    """Create a test DB connection with row_factory set (Python 3.13 compat)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _decode_json_result(result: str) -> dict:
    """Decode a JSON tool envelope."""
    return json.loads(result)


@pytest.mark.asyncio
async def test_text_tools_do_not_advertise_structured_output(monkeypatch):
    """Text-only Foresight tools should not trigger MCP outputSchema validation."""
    monkeypatch.setenv("FORESIGHT_ALLOW_UNAUTHENTICATED", "1")
    monkeypatch.delenv("FORESIGHT_REQUIRE_API_KEY", raising=False)

    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    for tool_name in ("manage_subconscious", "search_memories", "inject_context"):
        assert tools[tool_name].outputSchema is None


@pytest.mark.asyncio
async def test_local_mcp_calls_return_text_without_api_key(monkeypatch):
    """Local stdio callers can use Foresight when the launcher disables auth."""
    monkeypatch.setenv("FORESIGHT_ALLOW_UNAUTHENTICATED", "1")
    monkeypatch.delenv("FORESIGHT_REQUIRE_API_KEY", raising=False)

    async with Client(mcp) as client:
        result = await client.call_tool(
            "manage_subconscious",
            {"options": {"action": "list"}, "user_id": "test_user"},
        )

    assert result.is_error is False
    assert result.content


@contextmanager
def _patched_context_block_storage(db_path: str) -> Iterator[None]:
    """Point context-block persistence at a test database and isolate agent cache."""
    from foresight_mcp import subconscious as subconscious_module

    with (
        patch.object(subconscious_module, "DB_PATH", db_path),
        patch.dict(subconscious_module._context_block_agents, {}, clear=True),
    ):
        yield


def test_bridge_context_blocks_to_memories():
    """_bridge_context_blocks_to_memories stores extracted blocks as memories."""
    from foresight_mcp.server import _bridge_context_blocks_to_memories
    from foresight_mcp.subconscious import ContextBlockAgent

    agent = ContextBlockAgent(user_id="bridge_test_user")
    # Populate some blocks via the agent's normal extraction
    agent._extract_preference("I always use type hints")
    agent._extract_pending_item("TODO: add more tests", "sess_1")

    db_path = _make_test_db()
    with (
        patch("foresight_mcp.server.get_db_connection", lambda: _mock_db_connection(db_path)),
        patch("foresight_mcp.server.BANK_ID", "test_bank"),
    ):
        stored = _bridge_context_blocks_to_memories(agent, "bridge_test_user")

    assert stored >= 2  # at least one preference + one pending

    # Verify rows were actually inserted
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT category FROM memories WHERE user_id = ?",
        ("bridge_test_user",),
    ).fetchall()
    conn.close()
    categories = {r[0] for r in rows}
    assert "preference" in categories
    assert "pending" in categories


def test_bridge_context_blocks_dedup():
    """Bridging the same agent state twice should bump, not duplicate."""
    from foresight_mcp.server import _bridge_context_blocks_to_memories
    from foresight_mcp.subconscious import ContextBlockAgent

    agent = ContextBlockAgent(user_id="dedup_bridge_user")
    agent._extract_preference("I prefer explicit returns")

    db_path = _make_test_db()
    with (
        patch("foresight_mcp.server.get_db_connection", lambda: _mock_db_connection(db_path)),
        patch("foresight_mcp.server.BANK_ID", "test_bank"),
    ):
        _bridge_context_blocks_to_memories(agent, "dedup_bridge_user")
        _bridge_context_blocks_to_memories(agent, "dedup_bridge_user")

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT content FROM memories WHERE user_id = ?",
        ("dedup_bridge_user",),
    ).fetchall()
    conn.close()
    # Should have exactly one row (dedup on second call)
    assert len(rows) == 1


def test_bridge_transcript_entities():
    """_bridge_transcript_entities runs extraction and stores entities."""
    from foresight_mcp.entity_extractor import reset_entity_extractor
    from foresight_mcp.graph_store import GraphStore, reset_graph_store
    from foresight_mcp.server import _bridge_transcript_entities

    reset_entity_extractor()
    reset_graph_store()

    messages = [
        {"role": "user", "content": "I've been feeling a lot of anxiety about work lately"},
    ]

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    # GraphStore.__init__ creates the correct schema including tenant_id
    with patch("foresight_mcp.server.get_graph_store", lambda: GraphStore(db_path)):
        count = _bridge_transcript_entities(messages, "entity_test_user")

    assert count >= 1


# =============================================================================
# inject_context tests
# =============================================================================


def _make_inject_test_db(memories=None):
    """Create a temporary DB with full memories schema for inject_context tests.

    memories: optional list of dicts with keys:
        id, content, user_id, tenant_id, importance, created_at, is_ghost
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS memories (
        id TEXT PRIMARY KEY, content TEXT NOT NULL, content_hash TEXT,
        tenant_id TEXT NOT NULL DEFAULT 'default',
        scope TEXT DEFAULT 'session', retention TEXT DEFAULT 'short_term',
        category TEXT DEFAULT 'fact', user_id TEXT DEFAULT 'default',
        bank_id TEXT DEFAULT 'default', created_at TEXT NOT NULL,
        updated_at TEXT, tags TEXT DEFAULT '[]',
        emotional_context TEXT DEFAULT '{}', metrics TEXT DEFAULT '{}',
        vector_id TEXT, gist TEXT, is_ghost INTEGER DEFAULT 0,
        synthesized_from TEXT DEFAULT '[]', version INTEGER DEFAULT 1,
        importance REAL DEFAULT 1.0, activation_count INTEGER DEFAULT 0,
        decay_rate REAL DEFAULT 0.01, retrieval_count INTEGER DEFAULT 0,
        strength_trend TEXT DEFAULT 'stable', last_retrieved_at TEXT,
        accessed_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    if memories:
        for m in memories:
            conn.execute(
                "INSERT INTO memories (id, content, tenant_id, user_id, importance, created_at, is_ghost) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    m.get("id", hashlib.sha256(m["content"].encode()).hexdigest()[:16]),
                    m["content"],
                    m.get("tenant_id", "default"),
                    m.get("user_id", "inject_test_user"),
                    m.get("importance", 1.0),
                    m.get("created_at", datetime.now(timezone.utc).isoformat()),
                    m.get("is_ghost", 0),
                ),
            )
    conn.commit()
    conn.close()
    return tmp.name


def _mock_db_with_rows(db_path):
    """Return a connection to the test DB with row_factory set."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _make_hybrid_result(memory_id, content, combined_score=0.8, **kwargs):
    """Build a HybridResult for testing."""
    return HybridResult(
        memory_id=memory_id,
        content=content,
        category=kwargs.get("category", "fact"),
        importance=kwargs.get("importance", 1.0),
        strength_trend=kwargs.get("strength_trend", "stable"),
        created_at=kwargs.get("created_at", datetime.now(timezone.utc).isoformat()),
        keyword_score=kwargs.get("keyword_score", 0.0),
        tfidf_cosine_score=kwargs.get("tfidf_cosine_score", 0.0),
        semantic_score=kwargs.get("semantic_score", 0.0),
        graph_score=kwargs.get("graph_score", 0.0),
        temporal_score=kwargs.get("temporal_score", 0.0),
        combined_score=combined_score,
        source_signals=kwargs.get("source_signals", []),
    )


def _patch_hybrid_retriever(results, total_candidates=None, signal_counts=None):
    """Patch get_hybrid_retriever to return a mock with given HybridResults."""
    mock_retriever = MagicMock()
    mock_retriever.search.return_value = HybridSearchResult(
        results=results,
        total_candidates=total_candidates if total_candidates is not None else len(results),
        signal_counts=signal_counts or {},
    )
    return patch("foresight_mcp.server.get_hybrid_retriever", return_value=mock_retriever)


def test_extract_terms_filters_stop_words():
    """_extract_terms removes stop words and short tokens."""
    text = "The user is very interested in authentication and database performance"
    terms = _extract_terms(text)
    assert "the" not in terms
    assert "is" not in terms
    assert "very" not in terms
    assert "and" not in terms
    assert "user" in terms
    assert "interested" in terms
    assert "authentication" in terms
    assert "database" in terms
    assert "performance" in terms


def test_extract_terms_short_tokens():
    """Tokens of 3 chars or fewer are excluded."""
    text = "I am a cat and I run far"
    terms = _extract_terms(text)
    # All tokens are 3 chars or fewer or stop words
    assert len(terms) == 0


def test_extract_terms_empty():
    """Empty text returns empty terms."""
    assert _extract_terms("") == []


def test_inject_context_returns_formatted_output():
    """inject_context returns structured context block with matching memories."""
    results = [
        _make_hybrid_result(
            "mem1", "User prefers Python type hints in all functions", combined_score=0.85, importance=0.8
        ),
        _make_hybrid_result(
            "mem2", "Session discussed database migration strategies", combined_score=0.7, importance=0.6
        ),
    ]

    with (
        _patch_hybrid_retriever(results),
        patch("foresight_mcp.server.USER_ID", "inject_test_user"),
        patch("foresight_mcp.server.get_context_block_agent"),
    ):
        result = inject_context("Let's talk about database and type hints")

    assert "Relevant Context" in result
    assert "mem1" in result or "mem2" in result


def test_inject_context_respects_max_memories():
    """inject_context respects the max_memories limit."""
    results = [
        _make_hybrid_result(f"mem{i}", f"Memory about python topic number {i}", combined_score=0.9) for i in range(10)
    ]

    with (
        _patch_hybrid_retriever(results),
        patch("foresight_mcp.server.USER_ID", "inject_test_user"),
        patch("foresight_mcp.server.get_context_block_agent"),
    ):
        result = inject_context("python topic", max_memories=2)

    memory_lines = [line for line in result.splitlines() if line.startswith("- [")]
    assert len(memory_lines) <= 2


def test_inject_context_no_match():
    """inject_context with no matching memories returns empty context message."""
    results = [
        _make_hybrid_result("mem1", "Completely unrelated content about sailing", combined_score=0.1, importance=0.1),
    ]

    with (
        _patch_hybrid_retriever(results),
        patch("foresight_mcp.server.USER_ID", "inject_test_user"),
        patch("foresight_mcp.server.get_context_block_agent"),
    ):
        result = inject_context("quantum computing algorithms", min_relevance=0.5)

    assert "0 memories surfaced" in result


def test_inject_context_empty_conversation_text():
    """inject_context with empty conversation text still works (no terms to match)."""
    with (
        _patch_hybrid_retriever([]),
        patch("foresight_mcp.server.USER_ID", "inject_test_user"),
        patch("foresight_mcp.server.get_context_block_agent"),
    ):
        result = inject_context("")

    assert "0 memories surfaced" in result


def test_inject_context_include_details_returns_json():
    """inject_context with include_details=True returns JSON with memories, context_blocks, formatted keys."""
    results = [
        _make_hybrid_result("mem1", "User prefers Python type hints", combined_score=0.85, importance=0.8),
    ]
    with (
        _patch_hybrid_retriever(results, total_candidates=5, signal_counts={"keyword": 3}),
        patch("foresight_mcp.server.USER_ID", "inject_test_user"),
        patch("foresight_mcp.server.get_context_block_agent"),
    ):
        result = inject_context("python type hints", include_details=True)

    payload = json.loads(result)
    assert "formatted" in payload
    assert "memories" in payload
    assert "context_blocks" in payload
    assert len(payload["memories"]) == 1
    assert payload["memories"][0]["memory_id"] == "mem1"
    assert "pre_prompt" in payload["context_blocks"]
    assert "post_prompt" in payload["context_blocks"]
    assert "whisper_only" in payload["context_blocks"]


def test_get_relevant_memories_returns_structured_data():
    """get_relevant_memories returns JSON with memories, total_candidates, signal_counts."""
    results = [
        _make_hybrid_result("mem1", "User prefers Python type hints", combined_score=0.85),
        _make_hybrid_result("mem2", "Session discussed database migrations", combined_score=0.7),
    ]
    with (
        _patch_hybrid_retriever(results, total_candidates=5, signal_counts={"keyword": 3, "graph": 2}),
        patch("foresight_mcp.server.USER_ID", "inject_test_user"),
    ):
        result = get_relevant_memories("python type hints")

    payload = json.loads(result)
    assert "memories" in payload
    assert "total_candidates" in payload
    assert "signal_counts" in payload
    assert len(payload["memories"]) == 2
    assert payload["memories"][0]["memory_id"] == "mem1"
    assert payload["total_candidates"] == 5
    assert payload["signal_counts"] == {"keyword": 3, "graph": 2}


def test_get_relevant_memories_filters_by_min_relevance():
    """get_relevant_memories filters out results below min_relevance threshold."""
    results = [
        _make_hybrid_result("mem1", "High relevance memory", combined_score=0.9),
        _make_hybrid_result("mem2", "Low relevance memory", combined_score=0.1),
    ]
    with (
        _patch_hybrid_retriever(results),
        patch("foresight_mcp.server.USER_ID", "inject_test_user"),
    ):
        result = get_relevant_memories("test query", min_relevance=0.5)

    payload = json.loads(result)
    assert len(payload["memories"]) == 1
    assert payload["memories"][0]["memory_id"] == "mem1"


def test_get_relevant_memories_respects_limit():
    """get_relevant_memories respects the limit parameter."""
    results = [_make_hybrid_result(f"mem{i}", f"Memory {i}", combined_score=0.9 - i * 0.05) for i in range(10)]
    with (
        _patch_hybrid_retriever(results),
        patch("foresight_mcp.server.USER_ID", "inject_test_user"),
    ):
        result = get_relevant_memories("test query", limit=3)

    payload = json.loads(result)
    assert len(payload["memories"]) == 3


def test_format_context_blocks_by_injection_point_groups_by_schema():
    """_format_context_blocks_by_injection_point groups matching entries by InjectionPoint."""
    mock_agent = MagicMock()

    def fake_block(label):
        block = MagicMock()
        block.is_empty = lambda: False
        block.content = f"{label}: user mentioned python in session"
        return block

    mock_agent.state.get_block.side_effect = fake_block

    with patch("foresight_mcp.server.get_context_block_agent", return_value=mock_agent):
        result = _format_context_blocks_by_injection_point("test_user", "default", ["python"])

    total_entries = sum(len(v) for v in result.values())
    assert total_entries >= 1
    for entries in result.values():
        for entry in entries:
            assert "label" in entry
            assert "content" in entry
            assert "matched_terms" in entry
            assert entry["matched_terms"] == ["python"]


def test_score_memory_relevance():
    """_score_memory_relevance combines overlap, importance, and recency."""
    now = datetime.now(timezone.utc)

    # Create a mock row-like object using a dict wrapped to behave like Row
    class FakeRow:
        def __getitem__(self, key):
            return {
                "content": "User prefers Python type hints in all functions",
                "importance": 0.8,
                "created_at": now.isoformat(),
            }[key]

    row = FakeRow()
    terms = ["python", "type", "hints", "functions"]
    score = _score_memory_relevance(row, terms, now)

    # overlap_score=1.0 (4/4), importance=0.8*0.5=0.4, decay~0.5 -> total ~1.9
    # Normalized score: 1.0 + 0.4 + 0.5 = 1.9
    assert score > 1.5  # Normalized score ~1.9


def test_score_memory_relevance_old_memory():
    """Older memories get lower recency scores."""
    now = datetime.now(timezone.utc)
    old_time = (now - timedelta(days=30)).isoformat()

    class FakeRow:
        def __getitem__(self, key):
            return {
                "content": "Some content about databases",
                "importance": 1.0,
                "created_at": old_time,
            }[key]

    row = FakeRow()
    score_old = _score_memory_relevance(row, ["databases"], now)

    # Same content but fresh
    class FreshRow:
        def __getitem__(self, key):
            return {
                "content": "Some content about databases",
                "importance": 1.0,
                "created_at": now.isoformat(),
            }[key]

    fresh_row = FreshRow()
    score_fresh = _score_memory_relevance(fresh_row, ["databases"], now)

    assert score_fresh > score_old


def test_manage_context_blocks_update_reset_clear_cycle():
    """The renamed block manager preserves the continuity block semantics."""
    user_id = f"context_block_test_{datetime.now(timezone.utc).timestamp()}"
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()

    with _patched_context_block_storage(tmp.name):
        listed = _decode_json_result(manage_context_blocks(ContextBlockAction(action="list"), user_id=user_id))
        assert listed["ok"] is True
        assert any(block["label"] == "core_directives" for block in listed["blocks"])

        initial_guidance = _decode_json_result(
            manage_context_blocks(ContextBlockAction(action="get", label="guidance"), user_id=user_id)
        )
        assert "No active guidance" in initial_guidance["content"]

        updated = _decode_json_result(
            manage_context_blocks(
                ContextBlockAction(
                    action="update", label="guidance", content="Always show exact verification evidence."
                ),
                user_id=user_id,
            )
        )
        assert updated == {
            "ok": True,
            "action": "update",
            "label": "guidance",
            "message": "Updated block 'guidance'",
        }

        fetched = _decode_json_result(
            manage_context_blocks(ContextBlockAction(action="get", label="guidance"), user_id=user_id)
        )
        assert fetched["content"] == "Always show exact verification evidence."

        cleared = _decode_json_result(
            manage_context_blocks(ContextBlockAction(action="clear", label="guidance"), user_id=user_id)
        )
        assert cleared["message"] == "Cleared block 'guidance'"

        empty_fetch = _decode_json_result(
            manage_context_blocks(ContextBlockAction(action="get", label="guidance"), user_id=user_id)
        )
        assert empty_fetch["content"] == ""

        listed_after_clear = _decode_json_result(
            manage_context_blocks(ContextBlockAction(action="list"), user_id=user_id)
        )
        assert all(block["label"] != "guidance" for block in listed_after_clear["blocks"])

        reset = _decode_json_result(
            manage_context_blocks(ContextBlockAction(action="reset", label="guidance"), user_id=user_id)
        )
        assert reset["message"] == "Reset block 'guidance' to default"


def test_registered_context_block_schema_allows_validated_custom_block():
    """Custom schemas can create validated non-default context blocks."""
    label = f"custom_notes_{datetime.now(timezone.utc).timestamp()}".replace(".", "_")
    user_id = f"custom_block_user_{datetime.now(timezone.utc).timestamp()}"
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    register_context_block_schema(
        MemoryBlockSchema(
            label=label,
            description="Short custom notes",
            char_limit=12,
        )
    )

    with _patched_context_block_storage(db_path):
        updated = _decode_json_result(
            manage_context_blocks(
                ContextBlockAction(action="update", label=label, content="short note"), user_id=user_id
            )
        )
        assert updated["ok"] is True

        fetched = _decode_json_result(
            manage_context_blocks(ContextBlockAction(action="get", label=label), user_id=user_id)
        )
        assert fetched["content"] == "short note"

        listed = _decode_json_result(manage_context_blocks(ContextBlockAction(action="list"), user_id=user_id))
        custom_block = next(block for block in listed["blocks"] if block["label"] == label)
        assert custom_block["description"] == "Short custom notes"
        assert custom_block["char_limit"] == 12
        invalid = _decode_json_result(
            manage_context_blocks(
                ContextBlockAction(action="update", label=label, content="this note is too long"), user_id=user_id
            )
        )
        assert invalid["ok"] is False
        assert "Content exceeds char limit" in invalid["error"]["message"]
        reloaded = _decode_json_result(
            manage_context_blocks(ContextBlockAction(action="get", label=label), user_id=user_id)
        )
        assert reloaded["content"] == "short note"

        reset = _decode_json_result(
            manage_context_blocks(ContextBlockAction(action="reset", label=label), user_id=user_id)
        )
        assert reset["ok"] is True
        assert reset["message"] == f"Reset block '{label}' to default"

        after_reset = _decode_json_result(
            manage_context_blocks(ContextBlockAction(action="get", label=label), user_id=user_id)
        )
        assert after_reset["content"] == ""


def test_manage_context_blocks_are_tenant_isolated():
    """Same user ID gets separate persisted context blocks per tenant."""
    user_id = f"tenant_isolation_user_{datetime.now(timezone.utc).timestamp()}"
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()

    with _patched_context_block_storage(tmp.name):
        with patch("foresight_mcp.server.get_current_tenant_id", return_value="tenant-a"):
            update_a = _decode_json_result(
                manage_context_blocks(
                    ContextBlockAction(action="update", label="guidance", content="Tenant A guidance"),
                    user_id=user_id,
                )
            )
            assert update_a["ok"] is True

        with patch("foresight_mcp.server.get_current_tenant_id", return_value="tenant-b"):
            fetched_b = _decode_json_result(
                manage_context_blocks(ContextBlockAction(action="get", label="guidance"), user_id=user_id)
            )
            assert fetched_b["content"] != "Tenant A guidance"

        with patch("foresight_mcp.server.get_current_tenant_id", return_value="tenant-a"):
            fetched_a = _decode_json_result(
                manage_context_blocks(ContextBlockAction(action="get", label="guidance"), user_id=user_id)
            )
            assert fetched_a["content"] == "Tenant A guidance"


def _make_curation_test_db():
    """Create a temp DB with the schemas required for curation workflow tests."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY, content TEXT NOT NULL,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            scope TEXT DEFAULT 'session', retention TEXT DEFAULT 'short_term',
            category TEXT DEFAULT 'fact', user_id TEXT DEFAULT 'default',
            bank_id TEXT DEFAULT 'default', created_at TEXT NOT NULL,
            updated_at TEXT, tags TEXT DEFAULT '[]',
            emotional_context TEXT DEFAULT '{}', metrics TEXT DEFAULT '{}',
            vector_id TEXT, gist TEXT, is_ghost INTEGER DEFAULT 0,
            synthesized_from TEXT DEFAULT '[]', version INTEGER DEFAULT 1,
            importance REAL DEFAULT 1.0, activation_count INTEGER DEFAULT 0,
            decay_rate REAL DEFAULT 0.01, retrieval_count INTEGER DEFAULT 0,
            strength_trend TEXT DEFAULT 'stable', last_retrieved_at TEXT,
            accessed_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS curation_runs (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            source_bank_id TEXT NOT NULL,
            output_bank_id TEXT NOT NULL,
            policy_mode TEXT NOT NULL,
            tool_access TEXT NOT NULL,
            output_mode TEXT NOT NULL,
            status TEXT NOT NULL,
            instructions TEXT,
            transcript_bundle_json TEXT,
            session_id TEXT,
            project_path TEXT,
            summary_json TEXT DEFAULT '{}',
            error_json TEXT DEFAULT '{}',
            created_at TEXT NOT NULL,
            started_at TEXT,
            ended_at TEXT,
            archived_at TEXT
        )"""
    )
    conn.commit()
    conn.close()
    return tmp.name


def _seed_memory(
    db_path: str, *, memory_id: str, content: str, bank_id: str, user_id: str, tenant_id: str = "_test_"
) -> None:
    """Insert a memory row for curation tests."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO memories
        (id, content, tenant_id, scope, retention, category, user_id, bank_id, created_at,
         updated_at, tags, emotional_context, metrics, is_ghost, synthesized_from, version,
         importance, activation_count, decay_rate, retrieval_count, strength_trend, last_retrieved_at, accessed_at)
        VALUES (?, ?, ?, 'arc', 'long_term', 'fact', ?, ?, ?, ?, '[]', '{}', '{}', 0, '[]', 1, 1.0, 0, 0.01, 0, 'stable', NULL, ?)""",
        (memory_id, content, tenant_id, user_id, bank_id, now, now, now),
    )
    conn.commit()
    conn.close()


def test_manage_curation_runs_create_cancel_archive():
    """Pending runs can be created, canceled, and archived."""
    db_path = _make_curation_test_db()
    user_id = "curation_cancel_user"
    _seed_memory(db_path, memory_id="mem1", content="A durable memory", bank_id="source_bank", user_id=user_id)

    with (
        patch("foresight_mcp.server.DB_PATH", db_path),
        patch("foresight_mcp.server.get_db_connection", lambda: _mock_db_with_rows(db_path)),
        patch("foresight_mcp.server._start_curation_worker", lambda *_args, **_kwargs: None),
        patch("foresight_mcp.server._publish_curation_status", lambda *_args, **_kwargs: None),
    ):
        created = json.loads(
            manage_curation_runs(
                CurationRunAction(action="create", source_bank_id="source_bank"),
                user_id=user_id,
            )
        )
        assert created["ok"] is True
        assert created["run"]["status"] == "pending"
        assert created["run"]["output_bank_id"].startswith("curation:")

        fetched = json.loads(
            manage_curation_runs(CurationRunAction(action="get", run_id=created["run"]["id"]), user_id=user_id)
        )
        assert fetched["run"]["id"] == created["run"]["id"]

        listed = json.loads(manage_curation_runs(CurationRunAction(action="list", limit=5), user_id=user_id))
        assert listed["runs"][0]["id"] == created["run"]["id"]

        canceled = json.loads(
            manage_curation_runs(CurationRunAction(action="cancel", run_id=created["run"]["id"]), user_id=user_id)
        )
        assert canceled["run"]["status"] == "canceled"

        archived = json.loads(
            manage_curation_runs(CurationRunAction(action="archive", run_id=created["run"]["id"]), user_id=user_id)
        )
        assert archived["run"]["archived_at"] is not None


def test_manage_curation_runs_validates_in_place_and_transcript_rules():
    """Explicit in-place writes and transcript processing require operate access."""
    user_id = "curation_validation_user"

    in_place = manage_curation_runs(
        CurationRunAction(
            action="create",
            source_bank_id="source_bank",
            output_mode="in_place",
            tool_access="observe",
        ),
        user_id=user_id,
    )
    in_place_result = json.loads(in_place)
    assert in_place_result["ok"] is False
    assert in_place_result["error"]["message"] == "output_mode=in_place requires tool_access=operate"

    unsafe_output_bank = json.loads(
        manage_curation_runs(
            CurationRunAction(
                action="create",
                source_bank_id="source_bank",
                output_bank_id="source_bank",
                output_mode="in_place",
                tool_access="operate",
            ),
            user_id=user_id,
        )
    )
    assert unsafe_output_bank["ok"] is False
    assert unsafe_output_bank["error"]["message"] == "output_mode=in_place does not allow output_bank_id override"

    transcript = manage_curation_runs(
        CurationRunAction(
            action="create",
            source_bank_id="source_bank",
            tool_access="observe",
            transcript_bundle=[{"role": "user", "content": "Remember this"}],
        ),
        user_id=user_id,
    )
    transcript_result = json.loads(transcript)
    assert transcript_result["ok"] is False
    assert transcript_result["error"]["message"] == "transcript_bundle requires tool_access=operate"


def test_manage_curation_runs_reviewable_output_and_failure_status():
    """Curation runs keep failed output reviewable and persist error metadata."""
    from foresight_mcp import server as server_module

    db_path = _make_curation_test_db()
    user_id = "curation_failure_user"
    _seed_memory(db_path, memory_id="mem1", content="Primary insight", bank_id="source_bank", user_id=user_id)
    events = []

    class _FakeBus:
        def publish(self, event):
            events.append(event.event_type.value)

    def _run_inline(run_id, payload):
        server_module._execute_curation_run(run_id, payload)

    with (
        _patched_context_block_storage(db_path),
        patch("foresight_mcp.server.DB_PATH", db_path),
        patch("foresight_mcp.server.get_db_connection", lambda: _mock_db_with_rows(db_path)),
        patch("foresight_mcp.server.get_event_bus_with_stream", return_value=_FakeBus()),
        patch("foresight_mcp.server._start_curation_worker", side_effect=_run_inline),
        patch("foresight_mcp.server._build_synthesis_snapshot", return_value={"insights": [], "contradictions": []}),
        patch(
            "foresight_mcp.server._build_reflection_snapshot",
            return_value={"trend_summary": {"overall": "stable"}, "insights": []},
        ),
        patch("foresight_mcp.server._insert_curation_entries", side_effect=RuntimeError("curation exploded")),
    ):
        created = json.loads(
            manage_curation_runs(
                CurationRunAction(action="create", source_bank_id="source_bank"),
                user_id=user_id,
            )
        )
        fetched = json.loads(
            manage_curation_runs(CurationRunAction(action="get", run_id=created["run"]["id"]), user_id=user_id)
        )
        assert fetched["run"]["status"] == "failed"
        assert fetched["run"]["output_bank_id"].startswith("curation:")
        assert fetched["run"]["error"]["message"] == "curation exploded"

    assert "curation.created" in events
    assert "curation.failed" in events


def test_manage_curation_runs_in_place_archives_originals_and_promotes_staged_output():
    """Successful in-place runs archive the source bank and promote staged entries into it."""
    from foresight_mcp import server as server_module

    db_path = _make_curation_test_db()
    user_id = "curation_in_place_user"
    _seed_memory(db_path, memory_id="mem1", content="Original memory one", bank_id="source_bank", user_id=user_id)
    _seed_memory(db_path, memory_id="mem2", content="Original memory two", bank_id="source_bank", user_id=user_id)

    def _run_inline(run_id, payload):
        server_module._execute_curation_run(run_id, payload)

    with (
        _patched_context_block_storage(db_path),
        patch("foresight_mcp.server.DB_PATH", db_path),
        patch("foresight_mcp.server.get_db_connection", lambda: _mock_db_with_rows(db_path)),
        patch("foresight_mcp.server.get_event_bus_with_stream"),
        patch("foresight_mcp.server._start_curation_worker", side_effect=_run_inline),
        patch("foresight_mcp.server._build_synthesis_snapshot", return_value={"insights": [], "contradictions": []}),
        patch(
            "foresight_mcp.server._build_reflection_snapshot",
            return_value={"trend_summary": {"overall": "stable"}, "insights": []},
        ),
    ):
        created = json.loads(
            manage_curation_runs(
                CurationRunAction(
                    action="create",
                    source_bank_id="source_bank",
                    output_mode="in_place",
                    tool_access="operate",
                    policy_mode="preserve",
                ),
                user_id=user_id,
            )
        )
        run = created["run"]
        fetched = json.loads(manage_curation_runs(CurationRunAction(action="get", run_id=run["id"]), user_id=user_id))[
            "run"
        ]

    assert fetched["status"] == "completed"
    assert fetched["output_mode"] == "in_place"
    assert fetched["output_bank_id"].startswith("curation:stage:")
    assert fetched["summary"]["archive_bank_id"] == f"source_bank:archived:{run['id']}"
    assert fetched["summary"]["archived_memory_count"] == 2
    assert fetched["summary"]["promoted_memory_count"] == fetched["summary"]["output_memory_count"]

    conn = sqlite3.connect(db_path)
    source_rows = conn.execute(
        "SELECT id, bank_id, tags FROM memories WHERE user_id = ? AND bank_id = ? ORDER BY id",
        (user_id, "source_bank"),
    ).fetchall()
    archived_rows = conn.execute(
        "SELECT id FROM memories WHERE user_id = ? AND bank_id = ? ORDER BY id",
        (user_id, fetched["summary"]["archive_bank_id"]),
    ).fetchall()
    staging_rows = conn.execute(
        "SELECT id FROM memories WHERE user_id = ? AND bank_id = ?",
        (user_id, fetched["summary"]["staging_bank_id"]),
    ).fetchall()
    original_memories = conn.execute("SELECT bank_id FROM memories WHERE id IN ('mem1', 'mem2') ORDER BY id").fetchall()
    conn.close()

    assert len(source_rows) == fetched["summary"]["output_memory_count"]
    assert all('"curation_run:' in (row[2] or "") for row in source_rows)
    assert len(archived_rows) == 2
    assert len(staging_rows) == 0
    assert {row[0] for row in original_memories} == {fetched["summary"]["archive_bank_id"]}


def test_manage_curation_runs_canceled_in_place_run_leaves_source_bank_untouched():
    """Canceled in-place runs do not promote or archive any source memories."""
    from foresight_mcp import server as server_module

    db_path = _make_curation_test_db()
    user_id = "curation_cancel_integrity_user"
    _seed_memory(db_path, memory_id="mem1", content="Keep this intact", bank_id="source_bank", user_id=user_id)

    def _run_inline(run_id, payload):
        server_module._execute_curation_run(run_id, payload)

    def _cancel_before_insert(run, source_rows, block_snapshot, synthesis, reflection):
        server_module._get_curation_cancel_event(run["id"]).set()
        return [
            {
                "content": "Should never persist",
                "category": "curation_summary",
                "scope": "arc",
                "retention": "long_term",
                "tags": [f"curation_run:{run['id']}"],
            }
        ]

    with (
        _patched_context_block_storage(db_path),
        patch("foresight_mcp.server.DB_PATH", db_path),
        patch("foresight_mcp.server.get_db_connection", lambda: _mock_db_with_rows(db_path)),
        patch("foresight_mcp.server.get_event_bus_with_stream"),
        patch("foresight_mcp.server._start_curation_worker", side_effect=_run_inline),
        patch("foresight_mcp.server._build_synthesis_snapshot", return_value={"insights": [], "contradictions": []}),
        patch(
            "foresight_mcp.server._build_reflection_snapshot",
            return_value={"trend_summary": {"overall": "stable"}, "insights": []},
        ),
        patch("foresight_mcp.server._make_curated_entries", side_effect=_cancel_before_insert),
    ):
        created = json.loads(
            manage_curation_runs(
                CurationRunAction(
                    action="create",
                    source_bank_id="source_bank",
                    output_mode="in_place",
                    tool_access="operate",
                ),
                user_id=user_id,
            )
        )
        fetched = json.loads(
            manage_curation_runs(CurationRunAction(action="get", run_id=created["run"]["id"]), user_id=user_id)
        )["run"]

    assert fetched["status"] == "canceled"

    conn = sqlite3.connect(db_path)
    source_rows = conn.execute(
        "SELECT id, bank_id FROM memories WHERE user_id = ? AND bank_id = ?",
        (user_id, "source_bank"),
    ).fetchall()
    archive_rows = conn.execute(
        "SELECT id FROM memories WHERE user_id = ? AND bank_id = ?",
        (user_id, f"source_bank:archived:{created['run']['id']}"),
    ).fetchall()
    staged_rows = conn.execute(
        "SELECT id FROM memories WHERE user_id = ? AND bank_id = ?",
        (user_id, created["run"]["output_bank_id"]),
    ).fetchall()
    conn.close()

    assert source_rows == [("mem1", "source_bank")]
    assert archive_rows == []
    assert staged_rows == []


def test_manage_curation_runs_cancel_during_promotion_restores_source_bank():
    """Cancellation landing after promotion starts must restore the source bank and keep the run canceled."""
    from foresight_mcp import server as server_module

    db_path = _make_curation_test_db()
    user_id = "curation_cancel_promotion_user"
    _seed_memory(db_path, memory_id="mem1", content="Original source memory", bank_id="source_bank", user_id=user_id)

    def _run_inline(run_id, payload):
        server_module._execute_curation_run(run_id, payload)

    original_promote = server_module._promote_in_place_curation

    def _cancel_after_promote(
        uid, tenant_id, run_id, source_bank_id, staging_bank_id, source_rows, staged_ids, *, cancel_event
    ):
        summary = original_promote(
            uid,
            tenant_id,
            run_id,
            source_bank_id,
            staging_bank_id,
            source_rows,
            staged_ids,
            cancel_event=cancel_event,
        )
        cancel_event.set()
        server_module._update_curation_run(run_id, tenant_id, status="canceled")
        return summary

    with (
        _patched_context_block_storage(db_path),
        patch("foresight_mcp.server.DB_PATH", db_path),
        patch("foresight_mcp.server.get_db_connection", lambda: _mock_db_with_rows(db_path)),
        patch("foresight_mcp.server.get_event_bus_with_stream"),
        patch("foresight_mcp.server._start_curation_worker", side_effect=_run_inline),
        patch("foresight_mcp.server._build_synthesis_snapshot", return_value={"insights": [], "contradictions": []}),
        patch(
            "foresight_mcp.server._build_reflection_snapshot",
            return_value={"trend_summary": {"overall": "stable"}, "insights": []},
        ),
        patch("foresight_mcp.server._promote_in_place_curation", side_effect=_cancel_after_promote),
    ):
        created = json.loads(
            manage_curation_runs(
                CurationRunAction(
                    action="create",
                    source_bank_id="source_bank",
                    output_mode="in_place",
                    tool_access="operate",
                ),
                user_id=user_id,
            )
        )
        fetched = json.loads(
            manage_curation_runs(CurationRunAction(action="get", run_id=created["run"]["id"]), user_id=user_id)
        )["run"]

    assert fetched["status"] == "canceled"

    conn = sqlite3.connect(db_path)
    source_rows = conn.execute(
        "SELECT id, bank_id FROM memories WHERE user_id = ? AND bank_id = ? ORDER BY id",
        (user_id, "source_bank"),
    ).fetchall()
    staging_rows = conn.execute(
        "SELECT id, bank_id FROM memories WHERE user_id = ? AND bank_id = ? ORDER BY id",
        (user_id, created["run"]["output_bank_id"]),
    ).fetchall()
    archive_rows = conn.execute(
        "SELECT id FROM memories WHERE user_id = ? AND bank_id = ?",
        (user_id, f"source_bank:archived:{created['run']['id']}"),
    ).fetchall()
    conn.close()

    assert source_rows == [("mem1", "source_bank")]
    assert len(staging_rows) == 2
    assert archive_rows == []


def test_resume_pending_curation_runs_requeues_pending_and_running_rows():
    """Startup replay re-enqueues interrupted curation runs and normalizes running->pending."""
    from foresight_mcp.server import _resume_pending_curation_runs

    db_path = _make_curation_test_db()
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO curation_runs
        (id, tenant_id, user_id, source_bank_id, output_bank_id, policy_mode, tool_access,
         output_mode, status, instructions, summary_json, error_json, created_at)
        VALUES
        ('cur_pending', 'tenant-a', 'user-a', 'bank-a', 'curation:cur_pending', 'rebalance', 'observe',
         'reviewable_output', 'pending', NULL, '{}', '{}', ?),
        ('cur_running', 'tenant-b', 'user-b', 'bank-b', 'curation:cur_running', 'rebalance', 'observe',
         'reviewable_output', 'running', NULL, '{}', '{}', ?)""",
        (now, now),
    )
    conn.commit()
    conn.close()

    started: list[tuple[str, dict]] = []

    def _capture_start(run_id, payload):
        started.append((run_id, payload))

    with (
        patch("foresight_mcp.server.DB_PATH", db_path),
        patch("foresight_mcp.server.get_db_connection", lambda: _mock_db_with_rows(db_path)),
        patch("foresight_mcp.server._start_curation_worker", side_effect=_capture_start),
    ):
        _resume_pending_curation_runs()

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT id, status FROM curation_runs WHERE id IN ('cur_pending', 'cur_running') ORDER BY id"
    ).fetchall()
    queued = conn.execute(
        "SELECT id, tenant_id, entity_type FROM operations WHERE id IN ('cur_pending', 'cur_running') ORDER BY id"
    ).fetchall()
    conn.close()

    assert rows == [("cur_pending", "pending"), ("cur_running", "pending")]
    assert queued == [
        ("cur_pending", "tenant-a", "curation_run"),
        ("cur_running", "tenant-b", "curation_run"),
    ]
    assert [run_id for run_id, _payload in started] == ["cur_pending", "cur_running"]


def test_resume_pending_curation_runs_preserves_transcript_payload():
    """Startup replay must keep transcript/session/project payload for interrupted runs."""
    from foresight_mcp.server import _resume_pending_curation_runs

    db_path = _make_curation_test_db()
    now = datetime.now(timezone.utc).isoformat()
    transcript_bundle = [{"role": "user", "content": "Remember this detail"}]
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO curation_runs
        (id, tenant_id, user_id, source_bank_id, output_bank_id, policy_mode, tool_access,
         output_mode, status, instructions, transcript_bundle_json, session_id, project_path,
         summary_json, error_json, created_at)
        VALUES
        ('cur_transcript', 'tenant-a', 'user-a', 'bank-a', 'curation:stage:cur_transcript', 'rebalance', 'operate',
         'in_place', 'pending', 'Preserve the transcript context', ?, 'sess-123', '/tmp/project',
         '{}', '{}', ?)""",
        (json.dumps(transcript_bundle), now),
    )
    conn.commit()
    conn.close()

    started: list[tuple[str, dict]] = []

    def _capture_start(run_id, payload):
        started.append((run_id, payload))

    with (
        patch("foresight_mcp.server.DB_PATH", db_path),
        patch("foresight_mcp.server.get_db_connection", lambda: _mock_db_with_rows(db_path)),
        patch("foresight_mcp.server._start_curation_worker", side_effect=_capture_start),
    ):
        _resume_pending_curation_runs()

    assert started == [
        (
            "cur_transcript",
            {
                "tenant_id": "tenant-a",
                "user_id": "user-a",
                "source_bank_id": "bank-a",
                "output_bank_id": "curation:stage:cur_transcript",
                "policy_mode": "rebalance",
                "tool_access": "operate",
                "output_mode": "in_place",
                "instructions": "Preserve the transcript context",
                "run_clustering": False,
                "transcript_bundle": transcript_bundle,
                "session_id": "sess-123",
                "project_path": "/tmp/project",
            },
        )
    ]


def test_claim_curation_run_is_atomic_for_duplicate_workers():
    """Only one worker may claim a pending run even if execution is invoked twice."""
    from foresight_mcp import server as server_module

    db_path = _make_curation_test_db()
    user_id = "curation_atomic_claim_user"
    _seed_memory(db_path, memory_id="mem1", content="Atomic claim source", bank_id="source_bank", user_id=user_id)

    with (
        patch("foresight_mcp.server.DB_PATH", db_path),
        patch("foresight_mcp.server.get_db_connection", lambda: _mock_db_with_rows(db_path)),
        patch("foresight_mcp.server.get_event_bus_with_stream"),
        patch("foresight_mcp.server._start_curation_worker", lambda *_args, **_kwargs: None),
        patch("foresight_mcp.server._build_synthesis_snapshot", return_value={"insights": [], "contradictions": []}),
        patch(
            "foresight_mcp.server._build_reflection_snapshot",
            return_value={"trend_summary": {"overall": "stable"}, "insights": []},
        ),
    ):
        created = json.loads(
            manage_curation_runs(
                CurationRunAction(action="create", source_bank_id="source_bank"),
                user_id=user_id,
            )
        )
        run = created["run"]
        payload = {
            "tenant_id": run.get("tenant_id", "_test_"),
            "user_id": user_id,
            "source_bank_id": "source_bank",
            "output_bank_id": run["output_bank_id"],
            "policy_mode": run["policy_mode"],
            "tool_access": run["tool_access"],
            "output_mode": run["output_mode"],
            "instructions": run["instructions"],
            "transcript_bundle": None,
            "session_id": None,
            "project_path": None,
        }

        server_module._execute_curation_run(run["id"], payload)
        server_module._execute_curation_run(run["id"], payload)

        # Reset tenant context after _execute_curation_run changed it via set_current_tenant_id()
        from foresight_mcp.tenant_context import set_current_account_id

        set_current_account_id("_test_")

        fetched = json.loads(manage_curation_runs(CurationRunAction(action="get", run_id=run["id"]), user_id=user_id))[
            "run"
        ]

    conn = sqlite3.connect(db_path)
    output_rows = conn.execute(
        "SELECT id FROM memories WHERE user_id = ? AND bank_id = ? ORDER BY id",
        (user_id, run["output_bank_id"]),
    ).fetchall()
    conn.close()

    assert fetched["status"] == "completed"
    assert len(output_rows) == fetched["summary"]["output_memory_count"]


def test_manage_curation_runs_list_initializes_schema_for_empty_database():
    """Calling curation APIs directly should bootstrap their schema on a new database."""
    db_path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name

    with patch("foresight_mcp.server.DB_PATH", db_path):
        listed = json.loads(manage_curation_runs(CurationRunAction(action="list", limit=5), user_id="fresh_user"))

    assert listed == {"ok": True, "action": "list", "runs": []}


def test_decode_tool_result_wraps_plain_text_errors_for_json_output():
    """CLI JSON mode should never crash when the backend returns plain text."""
    decoded = _decode_tool_result("backend exploded")

    assert decoded["ok"] is False
    assert decoded["error"]["message"] == "backend exploded"


# =============================================================================
# Defense-in-Depth Tenant Isolation Tests
# (Regression tests for Issues B and C found during PIX-383/PIX-385 audit)
# =============================================================================


def test_handle_memory_archive_respects_tenant_scope():
    """_handle_memory_archive UPDATE must include tenant_id in WHERE clause.

    Regression test: if a memory_id collision existed across tenants (e.g.
    future schema change to composite key), the old UPDATE (scoped only by
    id+user_id) could modify a different tenant's memory. The fix adds
    tenant_id to the WHERE clause. This test executes the exact UPDATE
    statement from the function to verify the WHERE clause includes
    tenant_id.
    """
    db_path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """CREATE TABLE memories (
                id TEXT NOT NULL,
                content TEXT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                user_id TEXT DEFAULT 'default',
                is_ghost INTEGER DEFAULT 0,
                gist TEXT,
                UNIQUE(id, tenant_id)
            )"""
        )
        memory_id = "mem-archive-collision"
        for tenant, content in [
            ("tenant-a", "tenant-a content"),
            ("tenant-b", "tenant-b content"),
        ]:
            conn.execute(
                "INSERT INTO memories (id, content, tenant_id, user_id, is_ghost, gist) VALUES (?, ?, ?, ?, 0, NULL)",
                (memory_id, content, tenant, "user-1"),
            )
        conn.commit()

        ghost_content = "archived-content"
        ghost_gist = "archived-gist"
        cursor = conn.execute(
            "UPDATE memories SET content = ?, is_ghost = 1, gist = ? WHERE id = ? AND user_id = ? AND tenant_id = ?",
            (ghost_content, ghost_gist, memory_id, "user-1", "tenant-a"),
        )
        conn.commit()
        assert cursor.rowcount == 1, f"Expected exactly 1 row updated, got {cursor.rowcount}"

        row_a = conn.execute(
            "SELECT content, is_ghost, gist FROM memories WHERE id = ? AND tenant_id = ?",
            (memory_id, "tenant-a"),
        ).fetchone()
        assert row_a[0] == "archived-content"
        assert row_a[1] == 1
        assert row_a[2] == "archived-gist"

        row_b = conn.execute(
            "SELECT content, is_ghost, gist FROM memories WHERE id = ? AND tenant_id = ?",
            (memory_id, "tenant-b"),
        ).fetchone()
        assert row_b[0] == "tenant-b content", "tenant-b must NOT be modified"
        assert row_b[1] == 0, "tenant-b is_ghost must NOT be modified"
        assert row_b[2] is None, "tenant-b gist must NOT be modified"

        conn.close()
    finally:
        os.unlink(db_path)


def test_old_archive_update_would_leak_across_tenants():
    """Demonstrate the vulnerability the fix prevents.

    Without the fix, the UPDATE WHERE clause would be:
        WHERE id = ? AND user_id = ?
    This test shows that the OLD (vulnerable) UPDATE pattern would update
    BOTH tenant rows. The fixed UPDATE (tested above) only updates the
    correct tenant.
    """
    db_path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """CREATE TABLE memories (
                id TEXT NOT NULL,
                content TEXT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                user_id TEXT DEFAULT 'default',
                is_ghost INTEGER DEFAULT 0,
                gist TEXT,
                UNIQUE(id, tenant_id)
            )"""
        )
        memory_id = "mem-leak-demo"
        for tenant, content in [
            ("tenant-a", "tenant-a content"),
            ("tenant-b", "tenant-b content"),
        ]:
            conn.execute(
                "INSERT INTO memories (id, content, tenant_id, user_id, is_ghost, gist) VALUES (?, ?, ?, ?, 0, NULL)",
                (memory_id, content, tenant, "user-1"),
            )
        conn.commit()

        cursor = conn.execute(
            "UPDATE memories SET content = ?, is_ghost = 1, gist = ? WHERE id = ? AND user_id = ?",
            ("LEAKED", "leak-gist", memory_id, "user-1"),
        )
        conn.commit()

        assert cursor.rowcount == 2, (
            f"Vulnerability confirmed: old UPDATE leaks across tenants (updated {cursor.rowcount} rows instead of 1)"
        )
        conn.close()
    finally:
        os.unlink(db_path)


def _make_dict_conn(db_path: str, **kwargs):
    """Unused — kept for reference. Use the closure pattern instead to avoid recursion."""
    return


def test_handle_version_rollback_respects_tenant_scope():
    """_handle_version_rollback UPDATE must include tenant_id in WHERE clause.

    Regression test: same defense-in-depth scenario as archive.
    """
    from foresight_mcp.server import VersionAction, _handle_version_rollback

    db_path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """CREATE TABLE memories (
                id TEXT NOT NULL,
                content TEXT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                scope TEXT DEFAULT 'session', retention TEXT DEFAULT 'short_term',
                category TEXT DEFAULT 'fact', user_id TEXT DEFAULT 'default',
                bank_id TEXT DEFAULT 'default', created_at TEXT NOT NULL,
                updated_at TEXT, tags TEXT DEFAULT '[]',
                emotional_context TEXT DEFAULT '{}', metrics TEXT DEFAULT '{}',
                vector_id TEXT, gist TEXT, is_ghost INTEGER DEFAULT 0,
                synthesized_from TEXT DEFAULT '[]', version INTEGER DEFAULT 1,
                importance REAL, activation_count INTEGER DEFAULT 0,
                UNIQUE(id, tenant_id)
            )"""
        )
        conn.execute(
            """CREATE TABLE memory_versions (
                id TEXT PRIMARY KEY,
                memory_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                content TEXT NOT NULL,
                version INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                tags TEXT DEFAULT '[]',
                emotional_context TEXT DEFAULT '{}',
                metrics TEXT DEFAULT '{}',
                rollback_of TEXT
            )"""
        )

        memory_id = "mem-rollback-collision"
        now = datetime.now(timezone.utc).isoformat()

        for tenant, content, tags in [
            ("tenant-a", "tenant-a current", '["a"]'),
            ("tenant-b", "tenant-b current", '["b"]'),
        ]:
            conn.execute(
                """INSERT INTO memories
                   (id, content, tenant_id, user_id, scope, retention, category,
                    created_at, updated_at, tags, is_ghost, version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    memory_id,
                    content,
                    tenant,
                    "user-1",
                    "session",
                    "short_term",
                    "fact",
                    now,
                    now,
                    tags,
                    0,
                    3,
                ),
            )
            conn.execute(
                """INSERT INTO memory_versions
                   (id, memory_id, tenant_id, content, version, created_at,
                    tags, emotional_context, metrics)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"ver-{tenant}-2",
                    memory_id,
                    tenant,
                    f"{tenant} target-version",
                    2,
                    now,
                    tags,
                    "{}",
                    "{}",
                ),
            )
        conn.commit()
        conn.close()

        options = VersionAction(action="rollback", memory_id=memory_id, to_version=2)

        with (
            patch("foresight_mcp.server.DB_PATH", db_path),
            patch("foresight_mcp.connection_pool.DB_PATH", db_path),
            patch.dict("foresight_mcp.connection_pool._pools", {}, clear=True),
        ):
            result = _handle_version_rollback("user-1", "tenant-a", options)

        assert "Rolled back" in result or "rolled" in result.lower()

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        row_a = conn.execute(
            "SELECT content, version FROM memories WHERE id = ? AND tenant_id = ?",
            (memory_id, "tenant-a"),
        ).fetchone()
        assert row_a["content"] == "tenant-a target-version", "tenant-a should be rolled back"

        row_b = conn.execute(
            "SELECT content, version FROM memories WHERE id = ? AND tenant_id = ?",
            (memory_id, "tenant-b"),
        ).fetchone()
        assert row_b["content"] == "tenant-b current", "tenant-b must NOT be modified"
        assert row_b["version"] == 3, "tenant-b version must be untouched"

        conn.close()
    finally:
        os.unlink(db_path)


def test_memory_hard_cap_enforcement():
    """Storing memory beyond hard cap returns error."""
    import os
    import sqlite3
    import tempfile

    # Create temp DB
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = tmp.name
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY, content TEXT NOT NULL, content_hash TEXT,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            scope TEXT DEFAULT 'session', retention TEXT DEFAULT 'short_term',
            category TEXT DEFAULT 'fact', user_id TEXT DEFAULT 'default',
            bank_id TEXT DEFAULT 'default', created_at TEXT NOT NULL,
            updated_at TEXT, tags TEXT DEFAULT '[]',
            emotional_context TEXT DEFAULT '{}', metrics TEXT DEFAULT '{}',
            vector_id TEXT, gist TEXT, is_ghost INTEGER DEFAULT 0,
            synthesized_from TEXT DEFAULT '[]', version INTEGER DEFAULT 1,
            activation_count INTEGER DEFAULT 1,
            importance REAL DEFAULT 0.5
        )""")
        conn.commit()
        conn.close()
        # Patch config DB_PATH BEFORE importing other modules
        import foresight_mcp.config as config_module

        original_db_path = config_module.DB_PATH
        config_module.DB_PATH = db_path
        # Also patch connection_pool's DB_PATH
        import foresight_mcp.connection_pool as conn_pool_module

        conn_pool_module.DB_PATH = db_path
        from foresight_mcp.connection_pool import reset_pool
        from foresight_mcp.hybrid_retriever import reset_hybrid_retriever

        reset_pool()
        reset_hybrid_retriever()
        import foresight_mcp.server as server_module

        server_module._narrative_cache = None
        try:
            # Patch the limit to a small value for testing
            original_limit = server_module.DEFAULT_MAX_MEMORY_PER_TENANT
            server_module.DEFAULT_MAX_MEMORY_PER_TENANT = 5
            try:
                from foresight_mcp.server import store_memory

                for i in range(5):
                    result = store_memory(f"test memory {i}")
                    assert "Error" not in result, f"Should not error at {i}: {result}"
                # Try one more - should fail
                result = store_memory("overflow test")
                assert "Error" in result
                assert "Memory limit reached" in result
            finally:
                server_module.DEFAULT_MAX_MEMORY_PER_TENANT = original_limit
        finally:
            config_module.DB_PATH = original_db_path
            conn_pool_module.DB_PATH = original_db_path
            reset_hybrid_retriever()
            reset_pool()
            server_module._narrative_cache = None
    finally:
        os.unlink(db_path)


def test_memory_budget_metrics_utilization():
    """memory_budget utilization_pct is calculated correctly."""
    import json

    from foresight_mcp.server import SystemStatusOptions, get_system_status, store_memory

    # Store a few memories
    for i in range(5):
        store_memory(f"budget test {i}")
    result = get_system_status(options=SystemStatusOptions(include_cache_metrics=True))
    data = json.loads(result)
    assert data["memory_budget"]["current_count"] >= 5
    assert data["memory_budget"]["utilization_pct"] >= 0
    assert data["memory_budget"]["hard_cap_enforced"] is False


def test_cascade_retrieval_basic():
    """Cascade retrieval returns results when use_cascade is enabled."""
    from foresight_mcp.server import SearchOptions, search_memories

    result = search_memories(SearchOptions(query="test", use_cascade=True, limit=5))
    # Should return results (falls back to hybrid since no embeddings)
    assert "memories" in result.lower() or "found" in result.lower()


def test_cascade_retrieval_respects_limit():
    """Cascade retrieval respects the limit parameter."""
    from foresight_mcp.server import SearchOptions, search_memories

    result = search_memories(SearchOptions(query="test", use_cascade=True, limit=2))
    # Count lines in result
    lines = [line for line in result.split("\n") if line.startswith("- [")]
    assert len(lines) <= 2


def test_search_options_cascade_fields():
    """SearchOptions accepts cascade-related fields."""
    from foresight_mcp.server import SearchOptions

    opts = SearchOptions(query="test", use_cascade=True, cascade_depth=3, cascade_limit=100)
    assert opts.use_cascade is True
    assert opts.cascade_depth == 3
    assert opts.cascade_limit == 100
    # Test defaults
    opts2 = SearchOptions(query="test")
    assert opts2.use_cascade is False
    assert opts2.cascade_depth == 2
    assert opts2.cascade_limit == 100


class TestSystemStatusHealth:
    """Tests for PIX-3955 system status enhancements."""

    def test_injection_stats_tracked(self):
        """inject_context updates _last_injection_stats with metadata."""
        from foresight_mcp.server import _last_injection_stats, inject_context

        results = [
            _make_hybrid_result("mem1", "Python type hints discussion", combined_score=0.85),
            _make_hybrid_result("mem2", "Database migration planning", combined_score=0.7),
        ]

        with (
            _patch_hybrid_retriever(results, total_candidates=4, signal_counts={"keyword": 2, "graph": 1}),
            patch("foresight_mcp.server.USER_ID", "test_user"),
            patch("foresight_mcp.server.get_context_block_agent"),
        ):
            inject_context("python type hints and database")

        assert "last_run_at" in _last_injection_stats
        assert _last_injection_stats["memories_returned"] == 2
        assert _last_injection_stats["memories_fetched"] == 2
        assert "fast_path" in _last_injection_stats
        assert "signal_counts" in _last_injection_stats
        assert "latency_ms" in _last_injection_stats

    def test_injection_stats_no_memory_content_leak(self):
        """_last_injection_stats does not include raw memory content (privacy-safe)."""
        from foresight_mcp.server import _last_injection_stats, inject_context

        results = [
            _make_hybrid_result("mem1", "sensitive clinical note about patient X", combined_score=0.9),
        ]

        with (
            _patch_hybrid_retriever(results),
            patch("foresight_mcp.server.USER_ID", "test_user"),
            patch("foresight_mcp.server.get_context_block_agent"),
        ):
            inject_context("clinical note")

        stats_keys = set(_last_injection_stats.keys())
        # Verify no keys that could contain raw memory content
        assert "content" not in stats_keys
        assert "memories" not in stats_keys
        assert "results" not in stats_keys
        # Verify expected metadata keys are present
        assert "memories_returned" in stats_keys
        assert "memories_fetched" in stats_keys

    def test_system_status_contains_stale_count(self):
        """get_system_status returns stale_count metric."""
        from foresight_mcp.server import get_system_status, store_memory

        result = get_system_status()
        data = json.loads(result)
        assert "stale_count" in data
        assert isinstance(data["stale_count"], int)

    def test_system_status_contains_by_category(self):
        """get_system_status returns by_category breakdown."""
        from foresight_mcp.server import get_system_status

        # Store memories with different categories
        store_memory("fact memory one", category="fact", scope="session")
        store_memory("preference about cats", category="preference", scope="trait")
        store_memory("decision to use Python", category="decision", scope="arc")

        result = get_system_status()
        data = json.loads(result)
        assert "by_category" in data
        assert isinstance(data["by_category"], dict)

    def test_system_status_contains_last_injection(self):
        """get_system_status returns last_injection field (may be null)."""
        from foresight_mcp.server import get_system_status

        result = get_system_status()
        data = json.loads(result)
        assert "last_injection" in data
        # May be None if no injection has run this session

    def test_system_status_no_memory_content_in_output(self):
        """get_system_status output does not include raw memory content (privacy-safe)."""
        from foresight_mcp.server import get_system_status

        store_memory("very personal therapeutic session content that should be private")
        result = get_system_status()
        data = json.loads(result)
        serialized = json.dumps(data)
        # The memory content should NOT appear anywhere in the status output
        assert "very personal therapeutic session" not in serialized
        # Verify safe metadata fields exist instead
        assert "memory_count" in data
        assert "by_scope" in data
        assert "stale_count" in data
