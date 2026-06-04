"""Tests for Foresight MCP server."""

import hashlib
import json
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastmcp import Client
from foresight_cli.cli import _decode_tool_result
from foresight_mcp import memory_status, store_memory
from foresight_mcp.block_registry import MemoryBlockSchema
from foresight_mcp.context_blocks import register_context_block_schema
from foresight_mcp.server import (
    ContextBlockAction,
    CurationRunAction,
    _extract_terms,
    _score_memory_relevance,
    inject_context,
    manage_context_blocks,
    manage_curation_runs,
    mcp,
)


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
        id TEXT PRIMARY KEY, content TEXT NOT NULL,
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

    # Set up graph store schema directly
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS memory_entities (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL,
        entity_type TEXT NOT NULL, description TEXT,
        properties TEXT DEFAULT '{}',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, name, entity_type)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS entity_relationships (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
        source_entity_id TEXT NOT NULL, target_entity_id TEXT NOT NULL,
        relationship_type TEXT NOT NULL, confidence REAL DEFAULT 1.0,
        metadata TEXT DEFAULT '{}',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, source_entity_id, target_entity_id, relationship_type)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS memory_entity_links (
        memory_id TEXT NOT NULL, entity_id TEXT NOT NULL,
        user_id TEXT NOT NULL, relevance_score REAL DEFAULT 1.0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (memory_id, entity_id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_user ON memory_entities(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_type ON memory_entities(entity_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_relationships_source ON entity_relationships(source_entity_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_relationships_target ON entity_relationships(target_entity_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entity_links_memory ON memory_entity_links(memory_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entity_links_entity ON memory_entity_links(entity_id)")
    conn.commit()
    conn.close()

    # Patch get_graph_store at its source module (it's imported lazily)
    with patch("foresight_mcp.graph_store.get_graph_store", lambda: GraphStore(db_path)):
        count = _bridge_transcript_entities(messages, "entity_test_user")

    # The rule-based extractor finds "anxiety" (emotion) and "work" (concept)
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
    now_iso = datetime.now(timezone.utc).isoformat()
    db_path = _make_inject_test_db(
        memories=[
            {
                "id": "mem1",
                "content": "User prefers Python type hints in all functions",
                "importance": 0.8,
                "created_at": now_iso,
            },
            {
                "id": "mem2",
                "content": "Session discussed database migration strategies",
                "importance": 0.6,
                "created_at": now_iso,
            },
        ]
    )

    with (
        patch("foresight_mcp.server.get_db_connection", lambda: _mock_db_with_rows(db_path)),
        patch("foresight_mcp.server.USER_ID", "inject_test_user"),
        patch("foresight_mcp.server.get_context_block_agent"),
    ):
        result = inject_context("Let's talk about database and type hints")

    assert "Relevant Context" in result
    assert "mem1" in result or "mem2" in result


def test_inject_context_respects_max_memories():
    """inject_context respects the max_memories limit."""
    now_iso = datetime.now(timezone.utc).isoformat()
    memories = [
        {"id": f"mem{i}", "content": f"Memory about python topic number {i}", "importance": 0.9, "created_at": now_iso}
        for i in range(10)
    ]
    db_path = _make_inject_test_db(memories=memories)

    with (
        patch("foresight_mcp.server.get_db_connection", lambda: _mock_db_with_rows(db_path)),
        patch("foresight_mcp.server.USER_ID", "inject_test_user"),
        patch("foresight_mcp.server.get_context_block_agent"),
    ):
        result = inject_context("python topic", max_memories=2)

    # Count memory lines (lines starting with "- [")
    memory_lines = [l for l in result.splitlines() if l.startswith("- [")]
    assert len(memory_lines) <= 2


def test_inject_context_no_match():
    """inject_context with no matching memories returns empty context message."""
    now_iso = datetime.now(timezone.utc).isoformat()
    db_path = _make_inject_test_db(
        memories=[
            {
                "id": "mem1",
                "content": "Completely unrelated content about sailing",
                "importance": 0.1,
                "created_at": now_iso,
            },
        ]
    )

    with (
        patch("foresight_mcp.server.get_db_connection", lambda: _mock_db_with_rows(db_path)),
        patch("foresight_mcp.server.USER_ID", "inject_test_user"),
        patch("foresight_mcp.server.get_context_block_agent"),
    ):
        result = inject_context("quantum computing algorithms", min_relevance=0.5)

    assert "0 memories surfaced" in result


def test_inject_context_empty_conversation_text():
    """inject_context with empty conversation text still works (no terms to match)."""
    db_path = _make_inject_test_db(memories=[])

    with (
        patch("foresight_mcp.server.get_db_connection", lambda: _mock_db_with_rows(db_path)),
        patch("foresight_mcp.server.USER_ID", "inject_test_user"),
        patch("foresight_mcp.server.get_context_block_agent"),
    ):
        result = inject_context("")

    assert "0 memories surfaced" in result


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
        assert "Content exceeds char limit" in invalid["error"]

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


def _seed_memory(db_path: str, *, memory_id: str, content: str, bank_id: str, user_id: str) -> None:
    """Insert a memory row for curation tests."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO memories
        (id, content, tenant_id, scope, retention, category, user_id, bank_id, created_at,
         updated_at, tags, emotional_context, metrics, is_ghost, synthesized_from, version,
         importance, activation_count, decay_rate, retrieval_count, strength_trend, last_retrieved_at, accessed_at)
        VALUES (?, ?, 'default', 'arc', 'long_term', 'fact', ?, ?, ?, ?, '[]', '{}', '{}', 0, '[]', 1, 1.0, 0, 0.01, 0, 'stable', NULL, ?)""",
        (memory_id, content, user_id, bank_id, now, now, now),
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
            "tenant_id": "default",
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
