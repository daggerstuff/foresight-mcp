"""Tests for MEM-8: Memory Strength Decay Model."""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from foresight_mcp import decay_model as mod
from foresight_mcp.decay_model import (
    DEFAULT_ACTIVATION_BOOST,
    DEFAULT_HALF_LIFE_HOURS,
    DEFAULT_MIN_IMPORTANCE,
    DEFAULT_STALE_THRESHOLD,
    DEFAULT_STRENGTHENING_THRESHOLD,
    DecayConfig,
    DecayModelError,
    DecayStats,
    MemoryDecayService,
    StrengthEvent,
    get_decay_model,
    reset_decay_model,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_db() -> str:
    """Create a temp DB with memories + decay_config + memory_decay_events tables."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            content TEXT NOT NULL,
            category TEXT DEFAULT 'general',
            importance REAL DEFAULT 0.5,
            current_strength REAL,
            strength_trend TEXT DEFAULT 'stable',
            activation_count INTEGER DEFAULT 0,
            last_decay_at TEXT,
            accessed_at TEXT,
            last_retrieved_at TEXT,
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS decay_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            half_life_hours REAL DEFAULT 168.0,
            min_importance REAL DEFAULT 0.1,
            activation_boost REAL DEFAULT 1.2,
            strengthening_threshold INTEGER DEFAULT 5,
            stale_threshold REAL DEFAULT 0.2,
            UNIQUE(tenant_id, user_id, category)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS memory_decay_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            memory_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            old_strength REAL,
            new_strength REAL,
            decay_factor REAL,
            reason TEXT,
            created_at TEXT NOT NULL
        )"""
    )
    conn.commit()
    conn.close()
    return db_path


@contextmanager
def _patched_service(db_path: str) -> Iterator[MemoryDecayService]:
    reset_decay_model()
    with (
        patch.object(mod, "DB_PATH", db_path),
        patch("foresight_mcp.config.DB_PATH", db_path),
        patch("foresight_mcp.decay_model.DB_PATH", db_path),
    ):
        svc = MemoryDecayService(db_path)
        try:
            yield svc
        finally:
            reset_decay_model()


