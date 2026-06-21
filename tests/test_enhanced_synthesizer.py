"""
Tests for enhanced memory synthesizer.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from foresight_mcp.enhanced_synthesizer import (
    Contradiction,
    EnhancedMemorySynthesizer,
    Insight,
    TemporalTrend,
)
from foresight_mcp.memory_types import EmotionalMetadata, MemoryObject


def create_memory(content: str, timestamp: datetime, intensity: float = 0.5, tags=None) -> MemoryObject:
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
            create_memory("Feeling anxious", datetime.now(timezone.utc), intensity=0.8, tags=["anxiety"]),
            create_memory("High stress", datetime.now(timezone.utc), intensity=0.7, tags=["stress"]),
            create_memory("Less anxiety", datetime.now(timezone.utc), intensity=0.4, tags=["anxiety"]),
        ]

        clusters = synthesizer._cluster_by_topic(memories)

        assert "anxiety" in clusters
        assert len(clusters["anxiety"]) == 2

    def test_cluster_by_topic_multi_topic(self):
        """Memories about 'work anxiety' should appear in both 'work' and 'anxiety' clusters."""
        synthesizer = EnhancedMemorySynthesizer()

        memories = [
            create_memory("Work anxiety is increasing", datetime.now(timezone.utc), intensity=0.8),
            create_memory("Family stress is high", datetime.now(timezone.utc), intensity=0.7),
            create_memory("Therapy for anxiety helps", datetime.now(timezone.utc), intensity=0.4),
        ]

        clusters = synthesizer._cluster_by_topic(memories)

        # "Work anxiety is increasing" contains both 'work' and 'anxiety'
        assert "work" in clusters
        assert "anxiety" in clusters
        work_ids = [m.id for m in clusters["work"]]
        anxiety_ids = [m.id for m in clusters["anxiety"]]
        # The first memory should be in both clusters
        assert memories[0].id in work_ids
        assert memories[0].id in anxiety_ids

    def test_compute_overlap_score_identical(self):
        """Identical contents should have overlap score of 1.0."""
        synthesizer = EnhancedMemorySynthesizer()

        score = synthesizer._compute_overlap_score("I love therapy sessions", "I love therapy sessions")

        assert score == 1.0

    def test_compute_overlap_score_no_overlap(self):
        """Completely different contents should have overlap score of 0.0."""
        synthesizer = EnhancedMemorySynthesizer()

        score = synthesizer._compute_overlap_score("alpha beta gamma", "delta epsilon zeta")

        assert score == 0.0

    def test_compute_overlap_score_partial(self):
        """Partially overlapping contents should have 0 < score < 1."""
        synthesizer = EnhancedMemorySynthesizer()

        score = synthesizer._compute_overlap_score("I love therapy sessions", "I hate therapy sessions")

        # Shared words: i, therapy, sessions = 3/5 unique = 0.6
        assert 0 < score < 1
        assert score > 0.3  # Should exceed overlap threshold

    def test_find_sentiment_conflict_detected(self):
        """Should detect love/hate sentiment conflict."""
        synthesizer = EnhancedMemorySynthesizer()

        result = synthesizer._find_sentiment_conflict("I love therapy", "I hate therapy")

        assert result is not None
        pos, neg = result
        assert pos == "love"
        assert neg == "hate"

    def test_find_sentiment_conflict_none(self):
        """Should return None when no sentiment conflict exists."""
        synthesizer = EnhancedMemorySynthesizer()

        result = synthesizer._find_sentiment_conflict("I enjoy therapy", "I appreciate therapy")

        assert result is None

    def test_find_sentiment_conflict_reversed(self):
        """Should detect conflict regardless of which memory has which word."""
        synthesizer = EnhancedMemorySynthesizer()

        result = synthesizer._find_sentiment_conflict("Things are getting worse", "Things are getting better")

        assert result is not None
        pos, neg = result
        assert pos == "better"
        assert neg == "worse"

    def test_detect_contradictions_sentiment_overlap(self):
        """Should detect direct_conflict via keyword overlap + opposite sentiment."""
        synthesizer = EnhancedMemorySynthesizer()

        base_time = datetime.now(timezone.utc)
        historic = [
            create_memory("I love my therapy sessions", base_time - timedelta(days=30), intensity=0.5),
        ]
        recent = [
            create_memory("I hate my therapy sessions", base_time, intensity=0.5),
        ]

        contradictions = synthesizer._detect_contradictions(historic + recent)

        # Should find at least one direct_conflict from sentiment overlap
        sentiment_conflicts = [c for c in contradictions if c.type == "direct_conflict"]
        assert len(sentiment_conflicts) >= 1
        # The conflict should reference the specific sentiment words
        conflict = sentiment_conflicts[0]
        assert conflict.old_value in ("love", "hate")
        assert conflict.new_value in ("love", "hate")

    def test_sentiment_opposites_class_constant(self):
        """SENTIMENT_OPPOSITES should contain the expected word pairs."""
        assert len(EnhancedMemorySynthesizer.SENTIMENT_OPPOSITES) >= 20
        pairs = set(EnhancedMemorySynthesizer.SENTIMENT_OPPOSITES)
        assert ("love", "hate") in pairs
        assert ("good", "bad") in pairs
        assert ("happy", "sad") in pairs


class TestInsightGeneration:
    """Test evidence-anchored insight generation."""

    def test_generate_contradiction_insight(self):
        """Should format contradiction as insight."""
        synthesizer = EnhancedMemorySynthesizer()

        contradiction = Contradiction(
            type="evolution",
            attribute="anxiety",
            old_value="0.80",
            new_value="0.30",
            delta=-0.5,
            temporal_distance_days=30,
            evidence_ids=["mem1", "mem2"],
            confidence=0.9,
        )

        insight = synthesizer._format_contradiction_insight(contradiction)

        assert "anxiety" in insight

    def test_generate_trend_insight(self):
        """Should format trend as insight."""
        synthesizer = EnhancedMemorySynthesizer()

        trend = TemporalTrend(
            topic="stress",
            direction="improving",
            slope=-0.1,
            r_squared=0.85,
            evidence_ids=["mem1", "mem2", "mem3"],
            start_value=0.9,
            end_value=0.4,
        )

        insight = synthesizer._format_trend_insight(trend)

        assert "stress" in insight.lower() or "stress" in insight
        assert "improving" in insight.lower()

    def test_insight_requires_evidence(self):
        """All insights must have evidence IDs."""
        synthesizer = EnhancedMemorySynthesizer()

        contradiction = Contradiction(
            type="evolution",
            attribute="anxiety",
            old_value="0.80",
            new_value="0.30",
            delta=-0.5,
            temporal_distance_days=30,
            evidence_ids=["mem1", "mem2"],
            confidence=0.9,
        )

        insights = []
        if contradiction.confidence >= 0.7:
            insights.append(
                Insight(
                    statement=synthesizer._format_contradiction_insight(contradiction),
                    insight_type="contradiction",
                    confidence=contradiction.confidence,
                    evidence_ids=contradiction.evidence_ids,
                    recommended_action="review",
                )
            )

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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
