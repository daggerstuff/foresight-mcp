"""
Unified Memory Schema — Python Pydantic models.

Single source of truth for the Foresight MCP server and AI services.
Mirrors packages/memory-schema/src/types.ts exactly.

Sprint 1 — ADHD-318: Design Unified Memory Schema
Epic: ADHD-3 Foresight Memory Architecture
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------

MEMORY_SCHEMA_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class MemoryScope(str, Enum):
    """Logical lifecycle boundary of a memory."""

    SESSION = "session"  # Current conversation only
    ARC = "arc"  # Spans a therapeutic arc
    TRAIT = "trait"  # Persistent user/persona trait
    FACT = "fact"  # Ground-truth factual knowledge


class RetentionPolicy(str, Enum):
    """Controls how long a memory stays in active vector space."""

    EPHEMERAL = "ephemeral"  # < 1 hour
    SHORT_TERM = "short_term"  # 1 day – 1 week
    LONG_TERM = "long_term"  # 1 week – 6 months
    PERMANENT = "permanent"  # Never evicted


class StrengthTrend(str, Enum):
    """Memory strength trend — set by temporal decay scheduler."""

    STABLE = "stable"
    STRENGTHENING = "strengthening"
    WEAKENING = "weakening"
    STALE = "stale"


class GateDecision(str, Enum):
    """Socratic Gate evaluation decision."""

    AUTO = "auto"
    PASSIVE = "passive"
    ACTIVE = "active"
    BLOCK = "block"


class SourceService(str, Enum):
    """Which service originally wrote this memory."""

    FORESIGHT = "foresight"
    AI_SERVICES = "ai-services"
    ASTRO_FRONTEND = "astro-frontend"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Sub-objects
# ---------------------------------------------------------------------------


class EmotionalContext(BaseModel):
    """Emotional metadata anchored to Plutchik's Wheel of Emotions."""

    valence: float = Field(0.0, ge=-1.0, le=1.0, description="Valence: -1.0 (negative) → 1.0 (positive)")
    arousal: float = Field(0.0, ge=0.0, le=1.0, description="Arousal: 0.0 (calm) → 1.0 (activated)")
    dominance: float = Field(0.0, ge=0.0, le=1.0, description="Dominance: 0.0 (submissive) → 1.0 (dominant)")
    primary_emotion: str = Field("", description="Primary detected emotion")
    intensity: float = Field(0.0, ge=0.0, le=1.0, description="Intensity: 0.0 (trace) → 1.0 (maximum)")

    def to_camel_dict(self) -> dict:
        """Serialize to camelCase for cross-language JSON exchange."""
        return {
            "valence": self.valence,
            "arousal": self.arousal,
            "dominance": self.dominance,
            "primaryEmotion": self.primary_emotion,
            "intensity": self.intensity,
        }

    @classmethod
    def from_camel_dict(cls, d: dict) -> EmotionalContext:
        return cls(
            valence=d.get("valence", 0.0),
            arousal=d.get("arousal", 0.0),
            dominance=d.get("dominance", 0.0),
            primary_emotion=d.get("primaryEmotion", d.get("primary_emotion", "")),
            intensity=d.get("intensity", 0.0),
        )

    @classmethod
    def from_sqlite_json(cls, raw: str | dict | None) -> EmotionalContext | None:
        if not raw:
            return None
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return None
        if not raw:
            return None
        # Support both snake_case (legacy) and camelCase (unified)
        return cls.from_camel_dict(raw)


class EmpathyMetrics(BaseModel):
    """Empathy quality metrics from a therapeutic interaction."""

    reciprocity: float = Field(0.5, ge=0.0, le=1.0, description="How well participant matched AI empathy")
    validation_accuracy: float = Field(0.5, ge=0.0, le=1.0, description="Accuracy of emotional validation")
    resistance_level: float = Field(0.0, ge=0.0, le=1.0, description="Resistance to persona/perspective shift")

    def to_camel_dict(self) -> dict:
        return {
            "reciprocity": self.reciprocity,
            "validationAccuracy": self.validation_accuracy,
            "resistanceLevel": self.resistance_level,
        }

    @classmethod
    def from_camel_dict(cls, d: dict) -> EmpathyMetrics:
        return cls(
            reciprocity=d.get("reciprocity", 0.5),
            validation_accuracy=d.get("validationAccuracy", d.get("validation_accuracy", 0.5)),
            resistance_level=d.get("resistanceLevel", d.get("resistance_level", 0.0)),
        )

    @classmethod
    def from_sqlite_json(cls, raw: str | dict | None) -> EmpathyMetrics | None:
        if not raw:
            return None
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return None
        if not raw:
            return None
        return cls.from_camel_dict(raw)