def _insert_memory(
    db_path: str,
    user_id: str = "u1",
    tenant_id: str = "default",
    importance: float = 0.5,
    current_strength: float | None = None,
    last_decay_at: str | None = None,
    activation_count: int = 0,
    category: str = "general",
) -> str:
    mid = f"mem-{uuid.uuid4().hex[:8]}"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO memories
            (id, tenant_id, user_id, content, category, importance,
             current_strength, last_decay_at, activation_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            mid,
            tenant_id,
            user_id,
            f"content-{mid}",
            category,
            importance,
            current_strength,
            last_decay_at,
            activation_count,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    conn.close()
    return mid


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_default_constants_in_range():
    assert DEFAULT_HALF_LIFE_HOURS > 0
    assert 0.0 <= DEFAULT_MIN_IMPORTANCE <= 1.0
    assert DEFAULT_ACTIVATION_BOOST > 0
    assert DEFAULT_STRENGTHENING_THRESHOLD >= 0
    assert 0.0 <= DEFAULT_STALE_THRESHOLD <= 1.0


# ---------------------------------------------------------------------------
# DecayConfig dataclass
# ---------------------------------------------------------------------------


def test_decay_config_to_dict_round_trip():
    cfg = DecayConfig(
        tenant_id="t1",
        user_id="u1",
        category="c1",
        half_life_hours=200.0,
        min_importance=0.05,
        activation_boost=1.5,
        strengthening_threshold=3,
        stale_threshold=0.15,
    )
    out = cfg.to_dict()
    assert out == {
        "tenant_id": "t1",
        "user_id": "u1",
        "category": "c1",
        "half_life_hours": 200.0,
        "min_importance": 0.05,
        "activation_boost": 1.5,
        "strengthening_threshold": 3,
        "stale_threshold": 0.15,
    }


def test_decay_config_defaults_factory():
    cfg = DecayConfig.defaults("t1", "u1", "general")
    assert cfg.tenant_id == "t1"
    assert cfg.user_id == "u1"
    assert cfg.category == "general"
    assert cfg.half_life_hours == DEFAULT_HALF_LIFE_HOURS


# ---------------------------------------------------------------------------
# DecayStats
# ---------------------------------------------------------------------------


def test_decay_stats_starts_zero():
    s = DecayStats()
    assert s.processed == 0
    assert s.updated == 0
    assert s.skipped == 0
    assert s.reinforced == 0
    assert s.trend_counts == {}


def test_decay_stats_to_dict_includes_all_keys():
    s = DecayStats(processed=3, updated=2, skipped=1, reinforced=1, avg_decay_factor=0.75, trend_counts={"stable": 2})
    out = s.to_dict()
    assert out["processed"] == 3
    assert out["updated"] == 2
    assert out["trend_counts"] == {"stable": 2}


# ---------------------------------------------------------------------------
# Decay math
# ---------------------------------------------------------------------------


def test_compute_decay_factor_zero_hours_returns_one():
    assert MemoryDecayService._compute_decay_factor(0.0, 168.0) == 1.0


def test_compute_decay_factor_one_half_life_returns_half():
    factor = MemoryDecayService._compute_decay_factor(168.0, 168.0)
    assert abs(factor - 0.5) < 1e-9


def test_compute_decay_factor_two_half_lives_returns_quarter():
    factor = MemoryDecayService._compute_decay_factor(336.0, 168.0)
    assert abs(factor - 0.25) < 1e-9


def test_compute_decay_factor_zero_half_life_returns_zero():
    assert MemoryDecayService._compute_decay_factor(10.0, 0.0) == 0.0


def test_compute_decay_factor_negative_hours_clamps_to_zero():
    assert MemoryDecayService._compute_decay_factor(-1.0, 168.0) == 1.0


def test_compute_trend_stale_when_below_threshold():
    cfg = DecayConfig("t", "u", "c", stale_threshold=0.2)
    assert MemoryDecayService._compute_trend(0.1, 10, 0.0, cfg) == "stale"


def test_compute_trend_strengthening_at_threshold():
    cfg = DecayConfig(
        "t",
        "u",
        "c",
        strengthening_threshold=5,
        half_life_hours=168.0,
    )
    assert MemoryDecayService._compute_trend(0.5, 5, 0.0, cfg) == "strengthening"


def test_compute_trend_weakening_old_no_activation():
    cfg = DecayConfig(
        "t",
        "u",
        "c",
        strengthening_threshold=5,
        half_life_hours=168.0,
    )
    assert MemoryDecayService._compute_trend(0.5, 1, 200.0, cfg) == "weakening"


def test_compute_trend_stable_default():
    cfg = DecayConfig("t", "u", "c", half_life_hours=168.0)
    assert MemoryDecayService._compute_trend(0.5, 2, 50.0, cfg) == "stable"


# ---------------------------------------------------------------------------
# get_decay_config
# ---------------------------------------------------------------------------


def test_get_decay_config_returns_defaults_when_missing():
    with _patched_service(_make_test_db()) as svc:
        cfg = svc.get_decay_config("u1", "default", "general")
    assert cfg.tenant_id == "default"
    assert cfg.user_id == "u1"
    assert cfg.half_life_hours == DEFAULT_HALF_LIFE_HOURS


def test_get_decay_config_rejects_empty_user_id():
    with _patched_service(_make_test_db()) as svc, pytest.raises(DecayModelError, match="user_id"):
        svc.get_decay_config("", "t1", "general")


def test_get_decay_config_rejects_oversized_tenant():
    with _patched_service(_make_test_db()) as svc, pytest.raises(DecayModelError, match="tenant_id"):
        svc.get_decay_config("u1", "x" * 100, "general")


# ---------------------------------------------------------------------------
# set_decay_config
# ---------------------------------------------------------------------------


def test_set_decay_config_upserts_row():
    db = _make_test_db()
    with _patched_service(db) as svc:
        cfg = svc.set_decay_config(
            user_id="u1",
            tenant_id="default",
            category="general",
            half_life_hours=200.0,
            min_importance=0.05,
        )
        assert cfg.half_life_hours == 200.0
        assert cfg.min_importance == 0.05
        fetched = svc.get_decay_config("u1", "default", "general")
    assert fetched.half_life_hours == 200.0
    assert fetched.min_importance == 0.05
    assert fetched.activation_boost == DEFAULT_ACTIVATION_BOOST


def test_set_decay_config_validates_half_life_positive():
    with _patched_service(_make_test_db()) as svc, pytest.raises(DecayModelError, match="half_life_hours"):
        svc.set_decay_config("u1", "default", "general", half_life_hours=0)


def test_set_decay_config_validates_min_importance_range():
    with _patched_service(_make_test_db()) as svc, pytest.raises(DecayModelError, match="min_importance"):
        svc.set_decay_config("u1", "default", "general", min_importance=1.5)


def test_set_decay_config_validates_activation_boost_range():
    with _patched_service(_make_test_db()) as svc, pytest.raises(DecayModelError, match="activation_boost"):
        svc.set_decay_config("u1", "default", "general", activation_boost=20.0)


def test_set_decay_config_validates_strengthening_threshold():
    with _patched_service(_make_test_db()) as svc, pytest.raises(DecayModelError, match="strengthening_threshold"):
        svc.set_decay_config("u1", "default", "general", strengthening_threshold=-1)


def test_set_decay_config_validates_stale_threshold():
    with _patched_service(_make_test_db()) as svc, pytest.raises(DecayModelError, match="stale_threshold"):
        svc.set_decay_config("u1", "default", "general", stale_threshold=-0.1)


# ---------------------------------------------------------------------------
# get_memory_strength
# ---------------------------------------------------------------------------


def test_get_memory_strength_returns_none_for_missing():
    with _patched_service(_make_test_db()) as svc:
        assert svc.get_memory_strength("nope", "u1", "default") is None


def test_get_memory_strength_round_trip():
    db = _make_test_db()
    mid = _insert_memory(
        db,
        user_id="u1",
        importance=0.7,
        current_strength=0.5,
        activation_count=3,
        category="general",
    )
    with _patched_service(db) as svc:
        result = svc.get_memory_strength(mid, "u1", "default")
    assert result is not None
    assert result["memory_id"] == mid
    assert result["importance"] == 0.7
    assert result["current_strength"] == 0.5
    assert result["activation_count"] == 3
    assert result["category"] == "general"


# ---------------------------------------------------------------------------
# reinforce_memory
# ---------------------------------------------------------------------------


def test_reinforce_memory_returns_none_for_missing():
    with _patched_service(_make_test_db()) as svc:
        assert svc.reinforce_memory("nope", "u1", "default") is None


def test_reinforce_memory_boosts_strength():
    db = _make_test_db()
    mid = _insert_memory(
        db,
        user_id="u1",
        importance=0.8,
        current_strength=0.4,
        activation_count=0,
    )
    with _patched_service(db) as svc:
        result = svc.reinforce_memory(mid, "u1", "default")
    assert result is not None
    assert result["old_strength"] == 0.4
    assert result["current_strength"] == pytest.approx(0.4 * 1.2)
    assert result["activation_count"] == 1
    assert result["strength_trend"] == "stable"


def test_reinforce_memory_clamps_to_one():
    db = _make_test_db()
    mid = _insert_memory(
        db,
        importance=0.95,
        current_strength=0.95,
        activation_count=0,
    )
    with _patched_service(db) as svc:
        result = svc.reinforce_memory(mid, "u1", "default")
    assert result["current_strength"] == 1.0


def test_reinforce_memory_uses_explicit_boost():
    db = _make_test_db()
    mid = _insert_memory(
        db,
        importance=0.5,
        current_strength=0.5,
        activation_count=0,
    )
    with _patched_service(db) as svc:
        result = svc.reinforce_memory(mid, "u1", "default", activation_boost=2.0)
    assert result["current_strength"] == pytest.approx(1.0)


def test_reinforce_memory_respects_min_importance():
    db = _make_test_db()
    mid = _insert_memory(
        db,
        importance=0.05,
        current_strength=0.05,
        activation_count=0,
    )
    with _patched_service(db) as svc:
        result = svc.reinforce_memory(mid, "u1", "default", activation_boost=0.1)
    assert result["current_strength"] >= 0.1


def test_reinforce_memory_sets_trend_to_strengthening_at_threshold():
    db = _make_test_db()
    mid = _insert_memory(
        db,
        importance=0.8,
        current_strength=0.8,
        activation_count=4,
    )
    with _patched_service(db) as svc:
        result = svc.reinforce_memory(mid, "u1", "default")
    assert result["strength_trend"] == "strengthening"


# ---------------------------------------------------------------------------
# apply_decay_batch
# ---------------------------------------------------------------------------


def test_apply_decay_batch_decays_old_memory():
    db = _make_test_db()
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=168)).isoformat()
    mid = _insert_memory(
        db,
        importance=0.5,
        current_strength=0.5,
        last_decay_at=old_ts,
    )
    with _patched_service(db) as svc:
        stats = svc.apply_decay_batch("u1", "default")
    assert stats.processed == 1
    assert stats.updated == 1
    with _patched_service(db) as svc2:
        result = svc2.get_memory_strength(mid, "u1", "default")
    assert result["current_strength"] == pytest.approx(0.25, abs=1e-3)


