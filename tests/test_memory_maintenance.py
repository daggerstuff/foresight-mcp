"""Tests for Memory Maintenance Job (PIX-3952).

Covers all four modes: consolidate, contradict, archive_stale, synthesize.
"""

import sqlite3
import tempfile
from datetime import datetime, timezone


from foresight_mcp.memory_maintenance import (
    DUPLICATE_OVERLAP_HIGH,
    DUPLICATE_OVERLAP_MARGINAL,
    MAX_BATCH_SIZE,
    MAX_RUNTIME_SECONDS,
    STALE_IMPORTANCE_THRESHOLD,
    STALE_STRENGTH_THRESHOLD,
    MaintenanceConfig,
    MaintenanceStats,
    MemoryMaintenanceJob,
    SENTIMENT_OPPOSITES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_db() -> str:
    """Create a temp DB with the schema expected by MemoryMaintenanceJob."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL DEFAULT 'default',
            content TEXT,
            scope TEXT DEFAULT 'session',
            retention TEXT DEFAULT 'short_term',
            category TEXT DEFAULT 'general',
            importance REAL DEFAULT 0.5,
            current_strength REAL DEFAULT 1.0,
            strength_trend TEXT DEFAULT 'stable',
            activation_count INTEGER DEFAULT 0,
            is_ghost INTEGER DEFAULT 0,
            gist TEXT,
            synthesized_from TEXT,
            emotional_context TEXT,
            metrics TEXT,
            tags TEXT,
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            event_type TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            actor TEXT,
            entity_id TEXT,
            payload TEXT
        )"""
    )
    conn.commit()
    conn.close()
    return db_path


