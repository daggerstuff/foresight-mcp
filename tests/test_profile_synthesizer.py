"""Tests for MEM-1: User Profile Synthesis."""

import json
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import patch

from foresight_mcp import profile_synthesizer as ps_mod, subconscious as sub_mod
from foresight_mcp.context_blocks import update_context_block
from foresight_mcp.profile_synthesizer import (
    _deduplicate_lines,
    _extract_block_lines,
    _is_placeholder,
    profile_to_prompt,
    synthesize_profile,
)
from foresight_mcp.subconscious import ContextBlockAgent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_db() -> str:
    """Create a temporary DB with both memories and context_blocks schemas."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    conn = sqlite3.connect(db_path)
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
            importance REAL, activation_count INTEGER DEFAULT 0,
            strength_trend TEXT DEFAULT 'stable'
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS context_blocks (
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            label TEXT NOT NULL,
            content TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id, user_id, label)
        )"""
    )
    conn.commit()
    conn.close()
    return tmp.name


@dataclass
class MemorySeed:
    """Bundled seed options for _seed_memory."""

    content: str
    user_id: str = "test_user"
    scope: str = "fact"
    retention: str = "long_term"
    tags: list[str] | None = None
    importance: float = 0.5


def _seed_memory(conn: sqlite3.Connection, seed: MemorySeed) -> str:
    """Insert a test memory row and return its ID."""
    mid = f"mem_{abs(hash(seed.content))}"
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT OR IGNORE INTO memories
        (id, content, tenant_id, scope, retention, category, user_id,
         bank_id, created_at, updated_at, tags, emotional_context, metrics,
         is_ghost, importance)
        VALUES (?, ?, 'default', ?, ?, ?, ?, 'default', ?, ?, ?, '{}', '{}', 0, ?)""",
        (
            mid,
            seed.content,
            seed.scope,
            seed.retention,
            "test",
            seed.user_id,
            now,
            now,
            json.dumps(seed.tags or []),
            seed.importance,
        ),
    )
    conn.commit()
    return mid


def _seed_context_block(
    conn: sqlite3.Connection,
    label: str,
    content: str,
    *,
    user_id: str = "test_user",
) -> None:
    """Insert a test context block row."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT OR REPLACE INTO context_blocks
        (tenant_id, user_id, label, content, updated_at)
        VALUES ('default', ?, ?, ?, ?)""",
        (user_id, label, content, now),
    )
    conn.commit()


@contextmanager
def _patched_profile_env(db_path: str) -> Iterator[None]:
    """Point profile_synthesizer and context blocks at a test database."""

    with (
        patch.object(ps_mod, "DB_PATH", db_path),
        patch.object(sub_mod, "DB_PATH", db_path),
        patch.dict(sub_mod._context_block_agents, {}, clear=True),
    ):
        yield


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


class TestIsPlaceholder:
    def test_empty_line(self):
        assert _is_placeholder("") is True
        assert _is_placeholder("   ") is True

    def test_default_placeholders(self):
        assert _is_placeholder("(No preferences yet.") is True
        assert _is_placeholder("(no active guidance") is True
        assert _is_placeholder("ROLE: Curator") is True
        assert _is_placeholder("WHAT I AM:") is True
        assert _is_placeholder("AVAILABLE TOOLS:") is True

    def test_real_content(self):
        assert _is_placeholder("User prefers TypeScript") is False
        assert _is_placeholder("Working on auth migration") is False
        assert _is_placeholder("- [2026-05-01] Started new feature") is False


class TestDeduplicateLines:
    def test_exact_duplicates_removed(self):
        lines = ["a", "b", "a", "c", "b"]
        assert _deduplicate_lines(lines) == ["a", "b", "c"]

    def test_order_preserved(self):
        lines = ["c", "a", "b", "a"]
        assert _deduplicate_lines(lines) == ["c", "a", "b"]

    def test_empty_input(self):
        assert _deduplicate_lines([]) == []


class TestExtractBlockLines:
    def test_skips_placeholders(self):
        agent = ContextBlockAgent("test_user")
        agent.update_block("user_preferences", "Real preference\n(No preferences yet.")
        lines = _extract_block_lines(agent, ["user_preferences"])
        assert lines == ["Real preference"]

    def test_respects_max(self):
        agent = ContextBlockAgent("test_user")
        agent.update_block("user_preferences", "\n".join(f"pref_{i}" for i in range(20)))
        lines = _extract_block_lines(agent, ["user_preferences"], max_per_block=5)
        assert len(lines) <= 5


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


class TestSynthesizeProfile:
    def test_empty_user_returns_empty_lists(self):
        db_path = _make_test_db()
        with _patched_profile_env(db_path):
            profile = synthesize_profile("nonexistent_user")
        assert profile == {"static": [], "dynamic": []}

    def test_static_from_context_blocks(self):
        db_path = _make_test_db()
        with _patched_profile_env(db_path):
            update_context_block(
                "user_preferences",
                "User prefers dark mode\nUser uses Vim",
                user_id="test_user",
            )
            profile = synthesize_profile("test_user")
        assert "User prefers dark mode" in profile["static"]
        assert "User uses Vim" in profile["static"]

    def test_static_from_memories(self):
        db_path = _make_test_db()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _seed_memory(conn, MemorySeed("Senior engineer at Acme", scope="fact", retention="permanent", importance=0.9))
        _seed_memory(
            conn,
            MemorySeed("Uses functional programming patterns", scope="trait", retention="long_term", importance=0.8),
        )
        conn.close()

        with _patched_profile_env(db_path):
            profile = synthesize_profile("test_user")

        assert any("Senior engineer at Acme" in s for s in profile["static"])
        assert any("Uses functional programming" in s for s in profile["static"])

    def test_dynamic_from_context_blocks(self):
        db_path = _make_test_db()
        with _patched_profile_env(db_path):
            update_context_block(
                "project_context",
                "Working on auth service migration",
                user_id="test_user",
            )
            update_context_block(
                "pending_items",
                "Fix rate limiting bug",
                user_id="test_user",
            )
            profile = synthesize_profile("test_user")

        assert any("auth service migration" in d for d in profile["dynamic"])
        assert any("rate limiting" in d for d in profile["dynamic"])

    def test_dynamic_from_recent_sessions(self):
        db_path = _make_test_db()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _seed_memory(conn, MemorySeed("Debugging deployment pipeline", scope="session", retention="short_term"))
        _seed_memory(conn, MemorySeed("Working on Q2 roadmap", scope="arc", retention="short_term"))
        conn.close()

        with _patched_profile_env(db_path):
            profile = synthesize_profile("test_user")

        assert any("Debugging deployment" in d for d in profile["dynamic"])
        assert any("Q2 roadmap" in d for d in profile["dynamic"])

    def test_static_dynamic_separation(self):
        """Trait/fact memories go to static; session/arc go to dynamic."""
        db_path = _make_test_db()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _seed_memory(conn, MemorySeed("PREFERS TYPESCRIPT", scope="trait", retention="permanent"))
        _seed_memory(conn, MemorySeed("CURRENTLY FIXING AUTH", scope="session", retention="short_term"))
        conn.close()

        with _patched_profile_env(db_path):
            profile = synthesize_profile("test_user")

        assert any("PREFERS TYPESCRIPT" in s for s in profile["static"])
        assert any("CURRENTLY FIXING AUTH" in d for d in profile["dynamic"])

    def test_short_term_memories_excluded_from_static(self):
        """ephemeral/short_term memories should not appear in static even if scope=trait."""
        db_path = _make_test_db()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _seed_memory(conn, MemorySeed("Temporary preference", scope="trait", retention="short_term", importance=0.9))
        conn.close()

        with _patched_profile_env(db_path):
            profile = synthesize_profile("test_user")

        assert not any("Temporary preference" in s for s in profile["static"])

    def test_ghost_memories_excluded(self):
        """Ghost (archived) memories should not appear in either section."""
        db_path = _make_test_db()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        mid = _seed_memory(conn, MemorySeed("Old fact", scope="fact", retention="permanent"))
        conn.execute("UPDATE memories SET is_ghost = 1 WHERE id = ?", (mid,))
        conn.commit()
        conn.close()

        with _patched_profile_env(db_path):
            profile = synthesize_profile("test_user")

        assert not any("Old fact" in s for s in profile["static"])

    def test_profile_to_prompt_formatting(self):
        profile = {
            "static": ["Engineer", "Prefers dark mode"],
            "dynamic": ["Fixing auth bug"],
        }
        prompt = profile_to_prompt(profile, user_label="User")
        assert "ABOUT USER" in prompt
        assert "Engineer" in prompt
        assert "CURRENT CONTEXT" in prompt
        assert "Fixing auth bug" in prompt

    def test_profile_to_prompt_empty(self):
        prompt = profile_to_prompt({"static": [], "dynamic": []}, user_label="User")
        assert "No profile data available" in prompt

    def test_profile_to_prompt_custom_label(self):
        profile = {"static": ["Engineer"], "dynamic": []}
        prompt = profile_to_prompt(profile, user_label="Developer")
        assert "ABOUT DEVELOPER" in prompt