# ---------------------------------------------------------------------------
# Canonical Memory Object
# ---------------------------------------------------------------------------


class UnifiedMemory(BaseModel):
    """
    Canonical memory object — shared by all Pixelated Empathy services.

    SQLite mapping (Foresight):   snake_case columns
    MongoDB mapping (ai-services): see to_mongo_doc() / from_mongo_doc()
    TypeScript mapping:            camelCase (see types.ts)
    """

    # ── Identity ──────────────────────────────────────────────────────────
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str = Field("default", max_length=64)
    user_id: str = Field(..., max_length=128)
    bank_id: str = Field("default")

    # ── Content ───────────────────────────────────────────────────────────
    content: str = Field(..., min_length=1, max_length=100_000)
    scope: MemoryScope = Field(MemoryScope.SESSION)
    retention: RetentionPolicy = Field(RetentionPolicy.SHORT_TERM)
    category: str = Field("general")
    tags: list[str] = Field(default_factory=list)

    # ── Versioning ────────────────────────────────────────────────────────
    version: int = Field(1, ge=1)
    schema_version: str = Field(MEMORY_SCHEMA_VERSION)
    source_service: SourceService = Field(SourceService.FORESIGHT)
    is_latest: bool = Field(True)
    valid_from: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    valid_until: str | None = Field(None)

    # ── Decay & Importance ────────────────────────────────────────────────
    importance: float = Field(0.5, ge=0.0, le=1.0)
    decay_rate: float = Field(0.01, ge=0.0)
    strength_trend: StrengthTrend = Field(StrengthTrend.STABLE)
    activation_count: int = Field(0, ge=0)
    retrieval_count: int = Field(0, ge=0)

    # ── Ghost / Synthesis ─────────────────────────────────────────────────
    is_ghost: bool = Field(False)
    gist: str | None = Field(None, max_length=200)
    synthesized_from: list[str] = Field(default_factory=list)

    # ── Embeddings ────────────────────────────────────────────────────────
    vector_id: str | None = Field(None)

    # ── Emotional / Clinical ──────────────────────────────────────────────
    emotional_context: EmotionalContext | None = Field(None)
    empathy_metrics: EmpathyMetrics | None = Field(None)

    # ── Timestamps ────────────────────────────────────────────────────────
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str | None = Field(None)
    accessed_at: str | None = Field(None)
    last_retrieved_at: str | None = Field(None)

    # -------------------------------------------------------------------------
    # Factory
    # -------------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        content: str,
        user_id: str,
        *,
        tenant_id: str = "default",
        bank_id: str = "default",
        scope: MemoryScope = MemoryScope.SESSION,
        retention: RetentionPolicy = RetentionPolicy.SHORT_TERM,
        category: str = "general",
        tags: list[str] | None = None,
        importance: float = 0.5,
        source_service: SourceService = SourceService.FORESIGHT,
        emotional_context: EmotionalContext | None = None,
        empathy_metrics: EmpathyMetrics | None = None,
    ) -> UnifiedMemory:
        """Factory — creates a new UnifiedMemory with auto-generated id and timestamp."""
        return cls(
            content=content,
            user_id=user_id,
            tenant_id=tenant_id,
            bank_id=bank_id,
            scope=scope,
            retention=retention,
            category=category,
            tags=tags or [],
            importance=importance,
            source_service=source_service,
            emotional_context=emotional_context,
            empathy_metrics=empathy_metrics,
        )

    # -------------------------------------------------------------------------
    # SQLite serialization (Foresight)
    # -------------------------------------------------------------------------

    def to_sqlite_row(self) -> dict[str, Any]:
        """Serialize to a dict suitable for INSERT/UPDATE in the Foresight memories table."""
        return {
            "id": self.id,
            "content": self.content,
            "tenant_id": self.tenant_id,
            "scope": self.scope.value,
            "retention": self.retention.value,
            "category": self.category,
            "user_id": self.user_id,
            "bank_id": self.bank_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "tags": json.dumps(self.tags),
            "emotional_context": json.dumps(self.emotional_context.to_camel_dict() if self.emotional_context else {}),
            "metrics": json.dumps(self.empathy_metrics.to_camel_dict() if self.empathy_metrics else {}),
            "vector_id": self.vector_id,
            "gist": self.gist,
            "is_ghost": int(self.is_ghost),
            "synthesized_from": json.dumps(self.synthesized_from),
            "version": self.version,
            "schema_version": self.schema_version,
            "source_service": self.source_service.value,
            "is_latest": int(self.is_latest),
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "importance": self.importance,
            "decay_rate": self.decay_rate,
            "strength_trend": self.strength_trend.value,
            "activation_count": self.activation_count,
            "retrieval_count": self.retrieval_count,
            "accessed_at": self.accessed_at,
            "last_retrieved_at": self.last_retrieved_at,
        }

    @classmethod
    def from_sqlite_row(cls, row: dict[str, Any]) -> UnifiedMemory:
        """Deserialize from a Foresight SQLite row (supports both old and new schemas)."""
        tags = row.get("tags", "[]")
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except (json.JSONDecodeError, TypeError):
                tags = []

        synth = row.get("synthesized_from", "[]")
        if isinstance(synth, str):
            try:
                synth = json.loads(synth)
            except (json.JSONDecodeError, TypeError):
                synth = []

        return cls(
            id=row["id"],
            content=row["content"],
            tenant_id=row.get("tenant_id", "default"),
            user_id=row.get("user_id", "default"),
            bank_id=row.get("bank_id", "default"),
            scope=MemoryScope(row.get("scope", "session")),
            retention=RetentionPolicy(row.get("retention", "short_term")),
            category=row.get("category", "general"),
            tags=tags,
            version=row.get("version", 1),
            schema_version=row.get("schema_version", "0.0.0"),
            source_service=SourceService(row.get("source_service", "unknown")),
            is_latest=bool(row.get("is_latest", True)),
            valid_from=row.get("valid_from") or row.get("created_at", datetime.now(UTC).isoformat()),
            valid_until=row.get("valid_until"),
            importance=row.get("importance", 0.5),
            decay_rate=row.get("decay_rate", 0.01),
            strength_trend=StrengthTrend(row.get("strength_trend", "stable")),
            activation_count=row.get("activation_count", 0),
            retrieval_count=row.get("retrieval_count", 0),
            is_ghost=bool(row.get("is_ghost", False)),
            gist=row.get("gist"),
            synthesized_from=synth,
            vector_id=row.get("vector_id"),
            emotional_context=EmotionalContext.from_sqlite_json(row.get("emotional_context")),
            empathy_metrics=EmpathyMetrics.from_sqlite_json(row.get("metrics")),
            created_at=row.get("created_at", datetime.now(UTC).isoformat()),
            updated_at=row.get("updated_at"),
            accessed_at=row.get("accessed_at"),
            last_retrieved_at=row.get("last_retrieved_at"),
        )

    # -------------------------------------------------------------------------
    # MongoDB serialization (ai-services)
    # -------------------------------------------------------------------------

    def to_mongo_doc(self) -> dict[str, Any]:
        """Serialize to a MongoDB document with the unified field naming."""
        doc: dict[str, Any] = {
            "_id": self.id,
            "tenantId": self.tenant_id,
            "userId": self.user_id,
            "bankId": self.bank_id,
            "content": self.content,
            "scope": self.scope.value,
            "retention": self.retention.value,
            "category": self.category,
            "tags": self.tags,
            "version": self.version,
            "schemaVersion": self.schema_version,
            "sourceService": self.source_service.value,
            "isLatest": self.is_latest,
            "validFrom": self.valid_from,
            "validUntil": self.valid_until,
            "importance": self.importance,
            "decayRate": self.decay_rate,
            "strengthTrend": self.strength_trend.value,
            "activationCount": self.activation_count,
            "retrievalCount": self.retrieval_count,
            "isGhost": self.is_ghost,
            "gist": self.gist,
            "synthesizedFrom": self.synthesized_from,
            "vectorId": self.vector_id,
            "emotionalContext": self.emotional_context.to_camel_dict() if self.emotional_context else None,
            "empathyMetrics": self.empathy_metrics.to_camel_dict() if self.empathy_metrics else None,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "accessedAt": self.accessed_at,
            "lastRetrievedAt": self.last_retrieved_at,
        }
        return doc

    @classmethod
    def from_mongo_doc(cls, doc: dict[str, Any]) -> UnifiedMemory:
        """Deserialize from a MongoDB document (unified or legacy ai-services format)."""
        # Handle legacy ai-services format: top-level 'data' field (possibly encrypted)
        if "data" in doc and "content" not in doc:
            data = doc["data"]
            if isinstance(data, dict) and not data.get("_encrypted"):
                # Un-encrypted legacy doc — extract content from nested data
                content = data.get("content", str(data))
            else:
                content = "[encrypted — decrypt before parsing]"
        else:
            content = doc.get("content", "")

        emo = doc.get("emotionalContext")
        emp = doc.get("empathyMetrics")

        return cls(
            id=str(doc.get("_id", doc.get("id", str(uuid.uuid4())))),
            tenant_id=doc.get("tenantId", doc.get("tenant_id", "default")),
            user_id=doc.get("userId", doc.get("user_id", "default")),
            bank_id=doc.get("bankId", doc.get("bank_id", "default")),
            content=content,
            scope=MemoryScope(doc.get("scope", "session")),
            retention=RetentionPolicy(doc.get("retention", "short_term")),
            category=doc.get("category", doc.get("type", "general")),
            tags=doc.get("tags", []),
            version=doc.get("version", 1) if isinstance(doc.get("version"), int) else 1,
            schema_version=doc.get("schemaVersion", doc.get("schema_version", "0.0.0")),
            source_service=SourceService(doc.get("sourceService", doc.get("source_service", "unknown"))),
            is_latest=doc.get("isLatest", doc.get("is_latest", True)),
            valid_from=doc.get(
                "validFrom",
                doc.get("valid_from", doc.get("createdAt", doc.get("created_at", datetime.now(UTC).isoformat()))),
            ),
            valid_until=doc.get("validUntil", doc.get("valid_until")),
            importance=doc.get("importance", 0.5),
            decay_rate=doc.get("decayRate", doc.get("decay_rate", 0.01)),
            strength_trend=StrengthTrend(doc.get("strengthTrend", doc.get("strength_trend", "stable"))),
            activation_count=doc.get("activationCount", doc.get("activation_count", 0)),
            retrieval_count=doc.get("retrievalCount", doc.get("retrieval_count", 0)),
            is_ghost=doc.get("isGhost", doc.get("is_ghost", False)),
            gist=doc.get("gist"),
            synthesized_from=doc.get("synthesizedFrom", doc.get("synthesized_from", [])),
            vector_id=doc.get("vectorId", doc.get("vector_id")),
            emotional_context=EmotionalContext.from_camel_dict(emo) if emo else None,
            empathy_metrics=EmpathyMetrics.from_camel_dict(emp) if emp else None,
            created_at=str(
                doc.get("createdAt", doc.get("created_at", doc.get("timestamp", datetime.now(UTC).isoformat())))
            ),
            updated_at=doc.get("updatedAt", doc.get("updated_at")),
            accessed_at=doc.get("accessedAt", doc.get("accessed_at")),
            last_retrieved_at=doc.get("lastRetrievedAt", doc.get("last_retrieved_at")),
        )

    # -------------------------------------------------------------------------
    # Cross-language camelCase JSON (for REST APIs)
    # -------------------------------------------------------------------------

    def to_api_dict(self) -> dict[str, Any]:
        """Serialize to camelCase dict for REST API responses."""
        return self.to_mongo_doc()


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


