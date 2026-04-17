"""
Tests for temporal memory service.

Tests:
- Decay calculations
- Freshness trend tracking
- Temporal queries
- Batch decay updates
"""
import pytest
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add parent directory to path for imports
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from foresight_mcp.temporal_service import TemporalService, DecayConfig
from foresight_mcp.temporal_queries import TemporalQueryBuilder
from foresight_mcp.temporal_schema import run_temporal_migrations


def create_minimal_schema(db_path: str) -> None:
    """Create minimal memories table schema for testing."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            importance REAL DEFAULT 1.0,
            category TEXT DEFAULT 'general'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS decay_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            half_life_hours REAL DEFAULT 168.0,
            min_importance REAL DEFAULT 0.1,
            activation_boost REAL DEFAULT 1.2,
            strengthening_threshold INTEGER DEFAULT 5,
            stale_threshold REAL DEFAULT 0.2,
            UNIQUE(user_id, category)
        )
    """)
    conn.commit()
    conn.close()


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    fd, path = tempfile.mkstemp(suffix='.db')
    # Create minimal schema first
    create_minimal_schema(path)
    yield path
    import os
    os.close(fd)
    os.unlink(path)


@pytest.fixture
def temporal_service(temp_db):
    """Create temporal service with migrated database."""
    run_temporal_migrations(temp_db)
    return TemporalService(temp_db)


@pytest.fixture
def query_builder(temp_db):
    """Create query builder with migrated database."""
    run_temporal_migrations(temp_db)
    return TemporalQueryBuilder(temp_db)


class TestDecayCalculations:
    """Test decay calculation logic."""

    def test_no_decay_for_new_memory(self, temporal_service):
        """New memories should have full importance."""
        now = datetime.now(timezone.utc).isoformat()
        importance, trend = temporal_service.calculate_decay(
            importance=1.0,
            created_at=now,
            activation_count=0,
            category='general',
            user_id='test'
        )
        assert importance >= 0.95  # Allow small floating point variance
        assert trend == 'stable'

    def test_exponential_decay_after_one_half_life(self, temporal_service):
        """After one half-life (168 hours), importance should be ~50%."""
        one_week_ago = (datetime.now(timezone.utc) - timedelta(hours=168)).isoformat()
        importance, trend = temporal_service.calculate_decay(
            importance=1.0,
            created_at=one_week_ago,
            activation_count=0,
            category='general',
            user_id='test'
        )
        assert 0.45 <= importance <= 0.55  # ~50% with variance

    def test_category_multiplier_affects_decay(self, temporal_service):
        """Preferences should decay slower than conversations."""
        two_weeks_ago = (datetime.now(timezone.utc) - timedelta(hours=336)).isoformat()

        # Preference (2x half-life = 2 weeks)
        pref_importance, _ = temporal_service.calculate_decay(
            importance=1.0,
            created_at=two_weeks_ago,
            activation_count=0,
            category='preference',
            user_id='test'
        )

        # Conversation (0.5x half-life = 3.5 days)
        conv_importance, _ = temporal_service.calculate_decay(
            importance=1.0,
            created_at=two_weeks_ago,
            activation_count=0,
            category='conversation',
            user_id='test'
        )

        # Preference should have higher importance
        assert pref_importance > conv_importance

    def test_stale_threshold(self, temporal_service):
        """Very old memories should be marked as stale."""
        one_month_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        importance, trend = temporal_service.calculate_decay(
            importance=1.0,
            created_at=one_month_ago,
            activation_count=0,
            category='general',
            user_id='test'
        )
        assert trend == 'stale'
        assert importance <= 0.2  # Below stale threshold


class TestFreshnessTrends:
    """Test freshness trend calculation."""

    def test_strengthening_with_activations(self, temporal_service):
        """Frequent activations should result in strengthening trend."""
        now = datetime.now(timezone.utc).isoformat()
        importance, trend = temporal_service.calculate_decay(
            importance=0.8,
            created_at=now,
            activation_count=10,  # Above threshold of 5
            category='general',
            user_id='test'
        )
        assert trend == 'strengthening'

    def test_stable_for_normal_decay(self, temporal_service):
        """Recent memories with some activations should be stable."""
        one_day_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        importance, trend = temporal_service.calculate_decay(
            importance=0.9,
            created_at=one_day_ago,
            activation_count=2,
            category='general',
            user_id='test'
        )
        assert trend == 'stable'


class TestTemporalQueries:
    """Test temporal query patterns."""

    def test_get_memories_from_window(self, query_builder, temp_db):
        """Query memories from time window."""
        conn = sqlite3.connect(temp_db)

        # Insert test memories
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            INSERT INTO memories (id, user_id, content, created_at, importance)
            VALUES (?, ?, ?, ?, ?)
        """, ('mem1', 'test', 'Test memory', now, 0.8))

        conn.commit()
        conn.close()

        results = query_builder.get_memories_from_window(
            user_id='test',
            window='today'
        )

        assert len(results) == 1
        assert results[0].memory_id == 'mem1'

    def test_analyze_trends(self, query_builder, temp_db):
        """Get trend analysis for user."""
        conn = sqlite3.connect(temp_db)

        # Insert test memories
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            INSERT INTO memories (id, user_id, content, created_at, importance, category)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ('mem1', 'test', 'Test memory', now, 0.8, 'preference'))

        conn.commit()
        conn.close()

        analysis = query_builder.analyze_trends(user_id='test', timeframe='30 days')

        assert 'daily_stats' in analysis
        assert 'category_breakdown' in analysis
        assert 'overall_trend' in analysis


class TestBatchDecayUpdate:
    """Test batch decay update functionality."""

    def test_batch_update(self, temporal_service, temp_db):
        """Batch update should process all memories."""
        conn = sqlite3.connect(temp_db)

        # Insert test memories
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            INSERT INTO memories (id, user_id, content, created_at, importance)
            VALUES (?, ?, ?, ?, ?)
        """, ('mem1', 'test', 'Test 1', now, 0.8))
        conn.execute("""
            INSERT INTO memories (id, user_id, content, created_at, importance)
            VALUES (?, ?, ?, ?, ?)
        """, ('mem2', 'test', 'Test 2', now, 0.6))

        conn.commit()
        conn.close()

        count = temporal_service.batch_update_decay(user_id='test')

        assert count == 2


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
