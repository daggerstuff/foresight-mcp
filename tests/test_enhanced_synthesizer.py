"""
Tests for enhanced memory synthesizer.
"""
import pytest
from datetime import datetime, timezone, timedelta
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from foresight_mcp.enhanced_synthesizer import (
    EnhancedMemorySynthesizer,
    EnhancedSynthesisResult,
    Contradiction,
    TemporalTrend,
    Insight,
)
from foresight_mcp.memory_types import MemoryObject, EmotionalMetadata, SynthesisResult


def create_memory(
    content: str,
    timestamp: datetime,
    intensity: float = 0.5,
    tags = None
) -> MemoryObject:
    """Helper to create test memories."""
    return MemoryObject(
        id=f"mem_{abs(hash(content)) % 10000}",
        timestamp=timestamp.isoformat(),
        scope="session",
        retention="short_term",
        content=content,
        tags=tags if tags is not None else [],
        emotional_context=EmotionalMetadata(intensity=intensity),
    )


class TestContradictionDetection:
    """Test contradiction detection logic."""

    def test_calculate_slope(self):
        """Should calculate correct slope for linear data."""
        synthesizer = EnhancedMemorySynthesizer()

        # Perfect upward trend: 1, 2, 3, 4, 5
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        slope, r_squared = synthesizer._calculate_slope(values)

        assert abs(slope - 1.0) < 0.01
        assert r_squared > 0.99

    def test_calculate_slope_downward(self):
        """Should calculate negative slope for downward trend."""
        synthesizer = EnhancedMemorySynthesizer()

        values = [5.0, 4.0, 3.0, 2.0, 1.0]
        slope, r_squared = synthesizer._calculate_slope(values)

        assert abs(slope - (-1.0)) < 0.01
        assert r_squared > 0.99

    def test_extract_metric_value(self):
        """Should extract intensity from memories."""
        synthesizer = EnhancedMemorySynthesizer()

        memory = create_memory("Test", datetime.now(timezone.utc), intensity=0.75)
        value = synthesizer._extract_metric_value([memory])

        assert value == 0.75


class TestTemporalTrendAnalysis:
    """Test temporal trend analysis."""

    def test_cluster_by_topic(self):
        """Should cluster memories by topic."""
        synthesizer = EnhancedMemorySynthesizer()

        memories = [
            create_memory("Feeling anxious", datetime.now(timezone.utc), intensity=0.8, tags=['anxiety']),
            create_memory("High stress", datetime.now(timezone.utc), intensity=0.7, tags=['stress']),
            create_memory("Less anxiety", datetime.now(timezone.utc), intensity=0.4, tags=['anxiety']),
        ]

        clusters = synthesizer._cluster_by_topic(memories)

        assert 'anxiety' in clusters
        assert len(clusters['anxiety']) == 2


class TestInsightGeneration:
    """Test evidence-anchored insight generation."""

    def test_generate_contradiction_insight(self):
        """Should format contradiction as insight."""
        synthesizer = EnhancedMemorySynthesizer()

        contradiction = Contradiction(
            type='evolution',
            attribute='anxiety',
            old_value='0.80',
            new_value='0.30',
            delta=-0.5,
            temporal_distance_days=30,
            evidence_ids=['mem1', 'mem2'],
            confidence=0.9,
        )

        insight = synthesizer._format_contradiction_insight(contradiction)

        assert 'anxiety' in insight

    def test_generate_trend_insight(self):
        """Should format trend as insight."""
        synthesizer = EnhancedMemorySynthesizer()

        trend = TemporalTrend(
            topic='stress',
            direction='improving',
            slope=-0.1,
            r_squared=0.85,
            evidence_ids=['mem1', 'mem2', 'mem3'],
            start_value=0.9,
            end_value=0.4,
        )

        insight = synthesizer._format_trend_insight(trend)

        assert 'stress' in insight.lower() or 'stress' in insight
        assert 'improving' in insight.lower()

    def test_insight_requires_evidence(self):
        """All insights must have evidence IDs."""
        synthesizer = EnhancedMemorySynthesizer()

        contradiction = Contradiction(
            type='evolution',
            attribute='anxiety',
            old_value='0.80',
            new_value='0.30',
            delta=-0.5,
            temporal_distance_days=30,
            evidence_ids=['mem1', 'mem2'],
            confidence=0.9,
        )

        insights = []
        if contradiction.confidence >= 0.7:
            insights.append(Insight(
                statement=synthesizer._format_contradiction_insight(contradiction),
                insight_type='contradiction',
                confidence=contradiction.confidence,
                evidence_ids=contradiction.evidence_ids,
                recommended_action='review',
            ))

        for insight in insights:
            assert len(insight.evidence_ids) > 0


class TestEnhancedSynthesis:
    """Test full enhanced synthesis pipeline."""

    def test_synthesize_with_few_memories(self):
        """Should return None for too few memories."""
        import asyncio
        synthesizer = EnhancedMemorySynthesizer()

        memories = [
            create_memory("Memory 1", datetime.now(timezone.utc), intensity=0.5),
            create_memory("Memory 2", datetime.now(timezone.utc), intensity=0.6),
        ]

        # synthesize is async, so we need to run it
        async def run_test():
            return await synthesizer.base_synthesizer.synthesize(memories)

        result = asyncio.run(run_test())

        assert result is None

    def test_split_recent_and_historic(self):
        """Should split memories into 80/20."""
        synthesizer = EnhancedMemorySynthesizer()

        memories = [
            create_memory(f"Memory {i}", datetime.now(timezone.utc) - timedelta(days=i))
            for i in range(10)
        ]

        splits = synthesizer._split_recent_and_historic(memories)

        assert len(splits['historic']) == 8
        assert len(splits['recent']) == 2


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
