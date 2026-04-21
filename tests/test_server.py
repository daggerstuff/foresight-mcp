"""Tests for Foresight MCP server."""
import hashlib
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
from foresight_mcp import store_memory, memory_status
from foresight_mcp.server import (
    inject_context,
    _extract_terms,
    _score_memory_relevance,
    _STOP_WORDS,
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


def test_bridge_subconscious_to_memories():
    """_bridge_subconscious_to_memories stores extracted blocks as memories."""
    from foresight_mcp.subconscious import (
        SubconsciousAgent, USER_PREFERENCES, PENDING_ITEMS, SESSION_PATTERNS,
    )
    from foresight_mcp.server import _bridge_subconscious_to_memories

    agent = SubconsciousAgent(user_id="bridge_test_user")
    # Populate some blocks via the agent's normal extraction
    agent._extract_preference("I always use type hints")
    agent._extract_pending_item("TODO: add more tests", "sess_1")

    db_path = _make_test_db()
    with patch("foresight_mcp.server.get_db_connection", lambda: _mock_db_connection(db_path)), \
         patch("foresight_mcp.server.BANK_ID", "test_bank"):
        stored = _bridge_subconscious_to_memories(agent, "bridge_test_user")

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


def test_bridge_subconscious_dedup():
    """Bridging the same agent state twice should bump, not duplicate."""
    from foresight_mcp.subconscious import SubconsciousAgent
    from foresight_mcp.server import _bridge_subconscious_to_memories

    agent = SubconsciousAgent(user_id="dedup_bridge_user")
    agent._extract_preference("I prefer explicit returns")

    db_path = _make_test_db()
    with patch("foresight_mcp.server.get_db_connection", lambda: _mock_db_connection(db_path)), \
         patch("foresight_mcp.server.BANK_ID", "test_bank"):
        _bridge_subconscious_to_memories(agent, "dedup_bridge_user")
        _bridge_subconscious_to_memories(agent, "dedup_bridge_user")

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
    from foresight_mcp.server import _bridge_transcript_entities
    from foresight_mcp.entity_extractor import reset_entity_extractor
    from foresight_mcp.graph_store import reset_graph_store, GraphStore

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
    db_path = _make_inject_test_db(memories=[
        {"id": "mem1", "content": "User prefers Python type hints in all functions", "importance": 0.8, "created_at": now_iso},
        {"id": "mem2", "content": "Session discussed database migration strategies", "importance": 0.6, "created_at": now_iso},
    ])

    with patch("foresight_mcp.server.get_db_connection", lambda: _mock_db_with_rows(db_path)), \
         patch("foresight_mcp.server.USER_ID", "inject_test_user"), \
         patch("foresight_mcp.server.TENANT_ID", "default"), \
         patch("foresight_mcp.server.get_subconscious_agent"):
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

    with patch("foresight_mcp.server.get_db_connection", lambda: _mock_db_with_rows(db_path)), \
         patch("foresight_mcp.server.USER_ID", "inject_test_user"), \
         patch("foresight_mcp.server.TENANT_ID", "default"), \
         patch("foresight_mcp.server.get_subconscious_agent"):
        result = inject_context("python topic", max_memories=2)

    # Count memory lines (lines starting with "- [")
    memory_lines = [l for l in result.splitlines() if l.startswith("- [")]
    assert len(memory_lines) <= 2


def test_inject_context_no_match():
    """inject_context with no matching memories returns empty context message."""
    now_iso = datetime.now(timezone.utc).isoformat()
    db_path = _make_inject_test_db(memories=[
        {"id": "mem1", "content": "Completely unrelated content about sailing", "importance": 0.1, "created_at": now_iso},
    ])

    with patch("foresight_mcp.server.get_db_connection", lambda: _mock_db_with_rows(db_path)), \
         patch("foresight_mcp.server.USER_ID", "inject_test_user"), \
         patch("foresight_mcp.server.TENANT_ID", "default"), \
         patch("foresight_mcp.server.get_subconscious_agent"):
        result = inject_context("quantum computing algorithms", min_relevance=0.5)

    assert "0 memories surfaced" in result


def test_inject_context_empty_conversation_text():
    """inject_context with empty conversation text still works (no terms to match)."""
    db_path = _make_inject_test_db(memories=[])

    with patch("foresight_mcp.server.get_db_connection", lambda: _mock_db_with_rows(db_path)), \
         patch("foresight_mcp.server.USER_ID", "inject_test_user"), \
         patch("foresight_mcp.server.TENANT_ID", "default"), \
         patch("foresight_mcp.server.get_subconscious_agent"):
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

    # All 4 terms match content, importance=0.8*0.5=0.4, decay=~0.5 (fresh)
    # overlap=4, importance_boost=0.4, recency=0.5
    assert score > 4.0  # At minimum overlap of 4


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
