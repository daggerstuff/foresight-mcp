"""
Enhanced Memory Synthesizer with Temporal and Graph Analysis.

Extends the existing MemorySynthesizer with:
- Contradiction detection (direct conflicts, evolutions, regressions)
- Temporal trend analysis (improving/worsening/stable)
- Entity-based clustering for synthesis
- Evidence-anchored insights (prevents hallucination)
"""
from __future__ import annotations
from typing import List, Optional, Dict, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import uuid

from .memory_types import MemoryObject, StanceShift, SynthesisResult, EmpathyMetrics
from .memory_components import MemorySynthesizer, MemoryCrisisTagger

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
    evidence_ids: List[str]
    confidence: float

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            'type': self.type,
            'attribute': self.attribute,
            'old_value': self.old_value,
            'new_value': self.new_value,
            'delta': self.delta,
            'temporal_distance_days': self.temporal_distance_days,
            'evidence_ids': self.evidence_ids,
            'confidence': self.confidence,
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
    evidence_ids: List[str]
    start_value: float
    end_value: float

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            'topic': self.topic,
            'direction': self.direction,
            'slope': self.slope,
            'r_squared': self.r_squared,
            'evidence_ids': self.evidence_ids,
            'start_value': self.start_value,
            'end_value': self.end_value,
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
    evidence_ids: List[str]
    recommended_action: str  # 'preserve' | 'consolidate' | 'review'
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            'statement': self.statement,
            'insight_type': self.insight_type,
            'confidence': self.confidence,
            'evidence_ids': self.evidence_ids,
            'recommended_action': self.recommended_action,
            'metadata': self.metadata,
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
    merged_ids: List[str]
    new_memory_id: str
    stance_shifts: List[StanceShift]
    compression_ratio: float

    # New fields
    contradictions: List[Contradiction] = field(default_factory=list)
    temporal_trends: List[TemporalTrend] = field(default_factory=list)
    insights: List[Insight] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            'merged_ids': self.merged_ids,
            'new_memory_id': self.new_memory_id,
            'stance_shifts': [ss.to_dict() for ss in self.stance_shifts],
            'compression_ratio': self.compression_ratio,
            'contradictions': [c.to_dict() for c in self.contradictions],
            'temporal_trends': [t.to_dict() for t in self.temporal_trends],
            'insights': [i.to_dict() for i in self.insights],
        }