def test_apply_decay_batch_skips_fresh_memory():
    db = _make_test_db()
    fresh_ts = datetime.now(timezone.utc).isoformat()
    _insert_memory(
        db,
        importance=0.5,
        current_strength=0.5,
        last_decay_at=fresh_ts,
    )
    with _patched_service(db) as svc:
        stats = svc.apply_decay_batch("u1", "default")
    assert stats.processed == 1
    assert stats.skipped == 1
    assert stats.updated == 0


def test_apply_decay_batch_skips_memory_never_decayed():
    db = _make_test_db()
    _insert_memory(db, importance=0.5, current_strength=None, last_decay_at=None)
    with _patched_service(db) as svc:
        stats = svc.apply_decay_batch("u1", "default")
    assert stats.processed == 1
    assert stats.skipped == 1


def test_apply_decay_batch_respects_batch_size():
    db = _make_test_db()
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=168)).isoformat()
    for _ in range(5):
        _insert_memory(db, importance=0.5, current_strength=0.5, last_decay_at=old_ts)
    with _patched_service(db) as svc:
        stats = svc.apply_decay_batch("u1", "default", batch_size=2)
    assert stats.updated == 5


def test_apply_decay_batch_is_tenant_isolated():
    db = _make_test_db()
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=168)).isoformat()
    _insert_memory(db, user_id="u1", tenant_id="tA", importance=0.5, current_strength=0.5, last_decay_at=old_ts)
    _insert_memory(db, user_id="u1", tenant_id="tB", importance=0.5, current_strength=0.5, last_decay_at=old_ts)
    with _patched_service(db) as svc:
        stats = svc.apply_decay_batch("u1", "tA")
    assert stats.updated == 1