class CreateMemoryInput(BaseModel):
    """Input for creating a new memory."""

    content: str = Field(..., min_length=1, max_length=100_000)
    user_id: str = Field(..., max_length=128)
    tenant_id: str = Field("default", max_length=64)
    bank_id: str = Field("default")
    scope: MemoryScope = Field(MemoryScope.SESSION)
    retention: RetentionPolicy = Field(RetentionPolicy.SHORT_TERM)
    category: str = Field("general")
    tags: list[str] = Field(default_factory=list)
    importance: float = Field(0.5, ge=0.0, le=1.0)
    source_service: SourceService = Field(SourceService.FORESIGHT)
    emotional_context: EmotionalContext | None = Field(None)
    empathy_metrics: EmpathyMetrics | None = Field(None)

    def to_unified(self) -> UnifiedMemory:
        return UnifiedMemory.create(
            content=self.content,
            user_id=self.user_id,
            tenant_id=self.tenant_id,
            bank_id=self.bank_id,
            scope=self.scope,
            retention=self.retention,
            category=self.category,
            tags=self.tags,
            importance=self.importance,
            source_service=self.source_service,
            emotional_context=self.emotional_context,
            empathy_metrics=self.empathy_metrics,
        )


class UpdateMemoryInput(BaseModel):
    """Input for updating an existing memory. All fields optional."""

    content: str | None = Field(None, max_length=100_000)
    scope: MemoryScope | None = None
    retention: RetentionPolicy | None = None
    category: str | None = None
    tags: list[str] | None = None
    importance: float | None = Field(None, ge=0.0, le=1.0)
    emotional_context: EmotionalContext | None = None
    empathy_metrics: EmpathyMetrics | None = None
