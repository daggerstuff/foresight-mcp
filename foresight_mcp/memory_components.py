"""
Foresight Memory Components
Restored from src/lib/ai/memory/ - Socratic Gate, Crisis Tagger, Synthesizer, Linker
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .crisis_detection import AnomalyDetector, get_anomaly_detector
from .memory_types import GateDecision, GateResult, MemoryObject, StanceShift, SynthesisResult


class MemoryCrisisTagger:
    """
    Hybrid Crisis Signature Auto-Tagger
    Combines high-performance keyword scanning with deep AI analysis.
    Restored from src/lib/ai/memory/tagger.ts
    """

    def __init__(self, detector: AnomalyDetector | None = None):
        """
        Initialize the memory crisis tagger.

        Args:
            detector: AnomalyDetector instance (uses MentalHealthAnomalyDetector by default)
        """
        self.detector = detector or get_anomaly_detector(detector_type="mental_health")

    async def tag_memory(self, memory: MemoryObject, user_id: str) -> list[str]:
        """
        Analyzes a memory object's content and returns a list of tags.

        Args:
            memory: The memory object to analyze
            user_id: The user ID associated with this memory

        Returns:
            List of tags including crisis signals if detected
        """
        tags = []

        try:
            # Use anomaly detector for analysis
            result = self.detector.detect(
                content=memory.content, sensitivity_level="high", user_id=user_id, source="memory_tagger"
            )

            if result.is_anomaly:
                tags.append("ANOMALY_SIGNAL")
            if result.category:
                tags.append(f"ANOMALY_TYPE_{result.category.upper()}")
            tags.append(f"RISK_{result.risk_level.upper()}")

            if result.urgency == "immediate":
                tags.append("URGENT_INTERVENTION")
            elif result.confidence > 0.3:
                # Minor concern but not a full anomaly
                tags.append("CONCERN_SIGNAL")
                if result.category:
                    tags.append(f"CONCERN_TYPE_{result.category.upper()}")

            # Add detected terms as granular tags
            if result.detected_terms:
                for term in result.detected_terms:
                    tag = f"TERM_{term.upper().replace(' ', '_').replace('-', '_')}"
                    tags.append(tag)

            return list(set(tags))

        except Exception:
            # Log error but don't fail - return safe default
            return ["ERROR_ANALYSIS_FAILED"]


class SocraticGate:
    """
    Socratic Gate - Middleman for memory ingestion.
    Ensures psychological safety and data quality.
    Restored from src/lib/ai/memory/gate.ts
    """

    def __init__(self, tagger: MemoryCrisisTagger | None = None):
        self.tagger = tagger or MemoryCrisisTagger()

    async def evaluate(self, memory: MemoryObject, user_id: str) -> GateResult:
        """
        Evaluates if a memory should be ingested and with what level of confirmation.

        Args:
            memory: The memory object to evaluate
            user_id: The user ID associated with this memory

        Returns:
            GateResult with decision, reason, and suggested tags
        """
        try:
            # 1. Tag for anomaly and context
            tags = await self.tagger.tag_memory(memory, user_id)
            is_anomaly = "ANOMALY_SIGNAL" in tags

            # 2. Determine Decision Level
            decision: GateDecision = "auto"
            reason = "Normal information flow."

            if is_anomaly:
                decision = "active"
                reason = "Anomaly signal detected. Requires immediate professional review."
            elif any(t.startswith("CONCERN") for t in tags):
                decision = "passive"
                reason = "Moderate concern detected. Flagged for review in post-session summary."
            elif len(memory.content) > 500:
                # Large data chunks should be passively accepted
                decision = "passive"
                reason = "Large data volume. Ingesting passively to maintain performance."

            # 3. Trait shifts always require confirmation
            if memory.scope == "trait":
                decision = "active"
                reason = "Permanent trait modification requires explicit supervisor confirmation."

            return GateResult(decision=decision, reason=reason, suggested_tags=tags, anomaly_detected=is_anomaly)

        except Exception:
            # Safety first - block on errors
            return GateResult(
                decision="block",
                reason="Internal safety gate error. Blocking ingestion for security.",
                suggested_tags=["ERROR_GATE_FAILURE"],
                anomaly_detected=True,
            )


class MemorySynthesizer:
    """
    Memory Synthesizer - Handles reconciliation of stale memories
    and detection of behavioral shifts.
    Restored from src/lib/ai/memory/synthesizer.ts
    """

    def __init__(self):
        self.reconciliation_threshold = 0.4
        self.shift_threshold = 0.25

    async def synthesize(self, memories: list[MemoryObject]) -> SynthesisResult | None:
        """
        Performs synthesis over a set of memories.
        Identifies logical clusters for merging and detects stance shifts.

        Args:
            memories: List of memories to synthesize

        Returns:
            SynthesisResult or None if not enough data
        """
        if len(memories) < 5:
            return None  # Not enough context for synthesis

        try:
            # 1. Calculate Stance Shifts (comparing recent vs historic)
            historic, recent = self._split_recent_and_historic(memories)
            stance_shifts = self._detect_stance_shifts(historic, recent)

            # 2. Identify candidates for merging (low importance/decayed)
            merge_candidates = self._identify_merge_candidates(memories)

            if len(merge_candidates) < 2:
                return SynthesisResult(
                    merged_ids=[], new_memory_id="", stance_shifts=stance_shifts, compression_ratio=1.0
                )

            # 3. Create synthesized "Abstract Memory"
            merged_ids = [m.id for m in merge_candidates]

            return SynthesisResult(
                merged_ids=merged_ids,
                new_memory_id=self._generate_synthesis_id(),
                stance_shifts=stance_shifts,
                compression_ratio=len(memories) / (len(memories) - len(merge_candidates) + 1),
            )

        except Exception:
            return None

    def _split_recent_and_historic(self, memories: list[MemoryObject]) -> tuple[list[MemoryObject], list[MemoryObject]]:
        """Splits memories into historic baseline and recent observations (last 20%)."""
        sorted_memories = sorted(
            memories, key=lambda m: datetime.fromisoformat(m.timestamp.replace("Z", "+00:00")).timestamp()
        )
        split_idx = int(len(sorted_memories) * 0.8)
        return sorted_memories[:split_idx], sorted_memories[split_idx:]

    def _detect_stance_shifts(self, historic: list[MemoryObject], recent: list[MemoryObject]) -> list[StanceShift]:
        """Detects behavioral shifts in empathy and emotional metrics."""
        shifts = []

        historic_empathy = self._avg_empathy(historic)
        recent_empathy = self._avg_empathy(recent)

        # Check reciprocity shift
        reciprocity_delta = recent_empathy["reciprocity"] - historic_empathy["reciprocity"]
        if abs(reciprocity_delta) > self.shift_threshold:
            shifts.append(
                StanceShift(
                    attribute="reciprocity",
                    old_value=historic_empathy["reciprocity"],
                    new_value=recent_empathy["reciprocity"],
                    delta=reciprocity_delta,
                    evidence_ids=[m.id for m in recent],
                    confidence=0.8,
                )
            )

        # Check validation accuracy shift
        validation_delta = recent_empathy["validation_accuracy"] - historic_empathy["validation_accuracy"]
        if abs(validation_delta) > self.shift_threshold:
            shifts.append(
                StanceShift(
                    attribute="validation_accuracy",
                    old_value=historic_empathy["validation_accuracy"],
                    new_value=recent_empathy["validation_accuracy"],
                    delta=validation_delta,
                    evidence_ids=[m.id for m in recent],
                    confidence=0.75,
                )
            )

        return shifts

    def _identify_merge_candidates(self, memories: list[MemoryObject]) -> list[MemoryObject]:
        """Identifies memories that are candidates for archival/synthesis."""
        candidates = []
        for m in memories:
            # Never merge traits or facts without manual review
            if m.scope in ("trait", "fact"):
                continue

            # Never merge anomaly signals
            if "ANOMALY_SIGNAL" in (m.tags or []):
                continue

            score = self._calculate_importance(m)
            if score < self.reconciliation_threshold:
                candidates.append(m)

        return candidates

    def _calculate_importance(self, memory: MemoryObject) -> float:
        """Calculates importance based on recency and intensity."""
        now = datetime.now(timezone.utc).timestamp() * 1000
        ts = memory.timestamp.replace("Z", "+00:00")
        try:
            memory_time = datetime.fromisoformat(ts).timestamp() * 1000
        except Exception:
            memory_time = 0

        age_ms = now - memory_time
        day_in_ms = 24 * 60 * 60 * 1000

        # Time decay: 1.0 at creation, halves every 7 days
        decay = pow(0.5, age_ms / (7 * day_in_ms)) if age_ms > 0 else 1.0

        # Intensity boost
        intensity = 0.2
        if memory.emotional_context:
            intensity = memory.emotional_context.intensity or 0.2

        # Hybrid score
        return (decay * 0.7) + (intensity * 0.3)

    def _avg_empathy(self, mems: list[MemoryObject]) -> dict:
        """Calculate average empathy metrics."""
        valid = [m for m in mems if m.metrics]
        if not valid:
            return {"reciprocity": 0.5, "validation_accuracy": 0.5}

        return {
            "reciprocity": sum(m.metrics.reciprocity for m in valid if m.metrics) / len(valid),
            "validation_accuracy": sum(m.metrics.validation_accuracy for m in valid if m.metrics) / len(valid),
        }

    def _generate_synthesis_id(self) -> str:
        """Generate a unique ID for synthesized memory."""
        return str(uuid.uuid4())


class MemoryLinker:
    """
    Memory Linker - Manages relationships between active memories
    and their vector representations.
    Restored from src/lib/ai/memory/linker.ts
    """

    def link_vector(self, memory: MemoryObject, vector_id: str) -> MemoryObject:
        """
        Links a memory object to a vector ID.

        Args:
            memory: The memory object to link
            vector_id: The vector database ID

        Returns:
            Updated memory object with vector_id
        """
        memory.vector_id = vector_id
        return memory

    def to_ghost(self, memory: MemoryObject) -> MemoryObject:
        """
        Archives a memory into a "Ghost Node".
        Redacts the content and preserves the summary (gist).

        Args:
            memory: The memory object to archive

        Returns:
            Ghost node memory object
        """
        if not memory.vector_id:
            raise ValueError(f"Cannot archive memory {memory.id} without a vector_id.")

        memory.is_ghost = True
        memory.content = "[ARCHIVED_GHOST_NODE]"
        if not memory.gist:
            memory.gist = self._generate_gist(memory.content)

        return memory

    def _generate_gist(self, content: str) -> str:
        """Generates a short gist for the ghost node if not provided."""
        words = content.split()
        if len(words) <= 10:
            return content
        return " ".join(words[:10]) + "..."