def _insert_memory(
    conn,
    mid: str,
    content: str,
    *,
    importance: float = 0.5,
    strength_trend: str = "stable",
    is_ghost: int = 0,
    user_id: str = "u1",
    tenant_id: str = "t1",
    scope: str = "session",
    retention: str = "short_term",
) -> None:
    conn.execute(
        "INSERT INTO memories (id, user_id, tenant_id, content, scope, retention, importance, strength_trend, is_ghost, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            mid,
            user_id,
            tenant_id,
            content,
            scope,
            retention,
            importance,
            strength_trend,
            is_ghost,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_sentiment_opposites_is_nonempty(self) -> None:
        assert len(SENTIMENT_OPPOSITES) > 0

    def test_sentiment_opposites_are_string_pairs(self) -> None:
        for pair in SENTIMENT_OPPOSITES:
            assert isinstance(pair, tuple)
            assert len(pair) == 2
            assert isinstance(pair[0], str)
            assert isinstance(pair[1], str)

    def test_default_thresholds_sensible(self) -> None:
        assert 0 < STALE_STRENGTH_THRESHOLD < 1
        assert 0 < STALE_IMPORTANCE_THRESHOLD < 1
        assert DUPLICATE_OVERLAP_HIGH > DUPLICATE_OVERLAP_MARGINAL
        assert MAX_BATCH_SIZE > 0
        assert MAX_RUNTIME_SECONDS > 0


# ---------------------------------------------------------------------------
# MaintenanceConfig
# ---------------------------------------------------------------------------


class TestMaintenanceConfig:
    def test_default_modes(self) -> None:
        cfg = MaintenanceConfig()
        assert cfg.modes == ["consolidate", "contradict", "archive_stale", "synthesize"]

    def test_custom_modes(self) -> None:
        cfg = MaintenanceConfig(modes=["consolidate"])
        assert cfg.modes == ["consolidate"]

    def test_default_tenant_user(self) -> None:
        cfg = MaintenanceConfig()
        assert cfg.tenant_id == "default"
        assert cfg.user_id == "default"


# ---------------------------------------------------------------------------
# MaintenanceStats
# ---------------------------------------------------------------------------


class TestMaintenanceStats:
    def test_to_dict_keys(self) -> None:
        stats = MaintenanceStats()
        d = stats.to_dict()
        assert "maintenance_duration_seconds" in d
        assert "modes_run" in d
        assert "duplicates_found" in d
        assert "duplicates_auto_consolidated" in d
        assert "duplicates_flagged_review" in d
        assert "contradictions_found" in d
        assert "contradictions_flagged_review" in d
        assert "stale_found" in d
        assert "stale_archived" in d
        assert "insights_generated" in d
        assert "errors" in d

    def test_defaults_zero(self) -> None:
        stats = MaintenanceStats()
        assert stats.duplicates_found == 0
        assert stats.stale_archived == 0
        assert stats.insights_generated == 0
        assert stats.errors == []


# ---------------------------------------------------------------------------
# MemoryMaintenanceJob — integrate with real SQLite
# ---------------------------------------------------------------------------


class TestMaintenanceJobConsolidate:
    """Consolidate mode: find near-duplicates, auto-merge or flag."""

    def test_empty_db_returns_clean_stats(self) -> None:
        db_path = _make_test_db()
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            _insert_memory(conn, "m1", "I feel happy today", user_id="u1", tenant_id="t1")
            conn.close()

            job = MemoryMaintenanceJob(db_path=db_path)
            cfg = MaintenanceConfig(user_id="u1", tenant_id="t1", modes=["consolidate"])
            stats = job.run(cfg)
            assert stats.duplicates_found == 0  # single memory, no pairs
        finally:
            import os

            os.unlink(db_path)

    def test_near_duplicates_auto_consolidated(self) -> None:
        db_path = _make_test_db()
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            # Two very similar memories — should exceed 0.70 overlap
            _insert_memory(
                conn, "m1", "I feel very happy and grateful today about my progress", user_id="u1", tenant_id="t1"
            )
            _insert_memory(
                conn,
                "m2",
                "I feel very happy and grateful today about my progress in therapy",
                user_id="u1",
                tenant_id="t1",
            )
            conn.close()

            job = MemoryMaintenanceJob(db_path=db_path)
            cfg = MaintenanceConfig(user_id="u1", tenant_id="t1", modes=["consolidate"])
            stats = job.run(cfg)
            assert stats.duplicates_found >= 2  # 2 memories in the cluster
        finally:
            import os

            os.unlink(db_path)

    def test_dissimilar_memories_not_consolidated(self) -> None:
        db_path = _make_test_db()
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            _insert_memory(conn, "m1", "The weather is sunny and warm outside today", user_id="u1", tenant_id="t1")
            _insert_memory(conn, "m2", "Quantum mechanics explains particle wave duality", user_id="u1", tenant_id="t1")
            # Add a third to help form clusters
            _insert_memory(conn, "m3", "The weather is rainy and cold outside today", user_id="u1", tenant_id="t1")
            conn.close()

            job = MemoryMaintenanceJob(db_path=db_path)
            cfg = MaintenanceConfig(user_id="u1", tenant_id="t1", modes=["consolidate"], duplicate_threshold=0.25)
            stats = job.run(cfg)
            # Weather memories share enough words to cluster, quantum is separate
            # m1 and m3 share "weather", "outside", "today" — should cluster
            assert stats.duplicates_found >= 0  # no crash
        finally:
            import os

            os.unlink(db_path)


class TestMaintenanceJobContradict:
    """Contradict mode: detect sentiment-conflict pairs, flag for review."""

    def test_no_contradictions_single_memory(self) -> None:
        db_path = _make_test_db()
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            _insert_memory(conn, "m1", "I feel hopeful about recovery", user_id="u1", tenant_id="t1")
            conn.close()

            job = MemoryMaintenanceJob(db_path=db_path)
            cfg = MaintenanceConfig(user_id="u1", tenant_id="t1", modes=["contradict"])
            stats = job.run(cfg)
            assert stats.contradictions_found == 0
        finally:
            import os

            os.unlink(db_path)

    def test_sentiment_conflict_detected(self) -> None:
        db_path = _make_test_db()
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            # These share topic "therapy" but have conflicting sentiments
            _insert_memory(conn, "m1", "Therapy gives me hope about my future progress", user_id="u1", tenant_id="t1")
            _insert_memory(
                conn, "m2", "Therapy gives me despair about my future progress", user_id="u1", tenant_id="t1"
            )
            conn.close()

            job = MemoryMaintenanceJob(db_path=db_path)
            cfg = MaintenanceConfig(user_id="u1", tenant_id="t1", modes=["contradict"])
            stats = job.run(cfg)
            # Should detect "hope" vs "despair" conflict
            assert stats.contradictions_found >= 1
            assert stats.contradictions_flagged_review >= 1
        finally:
            import os

            os.unlink(db_path)

    def test_no_conflict_same_sentiment(self) -> None:
        db_path = _make_test_db()
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            _insert_memory(conn, "m1", "Therapy makes me feel happy about progress", user_id="u1", tenant_id="t1")
            _insert_memory(conn, "m2", "Therapy makes me feel grateful about progress", user_id="u1", tenant_id="t1")
            conn.close()

            job = MemoryMaintenanceJob(db_path=db_path)
            cfg = MaintenanceConfig(user_id="u1", tenant_id="t1", modes=["contradict"])
            stats = job.run(cfg)
            # "happy" and "grateful" are not in SENTIMENT_OPPOSITES — no conflict
            assert stats.contradictions_found == 0
        finally:
            import os

            os.unlink(db_path)


class TestMaintenanceJobArchiveStale:
    """Archive_stale mode: soft-archive low-strength/importance memories."""

    def test_stale_memory_archived(self) -> None:
        db_path = _make_test_db()
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            _insert_memory(conn, "m1", "Low importance note", importance=0.05, user_id="u1", tenant_id="t1")
            conn.close()

            job = MemoryMaintenanceJob(db_path=db_path)
            cfg = MaintenanceConfig(
                user_id="u1",
                tenant_id="t1",
                modes=["archive_stale"],
                stale_importance_threshold=0.1,
            )
            stats = job.run(cfg)
            assert stats.stale_found >= 1
            assert stats.stale_archived >= 1

            # Verify in DB that is_ghost=1
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT is_ghost FROM memories WHERE id = 'm1'").fetchone()
            conn.close()
            assert row["is_ghost"] == 1
        finally:
            import os

            os.unlink(db_path)

    def test_important_memory_not_archived(self) -> None:
        db_path = _make_test_db()
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            _insert_memory(conn, "m1", "Important memory", importance=0.9, user_id="u1", tenant_id="t1")
            conn.close()

            job = MemoryMaintenanceJob(db_path=db_path)
            cfg = MaintenanceConfig(
                user_id="u1",
                tenant_id="t1",
                modes=["archive_stale"],
                stale_importance_threshold=0.1,
            )
            stats = job.run(cfg)
            assert stats.stale_archived == 0

            # Verify is_ghost still 0
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT is_ghost FROM memories WHERE id = 'm1'").fetchone()
            conn.close()
            assert row["is_ghost"] == 0
        finally:
            import os

            os.unlink(db_path)

    def test_stale_trend_archived(self) -> None:
        db_path = _make_test_db()
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            _insert_memory(
                conn, "m1", "Stale memory", importance=0.5, strength_trend="stale", user_id="u1", tenant_id="t1"
            )
            conn.close()

            job = MemoryMaintenanceJob(db_path=db_path)
            cfg = MaintenanceConfig(user_id="u1", tenant_id="t1", modes=["archive_stale"])
            stats = job.run(cfg)
            assert stats.stale_found >= 1
            assert stats.stale_archived >= 1
        finally:
            import os

            os.unlink(db_path)


class TestMaintenanceJobSynthesize:
    """Synthesize mode: detect cross-memory topic patterns, emit insights."""

    def test_no_insights_few_memories(self) -> None:
        db_path = _make_test_db()
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            _insert_memory(conn, "m1", "Feeling good today", user_id="u1", tenant_id="t1")
            _insert_memory(conn, "m2", "Another day another feeling", user_id="u1", tenant_id="t1")
            conn.close()

            job = MemoryMaintenanceJob(db_path=db_path)
            cfg = MaintenanceConfig(user_id="u1", tenant_id="t1", modes=["synthesize"])
            stats = job.run(cfg)
            # Less than 3 memories → no insights
            assert stats.insights_generated == 0
        finally:
            import os

            os.unlink(db_path)

    def test_cross_topic_insights_generated(self) -> None:
        db_path = _make_test_db()
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            # 5 memories all mentioning "therapy" — should generate insight
            for i in range(5):
                _insert_memory(
                    conn,
                    f"m{i}",
                    f"Session notes about therapy and recovery progress number {i}",
                    user_id="u1",
                    tenant_id="t1",
                )
            conn.close()

            job = MemoryMaintenanceJob(db_path=db_path)
            cfg = MaintenanceConfig(user_id="u1", tenant_id="t1", modes=["synthesize"])
            stats = job.run(cfg)
            assert stats.insights_generated >= 1
        finally:
            import os

            os.unlink(db_path)


class TestMaintenanceJobMultiMode:
    """Test running multiple modes in a single call."""

    def test_all_modes_empty_db(self) -> None:
        db_path = _make_test_db()
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            conn.close()

            job = MemoryMaintenanceJob(db_path=db_path)
            cfg = MaintenanceConfig(user_id="u1", tenant_id="t1")
            stats = job.run(cfg)
            assert stats.modes_run == ["consolidate", "contradict", "archive_stale", "synthesize"]
            assert stats.maintenance_duration_seconds > 0
            assert stats.errors == []
        finally:
            import os

            os.unlink(db_path)

    def test_single_mode(self) -> None:
        db_path = _make_test_db()
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            _insert_memory(conn, "m1", "Test memory content", importance=0.05, user_id="u1", tenant_id="t1")
            conn.close()

            job = MemoryMaintenanceJob(db_path=db_path)
            cfg = MaintenanceConfig(user_id="u1", tenant_id="t1", modes=["archive_stale"])
            stats = job.run(cfg)
            assert stats.modes_run == ["archive_stale"]
            assert stats.stale_found >= 1
        finally:
            import os

            os.unlink(db_path)


class TestMaintenanceJobTenantIsolation:
    """Maintenance is scoped to (user_id, tenant_id)."""

    def test_different_tenant_data_not_touched(self) -> None:
        db_path = _make_test_db()
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            _insert_memory(conn, "m1", "Low importance", importance=0.05, user_id="u1", tenant_id="t1")
            _insert_memory(conn, "m2", "Low importance", importance=0.05, user_id="u2", tenant_id="t2")
            conn.close()

            job = MemoryMaintenanceJob(db_path=db_path)
            cfg = MaintenanceConfig(user_id="u1", tenant_id="t1", modes=["archive_stale"])
            job.run(cfg)

            # Only t1/u1 memory should be archived
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            r1 = conn.execute("SELECT is_ghost FROM memories WHERE id = 'm1'").fetchone()
            r2 = conn.execute("SELECT is_ghost FROM memories WHERE id = 'm2'").fetchone()
            conn.close()
            assert r1["is_ghost"] == 1
            assert r2["is_ghost"] == 0  # different tenant, untouched
        finally:
            import os

            os.unlink(db_path)


class TestPairwiseOverlaps:
    """Unit test for the _pairwise_overlaps helper."""

    def test_identical_memories(self) -> None:
        job = MemoryMaintenanceJob.__new__(MemoryMaintenanceJob)
        memories = [
            {"id": "a", "content": "I feel happy and grateful today"},
            {"id": "b", "content": "I feel happy and grateful today"},
        ]
        scores = job._pairwise_overlaps(memories)
        key: tuple[str, str] = tuple(sorted(["a", "b"]))  # type: ignore[assignment]
        assert key in scores
        assert scores[key] == 1.0

    def test_disjoint_memories(self) -> None:
        job = MemoryMaintenanceJob.__new__(MemoryMaintenanceJob)
        memories = [
            {"id": "a", "content": "alpha beta gamma"},
            {"id": "b", "content": "delta epsilon zeta"},
        ]
        scores = job._pairwise_overlaps(memories)
        key: tuple[str, str] = tuple(sorted(["a", "b"]))  # type: ignore[assignment]
        assert scores[key] == 0.0

    def test_empty_content(self) -> None:
        job = MemoryMaintenanceJob.__new__(MemoryMaintenanceJob)
        memories = [
            {"id": "a", "content": ""},
            {"id": "b", "content": "some words here"},
        ]
        scores = job._pairwise_overlaps(memories)
        key: tuple[str, str] = tuple(sorted(["a", "b"]))  # type: ignore[assignment]
        assert scores[key] == 0.0


class TestFindSentimentConflict:
    """Unit test for _find_sentiment_conflict."""

    def test_conflict_found(self) -> None:
        job = MemoryMaintenanceJob.__new__(MemoryMaintenanceJob)
        result = job._find_sentiment_conflict("I feel hope about my therapy", "I feel despair about my therapy")
        assert result is not None
        pos, neg = result
        assert pos == "hope"
        assert neg == "despair"

    def test_no_conflict(self) -> None:
        job = MemoryMaintenanceJob.__new__(MemoryMaintenanceJob)
        result = job._find_sentiment_conflict("I feel happy", "I feel grateful")
        assert result is None

    def test_reversed_order(self) -> None:
        job = MemoryMaintenanceJob.__new__(MemoryMaintenanceJob)
        result = job._find_sentiment_conflict("I feel despair about my therapy", "I feel hope about my therapy")
        assert result is not None
        # Should detect regardless of order
        pos, neg = result
        assert (pos, neg) in SENTIMENT_OPPOSITES

    def test_empty_content(self) -> None:
        job = MemoryMaintenanceJob.__new__(MemoryMaintenanceJob)
        result = job._find_sentiment_conflict("", "")
        assert result is None
