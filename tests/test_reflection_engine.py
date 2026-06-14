"""
Tests for reflection engine.
"""

import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from foresight_mcp.reflection_engine import (
    ReflectionEngine,
    ReflectionReport,
    reset_reflection_engine,
)


@pytest.fixture(autouse=True)
def cleanup():
    reset_reflection_engine()
    yield
    reset_reflection_engine()


def create_test_db():
    """Create a temp DB with schema and test data."""
    fd, path = tempfile.mkstemp(suffix=".db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

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
 tenant_id TEXT NOT NULL DEFAULT 'default',
 user_id TEXT NOT NULL,
 name TEXT NOT NULL,
 entity_type TEXT NOT NULL,
 description TEXT,
 properties TEXT DEFAULT '{}',
 created_at TEXT DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(tenant_id, user_id, name, entity_type)
 )
 """)

    conn.execute("""
 CREATE TABLE entity_relationships (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 tenant_id TEXT NOT NULL DEFAULT 'default',
 user_id TEXT NOT NULL,
 source_entity_id TEXT NOT NULL,
 target_entity_id TEXT NOT NULL,
 relationship_type TEXT NOT NULL,
 confidence REAL DEFAULT 1.0,
 decay_factor REAL DEFAULT 1.0,
 last_accessed TEXT DEFAULT CURRENT_TIMESTAMP,
 metadata TEXT DEFAULT '{}',
 created_at TEXT DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(tenant_id, user_id, source_entity_id, target_entity_id, relationship_type)
 )
 """)

    conn.execute("""
 CREATE TABLE memory_entity_links (
 memory_id TEXT NOT NULL,
 entity_id TEXT NOT NULL,
 tenant_id TEXT NOT NULL DEFAULT 'default',
 user_id TEXT NOT NULL,
 relevance_score REAL DEFAULT 1.0,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY (memory_id, entity_id)
 )
 """)

    now = datetime.now(timezone.utc)
    uid = "test_user"

    # Mix of trends for interesting analysis
    memories = [
        ("mem_1", "Anxiety reduced after starting therapy", "fact", 0.8, "strengthening", now - timedelta(hours=12)),
        ("mem_2", "CBT techniques helping with stress", "fact", 0.7, "strengthening", now - timedelta(hours=24)),
        ("mem_3", "Meditation practice consistent", "fact", 0.6, "stable", now - timedelta(days=3)),
        ("mem_4", "Sleep quality improved", "fact", 0.5, "stable", now - timedelta(days=5)),
        ("mem_5", "Old coping strategies no longer needed", "fact", 0.3, "stale", now - timedelta(days=6)),
        ("mem_6", "Work boundary setting progress", "fact", 0.8, "strengthening", now - timedelta(hours=6)),
    ]

    for mid, content, cat, imp, trend, ts in memories:
        conn.execute(
            "INSERT INTO memories (id, user_id, tenant_id, content, category, importance, strength_trend, created_at) VALUES (?, ?, 'default', ?, ?, ?, ?, ?)",
            (mid, uid, content, cat, imp, trend, ts.isoformat()),
        )

    # Entities and relationships
    entities = [
        ("entity_therapy", "therapy", "concept", uid),
        ("entity_anxiety", "anxiety", "emotion", uid),
        ("entity_stress", "stress", "emotion", uid),
    ]

    for eid, name, etype, euid in entities:
        conn.execute(
            "INSERT INTO memory_entities (id, tenant_id, name, entity_type, user_id) VALUES (?, 'default', ?, ?, ?)",
            (eid, name, etype, euid),
        )

    # Relationships to make anxiety a hub
    conn.execute(
        "INSERT INTO entity_relationships (tenant_id, source_entity_id, target_entity_id, relationship_type, user_id) VALUES ('default', ?, ?, ?, ?)",
        ("entity_anxiety", "entity_therapy", "relates_to", uid),
    )
    conn.execute(
        "INSERT INTO entity_relationships (tenant_id, source_entity_id, target_entity_id, relationship_type, user_id) VALUES ('default', ?, ?, ?, ?)",
        ("entity_anxiety", "entity_stress", "relates_to", uid),
    )
    conn.execute(
        "INSERT INTO entity_relationships (tenant_id, source_entity_id, target_entity_id, relationship_type, user_id) VALUES ('default', ?, ?, ?, ?)",
        ("entity_stress", "entity_therapy", "supports", uid),
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


class TestReflectionEngine:
    """Test reflection engine core functionality."""

    def test_reflect_returns_report(self, test_db):
        engine = ReflectionEngine(test_db)
        report = engine.reflect("test_user", period="weekly")

        assert report is not None
        assert isinstance(report, ReflectionReport)
        assert report.period == "weekly"
        assert report.memories_analyzed >= 5

    def test_reflect_insufficient_data(self, test_db):
        engine = ReflectionEngine(test_db)
        report = engine.reflect("nonexistent_user", period="weekly")

        assert report is None

    def test_reflect_generates_insights(self, test_db):
        engine = ReflectionEngine(test_db)
        report = engine.reflect("test_user", period="weekly")

        assert len(report.insights) > 0
        # All insights should have evidence
        for insight in report.insights:
            assert len(insight.evidence_ids) > 0
            assert insight.confidence > 0
            assert insight.insight_type in ("trend", "pattern", "warning", "breakthrough", "contradiction")

    def test_trend_summary_structure(self, test_db):
        engine = ReflectionEngine(test_db)
        report = engine.reflect("test_user", period="weekly")

        ts = report.trend_summary
        assert "overall" in ts
        assert "trend_counts" in ts
        assert "total_memories" in ts
        assert ts["overall"] in ("improving", "declining", "stable")

    def test_improving_trend_detected(self, test_db):
        """With 3 strengthening memories out of 6, should detect improving."""
        engine = ReflectionEngine(test_db)
        report = engine.reflect("test_user", period="weekly")

        # 3/6 = 50% strengthening, should be 'improving'
        assert report.trend_summary["overall"] == "improving"

    def test_entity_summary_structure(self, test_db):
        engine = ReflectionEngine(test_db)
        report = engine.reflect("test_user", period="weekly")

        es = report.entity_summary
        assert "entity_type_counts" in es
        assert "top_connected_entities" in es

    def test_report_stored_as_memory(self, test_db):
        engine = ReflectionEngine(test_db)
        report = engine.reflect("test_user", period="weekly")

        # Verify the report was stored
        conn = sqlite3.connect(test_db)
        row = conn.execute(
            "SELECT id, content, category FROM memories WHERE id = ?",
            (report.report_id,),
        ).fetchone()
        conn.close()

        assert row is not None
        assert row[2] == "reflection"
        assert "Reflection" in row[1]

    def test_report_to_dict(self, test_db):
        engine = ReflectionEngine(test_db)
        report = engine.reflect("test_user", period="weekly")

        d = report.to_dict()
        assert "report_id" in d
        assert "insights" in d
        assert "trend_summary" in d
        assert "entity_summary" in d
        assert len(d["insights"]) > 0

    def test_monthly_reflect(self, test_db):
        engine = ReflectionEngine(test_db)
        report = engine.reflect("test_user", period="monthly")

        assert report is not None
        assert report.period == "monthly"

    def test_content_anchored_insights(self, test_db):
        """Insights should reference actual memory content, not just counts."""
        engine = ReflectionEngine(test_db)
        report = engine.reflect("test_user", period="weekly")

        # At least one insight should contain content from actual memories
        has_content_evidence = False
        for insight in report.insights:
            if insight.insight_type == "trend" and insight.summary.startswith("Progress in"):
                has_content_evidence = True
                # The summary should contain an excerpt from actual memory content
                assert len(insight.summary) > len("Progress in general: ")
                # evidence_ids should reference specific memories, not generic first-5
                assert len(insight.evidence_ids) >= 1

        assert has_content_evidence, "Expected at least one content-anchored trend insight"

    def test_insights_have_specific_evidence_ids(self, test_db):
        """Content-anchored insights should point to specific memory IDs, not generic slices."""
        engine = ReflectionEngine(test_db)
        report = engine.reflect("test_user", period="weekly")

        # Strengthening/decline insights should have evidence_ids pointing to actual memories
        for insight in report.insights:
            if insight.metadata.get("trend") in ("strengthening", "weakening"):
                # Should reference a single specific memory, not a slice of first N
                assert len(insight.evidence_ids) >= 1
                # The referenced ID should be one of the actual memory IDs
                all_ids = {"mem_1", "mem_2", "mem_3", "mem_4", "mem_5", "mem_6"}
                for eid in insight.evidence_ids:
                    assert eid in all_ids

    def test_gist_contains_insight_summaries(self, test_db):
        """Stored reflection memory gist should contain insight summaries."""
        engine = ReflectionEngine(test_db)
        report = engine.reflect("test_user", period="weekly")

        conn = sqlite3.connect(test_db)
        row = conn.execute(
            "SELECT gist FROM memories WHERE id = ?",
            (report.report_id,),
        ).fetchone()
        conn.close()

        assert row is not None
        gist = row[0]
        # Gist should contain at least one insight summary (not just 'improving')
        assert len(gist) > 20  # More than just 'improving' or 'unknown'
        # Should contain semicolons if multiple insights
        if len(report.insights) > 1:
            assert ";" in gist