def test_apply_decay_batch_is_user_isolated():
    db = _make_test_db()
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=168)).isoformat()
    _insert_memory(db, user_id="alice", importance=0.5, current_strength=0.5, last_decay_at=old_ts)
    _insert_memory(db, user_id="bob", importance=0.5, current_strength=0.5, last_decay_at=old_ts)
    with _patched_service(db) as svc:
        stats = svc.apply_decay_batch("alice", "default")
    assert stats.updated == 1


def test_apply_decay_batch_rejects_zero_batch_size():
    with _patched_service(_make_test_db()) as svc, pytest.raises(DecayModelError, match="batch_size"):
        svc.apply_decay_batch("u1", "default", batch_size=0)


def test_apply_decay_batch_floors_at_min_importance():
    db = _make_test_db()
    very_old = (datetime.now(timezone.utc) - timedelta(hours=10000)).isoformat()
    mid = _insert_memory(
        db,
        importance=0.5,
        current_strength=0.001,
        last_decay_at=very_old,
    )
    with _patched_service(db) as svc:
        svc.apply_decay_batch("u1", "default")
    with _patched_service(db) as svc2:
        result = svc2.get_memory_strength(mid, "u1", "default")
    assert result["current_strength"] >= 0.1


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def test_get_decay_events_returns_empty_for_no_events():
    with _patched_service(_make_test_db()) as svc:
        events = svc.get_decay_events("u1", "default")
    assert events == []