class EnhancedMemorySynthesizer:
    """
    Enhanced memory synthesizer with temporal and graph analysis.

    Extends the base MemorySynthesizer with:
    - Contradiction detection
    - Temporal trend analysis
    - Evidence-anchored insight generation
    """

    def __init__(
        self,
        base_synthesizer: Optional[MemorySynthesizer] = None,
        contradiction_threshold: float = 0.25,
        trend_significance_threshold: float = 0.15,
        min_memories_for_trend: int = 5,
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

    async def synthesize(
        self,
        memories: List[MemoryObject],
        user_id: str = 'default'
    ) -> Optional[EnhancedSynthesisResult]:
        """
        Perform enhanced synthesis over memories.

        Args:
            memories: List of memories to synthesize
            user_id: User ID for context

        Returns:
            EnhancedSynthesisResult or None if not enough data
        """
        if len(memories) < 5:
            return None

        try:
            # Run base synthesis
            base_result = await self.base_synthesizer.synthesize(memories)
            if base_result is None:
                return None

            # Split recent and historic
            splits = self._split_recent_and_historic(memories)

            # Detect contradictions
            contradictions = self._detect_contradictions(
                splits['historic'],
                splits['recent']
            )

            # Analyze temporal trends
            temporal_trends = self._analyze_temporal_trends(memories)

            # Generate insights (evidence-anchored)
            insights = self._generate_insights(
                memories,
                contradictions,
                temporal_trends
            )

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

    def _split_recent_and_historic(
        self,
        memories: List[MemoryObject]
    ) -> Dict[str, List[MemoryObject]]:
        """Split memories into historic (80%) and recent (20%)."""
        sorted_memories = sorted(
            memories,
            key=lambda m: datetime.fromisoformat(
                m.timestamp.replace('Z', '+00:00')
            ).timestamp()
        )

        split_idx = int(len(sorted_memories) * 0.8)
        return {
            'historic': sorted_memories[:split_idx],
            'recent': sorted_memories[split_idx:],
        }

    def _detect_contradictions(
        self,
        historic: List[MemoryObject],
        recent: List[MemoryObject]
    ) -> List[Contradiction]:
        """
        Detect contradictions between historic and recent memories.

        Types:
        - direct_conflict: Opposite values (>0.5 delta)
        - evolution: Gradual improvement (positive delta)
        - regression: Setback (negative delta)
        """
        contradictions: List[Contradiction] = []

        # Group memories by topic (simplified: by tags or content similarity)
        topic_clusters = self._cluster_by_topic(historic + recent)

        for topic, cluster in topic_clusters.items():
            if len(cluster) < 2:
                continue

            # Split cluster into historic and recent
            historic_cluster = [m for m in cluster if m in historic]
            recent_cluster = [m for m in cluster if m in recent]

            if not historic_cluster or not recent_cluster:
                continue

            # Calculate average values
            historic_avg = self._extract_metric_value(historic_cluster)
            recent_avg = self._extract_metric_value(recent_cluster)

            delta = recent_avg - historic_avg

            if abs(delta) > self.contradiction_threshold:
                # Determine type
                if abs(delta) > 0.5:
                    contradiction_type = 'direct_conflict'
                elif delta > 0:
                    contradiction_type = 'evolution'
                else:
                    contradiction_type = 'regression'

                # Calculate temporal distance
                historic_time = datetime.fromisoformat(
                    historic_cluster[0].timestamp.replace('Z', '+00:00')
                )
                recent_time = datetime.fromisoformat(
                    recent_cluster[0].timestamp.replace('Z', '+00:00')
                )
                days_diff = (recent_time - historic_time).days

                contradictions.append(Contradiction(
                    type=contradiction_type,
                    attribute=topic,
                    old_value=f"{historic_avg:.2f}",
                    new_value=f"{recent_avg:.2f}",
                    delta=delta,
                    temporal_distance_days=days_diff,
                    evidence_ids=[m.id for m in cluster],
                    confidence=min(abs(delta) / 0.5, 1.0),
                ))

        return contradictions

    def _analyze_temporal_trends(
        self,
        memories: List[MemoryObject]
    ) -> List[TemporalTrend]:
        """
        Analyze temporal trends in memories.

        Identifies improving/worsening/stable patterns over time.
        """
        trends: List[TemporalTrend] = []

        # Group by topic
        topic_clusters = self._cluster_by_topic(memories)

        for topic, cluster in topic_clusters.items():
            if len(cluster) < self.min_memories_for_trend:
                continue

            # Sort by timestamp
            sorted_cluster = sorted(
                cluster,
                key=lambda m: datetime.fromisoformat(
                    m.timestamp.replace('Z', '+00:00')
                ).timestamp()
            )

            # Extract values and calculate slope
            values = [self._extract_metric_value([m]) for m in sorted_cluster]
            slope, r_squared = self._calculate_slope(values)

            if abs(slope) > self.trend_significance_threshold:
                direction = 'improving' if slope > 0 else 'worsening'

                trends.append(TemporalTrend(
                    topic=topic,
                    direction=direction,
                    slope=slope,
                    r_squared=r_squared,
                    evidence_ids=[m.id for m in sorted_cluster],
                    start_value=values[0],
                    end_value=values[-1],
                ))

        return trends

    def _generate_insights(
        self,
        memories: List[MemoryObject],
        contradictions: List[Contradiction],
        temporal_trends: List[TemporalTrend]
    ) -> List[Insight]:
        """
        Generate evidence-anchored insights.

        All insights must cite source memory IDs to prevent hallucination.
        """
        insights: List[Insight] = []

        # Generate insights from contradictions
        for contradiction in contradictions:
            if contradiction.confidence >= 0.7:
                insights.append(Insight(
                    statement=self._format_contradiction_insight(contradiction),
                    insight_type='contradiction',
                    confidence=contradiction.confidence,
                    evidence_ids=contradiction.evidence_ids,
                    recommended_action='review',
                    metadata={'contradiction_type': contradiction.type},
                ))

        # Generate insights from trends
        for trend in temporal_trends:
            if trend.r_squared >= 0.5:  # Good fit
                insights.append(Insight(
                    statement=self._format_trend_insight(trend),
                    insight_type='trend',
                    confidence=trend.r_squared,
                    evidence_ids=trend.evidence_ids,
                    recommended_action='preserve' if trend.direction == 'improving' else 'review',
                    metadata={
                        'slope': trend.slope,
                        'start_value': trend.start_value,
                        'end_value': trend.end_value,
                    },
                ))

        return insights

    def _cluster_by_topic(
        self,
        memories: List[MemoryObject]
    ) -> Dict[str, List[MemoryObject]]:
        """
        Cluster memories by topic.

        Simplified clustering based on tags and content keywords.
        In production, this would use semantic similarity.
        """
        clusters: Dict[str, List[MemoryObject]] = {}

        # Common topics to look for
        topics = [
            'anxiety', 'stress', 'therapy', 'mood',
            'sleep', 'work', 'family', 'health',
            'coping', 'progress', 'challenge'
        ]

        for memory in memories:
            content_lower = memory.content.lower()
            tags_lower = [t.lower() for t in memory.tags]

            for topic in topics:
                if topic in content_lower or topic in tags_lower:
                    if topic not in clusters:
                        clusters[topic] = []
                    clusters[topic].append(memory)
                    break  # Only assign to first matching topic

        return clusters

    def _extract_metric_value(self, memories: List[MemoryObject]) -> float:
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

    def _calculate_slope(self, values: List[float]) -> Tuple[float, float]:
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
        if contradiction.type == 'direct_conflict':
            return (
                f"Direct conflict detected in {contradiction.attribute}: "
                f"changed from '{contradiction.old_value}' to '{contradiction.new_value}' "
                f"over {contradiction.temporal_distance_days} days"
            )
        elif contradiction.type == 'evolution':
            return (
                f"Improvement in {contradiction.attribute}: "
                f"increased from {contradiction.old_value} to {contradiction.new_value} "
                f"over {contradiction.temporal_distance_days} days"
            )
        else:  # regression
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
_enhanced_synthesizer: Optional[EnhancedMemorySynthesizer] = None


def get_enhanced_synthesizer() -> EnhancedMemorySynthesizer:
    """Get or create global enhanced synthesizer instance."""
    global _enhanced_synthesizer
    if _enhanced_synthesizer is None:
        _enhanced_synthesizer = EnhancedMemorySynthesizer()
    return _enhanced_synthesizer


def reset_enhanced_synthesizer() -> None:
    """Reset global enhanced synthesizer (for testing)."""
    global _enhanced_synthesizer
    _enhanced_synthesizer = None
