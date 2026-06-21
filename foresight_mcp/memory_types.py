"""
Foresight Memory Types - Rich memory objects with psychological safety features.
Restored from src/lib/ai/memory/types.ts
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

# Memory Scope: Defines the logical boundary of the memory
MemoryScope = Literal["session", "arc", "trait", "fact"]

# Retention Policy: Defines how long the memory is kept in active vector space
RetentionPolicy = Literal["ephemeral", "short_term", "long_term", "permanent"]


@dataclass
class EmotionalMetadata:
    """Emotional Metadata based on Plutchik's Wheel + Big Five normalized scores."""

    valence: float = 0.0  # -1 (negative) to 1 (positive)
    arousal: float = 0.0  # 0 (calm) to 1 (intense)
    dominance: float = 0.0  # 0 (submissive) to 1 (dominant)
    primary_emotion: str = ""
    intensity: float = 0.0  # 0 (none) to 1 (maximum)

    def to_dict(self) -> dict:
        return {
            "valence": self.valence,
            "arousal": self.arousal,
            "dominance": self.dominance,
            "primary_emotion": self.primary_emotion,
            "intensity": self.intensity,
        }

    @classmethod
    def from_dict(cls, data: dict) -> EmotionalMetadata:
        return cls(
            valence=data.get("valence", 0.0),
            arousal=data.get("arousal", 0.0),
            dominance=data.get("dominance", 0.0),
            primary_emotion=data.get("primary_emotion", ""),
            intensity=data.get("intensity", 0.0),
        )


@dataclass
class EmpathyMetrics:
    """Empathy Metrics derived from the interaction."""

    reciprocity: float = 0.5  # How well the user matched empathy
    validation_accuracy: float = 0.5  # How accurately the user validated
    resistance_level: float = 0.0  # User's resistance to persona shift

    def to_dict(self) -> dict:
        return {
            "reciprocity": self.reciprocity,
            "validation_accuracy": self.validation_accuracy,
            "resistance_level": self.resistance_level,
        }

    @classmethod
    def from_dict(cls, data: dict) -> EmpathyMetrics:
        return cls(
            reciprocity=data.get("reciprocity", 0.5),
            validation_accuracy=data.get("validation_accuracy", 0.5),
            resistance_level=data.get("resistance_level", 0.0),
        )


@dataclass
class MemoryObject:
    """
    Memory Object: The atomic unit of the memory system.
    Restored from src/lib/ai/memory/types.ts
    """

    id: str
    timestamp: str
    scope: MemoryScope
    retention: RetentionPolicy
    content: str
    tags: list[str] = field(default_factory=list)
    synthesized_from: list[str] = field(default_factory=list)  # IDs of source memories
    is_ghost: bool = False
    emotional_context: EmotionalMetadata | None = None
    metrics: EmpathyMetrics | None = None
    vector_id: str | None = None  # ID in the vector database
    gist: str | None = None  # 10-word summary for Ghost Nodes

    def to_dict(self) -> dict:
        result = {
            "id": self.id,
            "timestamp": self.timestamp,
            "scope": self.scope,
            "retention": self.retention,
            "content": self.content,
            "tags": self.tags,
            "synthesized_from": self.synthesized_from,
            "is_ghost": self.is_ghost,
        }
        if self.emotional_context:
            result["emotional_context"] = self.emotional_context.to_dict()
        if self.metrics:
            result["metrics"] = self.metrics.to_dict()
        if self.vector_id:
            result["vector_id"] = self.vector_id
        if self.gist:
            result["gist"] = self.gist
        return result

    @classmethod
    def from_dict(cls, data: dict) -> MemoryObject:
        emotional_ctx = None
        if data.get("emotional_context"):
            emotional_ctx = EmotionalMetadata.from_dict(data["emotional_context"])

        metrics = None
        if data.get("metrics"):
            metrics = EmpathyMetrics.from_dict(data["metrics"])

        return cls(
            id=data["id"],
            timestamp=data["timestamp"],
            scope=data.get("scope", "session"),
            retention=data.get("retention", "short_term"),
            content=data["content"],
            tags=data.get("tags", []),
            synthesized_from=data.get("synthesized_from", []),
            is_ghost=data.get("is_ghost", False),
            emotional_context=emotional_ctx,
            metrics=metrics,
            vector_id=data.get("vector_id"),
            gist=data.get("gist"),
        )

    @classmethod
    def create(
        cls,
        content: str,
        scope: MemoryScope = "session",
        retention: RetentionPolicy = "short_term",
        emotional_context: EmotionalMetadata | None = None,
        metrics: EmpathyMetrics | None = None,
    ) -> MemoryObject:
        """Factory method to create a new memory with auto-generated ID and timestamp."""
        return cls(
            id=str(uuid.uuid4()),
            timestamp=datetime.now(UTC).isoformat(),
            scope=scope,
            retention=retention,
            content=content,
            emotional_context=emotional_context,
            metrics=metrics,
        )


@dataclass
class StanceShift:
    """
    Stance Shift: Represents a detected change in user/persona behavior.
    Restored from src/lib/ai/memory/types.ts
    """

    attribute: str  # e.g., 'openness', 'defensiveness'
    old_value: float
    new_value: float
    delta: float
    evidence_ids: list[str]  # IDs of memories showing the shift
    confidence: float

    def to_dict(self) -> dict:
        return {
            "attribute": self.attribute,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "delta": self.delta,
            "evidence_ids": self.evidence_ids,
            "confidence": self.confidence,
        }


@dataclass
class SynthesisResult:
    """
    Synthesis Result: The output of a reconciliation pass.
    Restored from src/lib/ai/memory/types.ts
    """

    merged_ids: list[str]
    new_memory_id: str
    stance_shifts: list[StanceShift]
    compression_ratio: float

    def to_dict(self) -> dict:
        return {
            "merged_ids": self.merged_ids,
            "new_memory_id": self.new_memory_id,
            "stance_shifts": [ss.to_dict() for ss in self.stance_shifts],
            "compression_ratio": self.compression_ratio,
        }


# Gate Decision Levels
GateDecision = Literal["auto", "passive", "active", "block"]


@dataclass
class GateResult:
    """
    Gate Result: The result of Socratic Gate evaluation.
    Restored from src/lib/ai/memory/types.ts
    """

    decision: GateDecision
    reason: str
    suggested_tags: list[str]
    anomaly_detected: bool = False  # Renamed from crisis_detected for domain-agnostic naming

    def to_dict(self) -> dict:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "suggested_tags": self.suggested_tags,
            "anomaly_detected": self.anomaly_detected,
        }