def test_decay_batch_records_events():
    db = _make_test_db()
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=168)).isoformat()
    mid = _insert_memory(
        db,
        importance=0.5,
        current_strength=0.5,
        last_decay_at=old_ts,
    )
    with _patched_service(db) as svc:
        svc.apply_decay_batch("u1", "default")
        events = svc.get_decay_events("u1", "default", memory_id=mid)
    assert len(events) == 1
    e = events[0]
    assert e.event_type == "decay"
    assert e.memory_id == mid
    assert e.old_strength == pytest.approx(0.5)
    assert e.new_strength == pytest.approx(0.25, abs=1e-3)
    assert e.decay_factor == pytest.approx(0.5, abs=1e-3)


def test_reinforce_records_event():
    db = _make_test_db()
    mid = _insert_memory(
        db,
        importance=0.8,
        current_strength=0.4,
        activation_count=0,
    )
    with _patched_service(db) as svc:
        svc.reinforce_memory(mid, "u1", "default")
        events = svc.get_decay_events("u1", "default", memory_id=mid)
    assert len(events) == 1
    assert events[0].event_type == "reinforce"
    assert events[0].old_strength == 0.4
    assert events[0].new_strength == pytest.approx(0.48)


def test_get_decay_events_filters_by_memory():
    db = _make_test_db()
    mid1 = _insert_memory(
        db, importance=0.5, current_strength=0.5, last_decay_at=datetime.now(timezone.utc).isoformat()
    )
    mid2 = _insert_memory(
        db, importance=0.5, current_strength=0.5, last_decay_at=datetime.now(timezone.utc).isoformat()
    )
    with _patched_service(db) as svc:
        svc.reinforce_memory(mid1, "u1", "default")
        svc.reinforce_memory(mid2, "u1", "default")
        only_mid1 = svc.get_decay_events("u1", "default", memory_id=mid1)
    assert len(only_mid1) == 1
    assert only_mid1[0].memory_id == mid1


def test_get_decay_events_limit_caps_results():
    db = _make_test_db()
    mid = _insert_memory(db, importance=0.5, current_strength=0.5, last_decay_at=datetime.now(timezone.utc).isoformat())
    with _patched_service(db) as svc:
        for _ in range(5):
            svc.reinforce_memory(mid, "u1", "default")
        events = svc.get_decay_events("u1", "default", limit=2)
    assert len(events) == 2


def test_get_decay_events_rejects_zero_limit():
    with _patched_service(_make_test_db()) as svc, pytest.raises(DecayModelError, match="limit"):
        svc.get_decay_events("u1", "default", limit=0)


# ---------------------------------------------------------------------------
# StrengthEvent to_dict
# ---------------------------------------------------------------------------


