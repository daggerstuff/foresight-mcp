"""
Enhanced Memory Synthesizer with Temporal and Graph Analysis.

Extends the existing MemorySynthesizer with:
- Contradiction detection (direct conflicts, evolutions, regressions)
- Temporal trend analysis (improving/worsening/stable)
- Entity-based clustering for synthesis
- Evidence-anchored insights (prevents hallucination)
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .memory_components import MemorySynthesizer
from .memory_types import MemoryObject, StanceShift

logger = logging.getLogger("foresight_enhanced_synthesizer")


@dataclass
class Contradiction:
    """
    Represents a detected contradiction or evolution between memories.

    Types:
    - direct_conflict: Opposite statements (e.g., "I love therapy" vs "I hate therapy")
    - evolution: Gradual improvement (e.g., anxiety 8/10 -> 5/10)
    - regression: Setback (e.g., anxiety 3/10 -> 7/10)
    """

    type: str  # 'direct_conflict' | 'evolution' | 'regression'
    attribute: str  # What changed (e.g., 'anxiety_severity', 'therapy_attitude')
    old_value: str
    new_value: str
    delta: float
    temporal_distance_days: int
    evidence_ids: list[str]
    confidence: float

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "type": self.type,
            "attribute": self.attribute,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "delta": self.delta,
            "temporal_distance_days": self.temporal_distance_days,
            "evidence_ids": self.evidence_ids,
            "confidence": self.confidence,
        }


@dataclass
class TemporalTrend:
    """
    Represents a temporal trend detected across memories.

    Direction:
    - improving: Positive change over time
    - worsening: Negative change over time
    - stable: No significant change
    """

    topic: str
    direction: str  # 'improving' | 'worsening' | 'stable'
    slope: float  # Rate of change
    r_squared: float  # Goodness of fit
    evidence_ids: list[str]
    start_value: float
    end_value: float

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "topic": self.topic,
            "direction": self.direction,
            "slope": self.slope,
            "r_squared": self.r_squared,
            "evidence_ids": self.evidence_ids,
            "start_value": self.start_value,
            "end_value": self.end_value,
        }


@dataclass
class Insight:
    """
    An insight derived from memory synthesis.

    All insights must be evidence-anchored to prevent hallucination.
    """

    statement: str
    insight_type: str  # 'trend' | 'pattern' | 'contradiction' | 'breakthrough'
    confidence: float
    evidence_ids: list[str]
    recommended_action: str  # 'preserve' | 'consolidate' | 'review'
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "statement": self.statement,
            "insight_type": self.insight_type,
            "confidence": self.confidence,
            "evidence_ids": self.evidence_ids,
            "recommended_action": self.recommended_action,
            "metadata": self.metadata,
        }


@dataclass
class EnhancedSynthesisResult:
    """
    Enhanced synthesis result with temporal and graph analysis.

    Extends SynthesisResult with:
    - Contradictions detected
    - Temporal trends
    - Generated insights
    """

    merged_ids: list[str]
    new_memory_id: str
    stance_shifts: list[StanceShift]
    compression_ratio: float

    # New fields
    contradictions: list[Contradiction] = field(default_factory=list)
    temporal_trends: list[TemporalTrend] = field(default_factory=list)
    insights: list[Insight] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "merged_ids": self.merged_ids,
            "new_memory_id": self.new_memory_id,
            "stance_shifts": [ss.to_dict() for ss in self.stance_shifts],
            "compression_ratio": self.compression_ratio,
            "contradictions": [c.to_dict() for c in self.contradictions],
            "temporal_trends": [t.to_dict() for t in self.temporal_trends],
            "insights": [i.to_dict() for i in self.insights],
        }


class EnhancedMemorySynthesizer:
    """
    Enhanced memory synthesizer with temporal and graph analysis.

    Extends the base MemorySynthesizer with:
    - Contradiction detection
    - Temporal trend analysis
    - Evidence-anchored insight generation
    """

    SENTIMENT_OPPOSITES: tuple[tuple[str, str], ...] = (
        ("love", "hate"),
        ("good", "bad"),
        ("happy", "sad"),
        ("better", "worse"),
        ("helpful", "harmful"),
        ("easy", "hard"),
        ("improve", "worsen"),
        ("like", "dislike"),
        ("hope", "despair"),
        ("calm", "anxious"),
        ("confident", "doubtful"),
        ("safe", "afraid"),
        ("trust", "distrust"),
        ("accept", "reject"),
        ("satisfied", "frustrated"),
        ("optimistic", "pessimistic"),
        ("grateful", "resentful"),
        ("comfortable", "uncomfortable"),
        ("peaceful", "distressed"),
        ("motivated", "discouraged"),
        ("supported", "abandoned"),
        ("connected", "isolated"),
        ("valued", "worthless"),
        ("strong", "weak"),
        ("progress", "regress"),
        ("healing", "hurting"),
        ("joy", "sorrow"),
    )

    def __init__(
        self,
        base_synthesizer: MemorySynthesizer | None = None,
        contradiction_threshold: float = 0.25,
        trend_significance_threshold: float = 0.15,
        min_memories_for_trend: int = 5,
        overlap_threshold: float = 0.3,
    ):
        """
        Initialize enhanced synthesizer.

        Args:
            base_synthesizer: Base synthesizer to extend (uses default if None)
            contradiction_threshold: Minimum delta for contradiction detection
            trend_significance_threshold: Minimum slope for significant trend
            min_memories_for_trend: Minimum memories needed for trend analysis
        """
        self.base_synthesizer = base_synthesizer or MemorySynthesizer()
        self.contradiction_threshold = contradiction_threshold
        self.trend_significance_threshold = trend_significance_threshold
        self.min_memories_for_trend = min_memories_for_trend
        self.overlap_threshold = overlap_threshold

    async def synthesize(
        self,
        memories: list[MemoryObject],
        user_id: str = "default",
    ) -> EnhancedSynthesisResult | None:
        """
        Perform enhanced synthesis over memories.

        Args:
            memories: List of memories to synthesize
            user_id: User ID for context

        Returns:
            EnhancedSynthesisResult or None if not enough data
        """
        logger.debug("Synthesizing for user: %s", user_id)
        if len(memories) < 5:
            return None

        try:
            # Run base synthesis
            base_result = await self.base_synthesizer.synthesize(memories)
            if base_result is None:
                return None

            # Detect contradictions
            contradictions = self._detect_contradictions(memories)

            # Analyze temporal trends
            temporal_trends = self._analyze_temporal_trends(memories)

            # Generate insights (evidence-anchored)
            insights = self._generate_insights(memories, contradictions, temporal_trends)

            return EnhancedSynthesisResult(
                merged_ids=base_result.merged_ids,
                new_memory_id=base_result.new_memory_id,
                stance_shifts=base_result.stance_shifts,
                compression_ratio=base_result.compression_ratio,
                contradictions=contradictions,
                temporal_trends=temporal_trends,
                insights=insights,
            )

        except Exception as e:
            logger.error(f"Enhanced synthesis failed: {e}")
            return None

    def _detect_contradictions(self, memories: list[MemoryObject]) -> list[Contradiction]:
        """
        Detect contradictions between memories using content overlap + sentiment.

        Finds pairs of memories with high content overlap but opposing sentiment
        words (e.g., "happy" vs "sad" in same topic).
        """
        contradictions: list[Contradiction] = []
        seen_pairs: set = set()

        topic_clusters = self._cluster_by_topic(memories)

        for topic, cluster in topic_clusters.items():
            if len(cluster) < 2:
                continue

            # Keyword overlap contradiction detection
            for i, mem_a in enumerate(cluster):
                for mem_b in cluster[i + 1 :]:
                    pair_key = tuple(sorted([mem_a.id, mem_b.id]))
                    if pair_key in seen_pairs:
                        continue

                    overlap = self._compute_overlap_score(mem_a.content, mem_b.content)
                    if overlap <= self.overlap_threshold:
                        continue

                    # Check for opposite sentiment words
                    conflicting_pair = self._find_sentiment_conflict(mem_a.content, mem_b.content)
                    if conflicting_pair is not None:
                        seen_pairs.add(pair_key)
                        pos_word, neg_word = conflicting_pair

                        # Calculate temporal distance
                        time_a = datetime.fromisoformat(mem_a.timestamp.replace("Z", "+00:00"))
                        time_b = datetime.fromisoformat(mem_b.timestamp.replace("Z", "+00:00"))
                        days_diff = abs((time_b - time_a).days)

                        contradictions.append(
                            Contradiction(
                                type="direct_conflict",
                                attribute=topic,
                                old_value=pos_word,
                                new_value=neg_word,
                                delta=-(overlap),
                                temporal_distance_days=days_diff,
                                evidence_ids=[mem_a.id, mem_b.id],
                                confidence=min(overlap * 1.5, 1.0),
                            )
                        )

        return contradictions

    def _analyze_temporal_trends(self, memories: list[MemoryObject]) -> list[TemporalTrend]:
        """
        Analyze temporal trends in memories.

        Identifies improving/worsening/stable patterns over time.
        """
        trends: list[TemporalTrend] = []

        # Group by topic
        topic_clusters = self._cluster_by_topic(memories)

        for topic, cluster in topic_clusters.items():
            if len(cluster) < self.min_memories_for_trend:
                continue

            # Sort by timestamp
            sorted_cluster = sorted(
                cluster, key=lambda m: datetime.fromisoformat(m.timestamp.replace("Z", "+00:00")).timestamp()
            )

            # Extract values and calculate slope
            values = [self._extract_metric_value([m]) for m in sorted_cluster]
            slope, r_squared = self._calculate_slope(values)

            if abs(slope) > self.trend_significance_threshold:
                direction = "improving" if slope > 0 else "worsening"

                trends.append(
                    TemporalTrend(
                        topic=topic,
                        direction=direction,
                        slope=slope,
                        r_squared=r_squared,
                        evidence_ids=[m.id for m in sorted_cluster],
                        start_value=values[0],
                        end_value=values[-1],
                    )
                )

        return trends

    def _generate_insights(
        self, _memories: list[MemoryObject], contradictions: list[Contradiction], temporal_trends: list[TemporalTrend]
    ) -> list[Insight]:
        """
        Generate evidence-anchored insights.

        All insights must cite source memory IDs to prevent hallucination.
        """
        insights: list[Insight] = []

        # Generate insights from contradictions
        for contradiction in contradictions:
            if contradiction.confidence >= 0.7:
                insights.append(
                    Insight(
                        statement=self._format_contradiction_insight(contradiction),
                        insight_type="contradiction",
                        confidence=contradiction.confidence,
                        evidence_ids=contradiction.evidence_ids,
                        recommended_action="review",
                        metadata={"contradiction_type": contradiction.type},
                    )
                )

        # Generate insights from trends
        for trend in temporal_trends:
            if trend.r_squared >= 0.5:  # Good fit
                insights.append(
                    Insight(
                        statement=self._format_trend_insight(trend),
                        insight_type="trend",
                        confidence=trend.r_squared,
                        evidence_ids=trend.evidence_ids,
                        recommended_action="preserve" if trend.direction == "improving" else "review",
                        metadata={
                            "slope": trend.slope,
                            "start_value": trend.start_value,
                            "end_value": trend.end_value,
                        },
                    )
                )

        return insights

    def _cluster_by_topic(self, memories: list[MemoryObject]) -> dict[str, list[MemoryObject]]:
        """
        Cluster memories by topic.

        Simplified clustering based on tags and content keywords.
        In production, this would use semantic similarity.
        """
        clusters: dict[str, list[MemoryObject]] = {}

        # Common topics to look for
        topics = [
            "anxiety",
            "stress",
            "therapy",
            "mood",
            "sleep",
            "work",
            "family",
            "health",
            "coping",
            "progress",
            "challenge",
        ]

        for memory in memories:
            content_lower = memory.content.lower()
            tags_lower = [t.lower() for t in memory.tags]

            for topic in topics:
                if re.search(rf"\b{re.escape(topic)}\b", content_lower) or topic in tags_lower:
                    if topic not in clusters:
                        clusters[topic] = []
                    clusters[topic].append(memory)

        return clusters

    def _compute_overlap_score(self, content_a: str, content_b: str) -> float:
        """
        Compute keyword overlap (Jaccard similarity) between two memory contents.

        Tokenizes both contents into word sets and returns the Jaccard index:
        |A intersection B| / |A union B|

        This gives content-based similarity without requiring embeddings.
        """
        words_a = set(re.findall(r"\b\w+\b", content_a.lower()))
        words_b = set(re.findall(r"\b\w+\b", content_b.lower()))

        if not words_a or not words_b:
            return 0.0

        intersection = words_a & words_b
        union = words_a | words_b

        return len(intersection) / len(union)

    def _find_sentiment_conflict(self, content_a: str, content_b: str) -> tuple[str, str] | None:
        """
        Check if two contents contain opposite sentiment words.

        Returns a tuple of (positive_word, negative_word) if a conflicting
        pair is found, or None otherwise.
        """
        words_a = set(re.findall(r"\b\w+\b", content_a.lower()))
        words_b = set(re.findall(r"\b\w+\b", content_b.lower()))

        for pos_word, neg_word in self.SENTIMENT_OPPOSITES:
            if (pos_word in words_a and neg_word in words_b) or (neg_word in words_a and pos_word in words_b):
                return (pos_word, neg_word)

        return None

    def _extract_metric_value(self, memories: list[MemoryObject]) -> float:
        """
        Extract a numeric metric value from memories.

        Priority:
        1. Emotional intensity
        2. Empathy metrics (reciprocity)
        3. Default to 0.5
        """
        if not memories:
            return 0.5

        values = []
        for memory in memories:
            if memory.emotional_context and memory.emotional_context.intensity:
                values.append(memory.emotional_context.intensity)
            elif memory.metrics and memory.metrics.reciprocity:
                values.append(memory.metrics.reciprocity)
            else:
                values.append(0.5)

        return sum(values) / len(values) if values else 0.5

    def _calculate_slope(self, values: list[float]) -> tuple[float, float]:
        """
        Calculate linear regression slope and R-squared.

        Returns:
            Tuple of (slope, r_squared)
        """
        if len(values) < 2:
            return 0.0, 0.0

        n = len(values)
        x = list(range(n))

        # Calculate means
        x_mean = sum(x) / n
        y_mean = sum(values) / n

        # Calculate slope
        numerator = sum((x[i] - x_mean) * (values[i] - y_mean) for i in range(n))
        denominator = sum((x[i] - x_mean) ** 2 for i in range(n))

        if denominator == 0:
            return 0.0, 0.0

        slope = numerator / denominator

        # Calculate R-squared
        y_pred = [slope * x[i] + (y_mean - slope * x_mean) for i in range(n)]
        ss_res = sum((values[i] - y_pred[i]) ** 2 for i in range(n))
        ss_tot = sum((values[i] - y_mean) ** 2 for i in range(n))

        r_squared = 1 - (ss_res / ss_tot) if ss_tot != 0 else 0.0

        return slope, max(0, r_squared)  # R-squared can't be negative

    def _format_contradiction_insight(self, contradiction: Contradiction) -> str:
        """Format a contradiction as an insight statement."""
        if contradiction.type == "direct_conflict":
            return (
                f"Direct conflict detected in {contradiction.attribute}: "
                f"changed from '{contradiction.old_value}' to '{contradiction.new_value}' "
                f"over {contradiction.temporal_distance_days} days"
            )
        if contradiction.type == "evolution":
            return (
                f"Improvement in {contradiction.attribute}: "
                f"increased from {contradiction.old_value} to {contradiction.new_value} "
                f"over {contradiction.temporal_distance_days} days"
            )
        # regression
        return (
            f"Setback in {contradiction.attribute}: "
            f"decreased from {contradiction.old_value} to {contradiction.new_value} "
            f"over {contradiction.temporal_distance_days} days"
        )

    def _format_trend_insight(self, trend: TemporalTrend) -> str:
        """Format a trend as an insight statement."""
        return (
            f"{trend.direction.capitalize()} trend in {trend.topic}: "
            f"{trend.start_value:.2f} -> {trend.end_value:.2f} "
            f"(slope: {trend.slope:.3f}, R²: {trend.r_squared:.2f})"
        )


# Global instance management
_ENHANCED_SYNTHESIZER_CONTAINER: dict[str, EnhancedMemorySynthesizer | None] = {"instance": None}
_enhanced_synthesizer_lock = threading.Lock()


def get_enhanced_synthesizer() -> EnhancedMemorySynthesizer:
    """Get or create global enhanced synthesizer instance (thread-safe)."""
    if _ENHANCED_SYNTHESIZER_CONTAINER["instance"] is None:
        with _enhanced_synthesizer_lock:
            if _ENHANCED_SYNTHESIZER_CONTAINER["instance"] is None:
                _ENHANCED_SYNTHESIZER_CONTAINER["instance"] = EnhancedMemorySynthesizer()
    return _ENHANCED_SYNTHESIZER_CONTAINER["instance"]


def reset_enhanced_synthesizer() -> None:
    """Reset global enhanced synthesizer (for testing)."""
    with _enhanced_synthesizer_lock:
        _ENHANCED_SYNTHESIZER_CONTAINER["instance"] = None