def test_strength_event_to_dict_shape():
    e = StrengthEvent(
        id=1,
        tenant_id="t",
        user_id="u",
        memory_id="m",
        event_type="decay",
        old_strength=0.5,
        new_strength=0.25,
        decay_factor=0.5,
        reason="test",
        created_at="2024-01-01T00:00:00",
    )
    out = e.to_dict()
    assert out["id"] == 1
    assert out["event_type"] == "decay"
    assert out["old_strength"] == 0.5


# ---------------------------------------------------------------------------
# Tenant + user isolation
# ---------------------------------------------------------------------------


def test_reinforce_memory_is_tenant_isolated():
    db = _make_test_db()
    mid = _insert_memory(
        db,
        user_id="u1",
        tenant_id="tA",
        importance=0.8,
        current_strength=0.4,
    )
    with _patched_service(db) as svc:
        result = svc.reinforce_memory(mid, "u1", "tB")
    assert result is None
    with _patched_service(db) as svc2:
        untouched = svc2.get_memory_strength(mid, "u1", "tA")
    assert untouched["activation_count"] == 0


def test_get_memory_strength_is_user_isolated():
    db = _make_test_db()
    mid = _insert_memory(
        db,
        user_id="alice",
        importance=0.5,
        current_strength=0.5,
    )
    with _patched_service(db) as svc:
        assert svc.get_memory_strength(mid, "bob", "default") is None


def test_decay_config_is_tenant_scoped():
    db = _make_test_db()
    with _patched_service(db) as svc:
        svc.set_decay_config(
            user_id="u1",
            tenant_id="tA",
            category="general",
            half_life_hours=100.0,
        )
        cfg_a = svc.get_decay_config("u1", "tA", "general")
        cfg_b = svc.get_decay_config("u1", "tB", "general")
    assert cfg_a.half_life_hours == 100.0
    assert cfg_b.half_life_hours == DEFAULT_HALF_LIFE_HOURS


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


def test_singleton_returns_same_instance():
    db = _make_test_db()
    with (
        patch.object(mod, "DB_PATH", db),
        patch("foresight_mcp.config.DB_PATH", db),
        patch("foresight_mcp.decay_model.DB_PATH", db),
    ):
        reset_decay_model()
        a = get_decay_model()
        b = get_decay_model()
        assert a is b
        reset_decay_model()
        c = get_decay_model()
        assert c is not a


def test_singleton_thread_safe():
    db = _make_test_db()
    with (
        patch.object(mod, "DB_PATH", db),
        patch("foresight_mcp.config.DB_PATH", db),
        patch("foresight_mcp.decay_model.DB_PATH", db),
    ):
        reset_decay_model()
        results = []

        def _grab():
            results.append(get_decay_model())

        threads = [threading.Thread(target=_grab) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len({id(s) for s in results}) == 1


# ---------------------------------------------------------------------------
# End-to-end: reinforce + decay round trip
# ---------------------------------------------------------------------------


def test_reinforce_then_decay_drops_strength():
    db = _make_test_db()
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=1000)).isoformat()
    mid = _insert_memory(
        db,
        importance=0.5,
        current_strength=0.5,
        last_decay_at=old_ts,
        activation_count=0,
    )
    with _patched_service(db) as svc:
        svc.reinforce_memory(mid, "u1", "default")
        result = svc.reinforce_memory(mid, "u1", "default")
    assert result["current_strength"] == pytest.approx(0.72, abs=1e-3)
    assert result["activation_count"] == 2

    even_older = (datetime.now(timezone.utc) - timedelta(hours=2000)).isoformat()
    with _patched_service(db) as svc:
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE memories SET last_decay_at = ? WHERE id = ?",
            (even_older, mid),
        )
        conn.commit()
        conn.close()
        svc.apply_decay_batch("u1", "default")

    with _patched_service(db) as svc:
        final = svc.get_memory_strength(mid, "u1", "default")
    assert final["current_strength"] < 0.72
