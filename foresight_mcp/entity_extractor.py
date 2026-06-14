"""
Entity Extraction Service for Foresight Memory System.

Extracts entities and relationships from memory content using LLM-based analysis.

Entity Types:
- person: Named individuals
- place: Locations, venues, areas
- concept: Abstract ideas, theories, principles
- event: Occurrences, happenings, incidents
- emotion: Feelings, emotional states
- object: Physical items, artifacts

Relationship Types:
- mentions: Entity A mentions Entity B
- located_at: Entity is at location
- experienced: Person experienced emotion/event
- caused: Event/thing caused another event
- relates_to: General semantic relationship
- contradicts: Entities have opposing views
- supports: Entities support each other
- part_of: Entity is part of larger entity
- created: Entity created another entity
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

logger = logging.getLogger("foresight_entity_extractor")

# Optional httpx dependency for external API calls
try:
    import httpx

    HAS_HTTPX = True
except Exception:
    httpx = None
    HAS_HTTPX = False

EntityType = Literal["person", "place", "concept", "event", "emotion", "object"]
RelationshipType = Literal[
    "mentions", "located_at", "experienced", "caused", "relates_to", "contradicts", "supports", "part_of", "created"
]


@dataclass
class Entity:
    """Represents a named entity extracted from text."""

    id: str
    name: str
    entity_type: EntityType
    description: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0

    def __post_init__(self) -> None:
        self.confidence = max(0.0, min(1.0, self.confidence))

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "entity_type": self.entity_type,
            "description": self.description,
            "properties": self.properties,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Entity:
        """Create from dictionary."""
        return cls(
            id=data["id"],
            name=data["name"],
            entity_type=data["entity_type"],  # type: ignore[arg-type]
            description=data.get("description"),
            properties=data.get("properties", {}),
            confidence=data.get("confidence", 1.0),
        )


@dataclass
class Relationship:
    """Represents a relationship between two entities."""

    source_entity_id: str
    target_entity_id: str
    relationship_type: RelationshipType
    confidence: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.confidence = max(0.0, min(1.0, self.confidence))

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "source_entity_id": self.source_entity_id,
            "target_entity_id": self.target_entity_id,
            "relationship_type": self.relationship_type,
            "confidence": self.confidence,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Relationship:
        """Create from dictionary."""
        return cls(
            source_entity_id=data["source_entity_id"],
            target_entity_id=data["target_entity_id"],
            relationship_type=data["relationship_type"],  # type: ignore[arg-type]
            confidence=data.get("confidence", 1.0),
            metadata=data.get("metadata", {}),
        )


@dataclass
class ExtractionResult:
    """Result of entity and relationship extraction."""

    entities: list[Entity]
    relationships: list[Relationship]

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "entities": [e.to_dict() for e in self.entities],
            "relationships": [r.to_dict() for r in self.relationships],
        }


class EntityExtractor:
    """
    LLM-based entity and relationship extractor.

    Extracts structured entities and relationships from unstructured text.
    Falls back to rule-based extraction when no API key is available.

    All regex patterns are pre-compiled at class definition time to avoid
    repeated compilation overhead on every extract() call.
    """

    ENTITY_EXTRACTION_PROMPT = """Extract entities and relationships from this text.

Entity Types:
- person: Named individuals (use proper names)
- place: Locations, venues, areas
- concept: Abstract ideas, theories, principles
- event: Occurrences, happenings, incidents
- emotion: Feelings, emotional states
- object: Physical items, artifacts

Relationship Types:
- mentions: Entity A mentions Entity B
- located_at: Entity is at location
- experienced: Person experienced emotion/event
- caused: Event/thing caused another event
- relates_to: General semantic relationship
- contradicts: Entities have opposing views
- supports: Entities support each other
- part_of: Entity is part of larger entity
- created: Entity created another entity

Output JSON format (no markdown, just raw JSON):
{{
  "entities": [
    {{"name": "John", "type": "person", "description": "User mentioned", "properties": {{"role": "participant"}}}},
    {{"name": "anxiety", "type": "emotion", "description": "Emotional state", "properties": {{"intensity": "high"}}}}
  ],
  "relationships": [
    {{"source": "John", "target": "anxiety", "type": "experienced", "confidence": 0.9}}
  ]
}}

Only extract entities explicitly mentioned. Be conservative with confidence scores.
If no clear entities/relationships exist, return empty lists.

Text: {text}

Output (raw JSON only, no markdown):
"""

    # ---------------------------------------------------------------------------
    # Class-level pre-compiled patterns
    # ---------------------------------------------------------------------------

    _EMOTION_PATTERNS: ClassVar[dict[str, dict]] = {
        "anxiety": {"intensity": "moderate"},
        "anxious": {"intensity": "moderate"},
        "stress": {"intensity": "moderate"},
        "depression": {"intensity": "high"},
        "happy": {"intensity": "positive"},
        "sad": {"intensity": "negative"},
        "angry": {"intensity": "negative"},
        "fear": {"intensity": "high"},
        "joy": {"intensity": "positive"},
        "anger": {"intensity": "negative"},
        "excitement": {"intensity": "positive"},
    }
    _CONCEPT_PATTERNS: ClassVar[dict[str, dict]] = {
        "therapy": {"category": "treatment"},
        "CBT": {"category": "technique"},
        "meditation": {"category": "practice"},
        "mindfulness": {"category": "practice"},
        "sleep": {"category": "health"},
        "work": {"category": "life_area"},
        "family": {"category": "relationship"},
        "health": {"category": "life_area"},
    }

    # {term: compiled pattern} — built once at class definition time
    _EMOTION_RE: ClassVar[dict[str, re.Pattern[str]]] = {
        term: re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE) for term in _EMOTION_PATTERNS
    }
    _CONCEPT_RE: ClassVar[dict[str, re.Pattern[str]]] = {
        term: re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE) for term in _CONCEPT_PATTERNS
    }

    # Strip markdown code fences from LLM responses
    _FENCE_START_RE: re.Pattern[str] = re.compile(r"^```(?:json)?\s*\n?", re.MULTILINE)
    _FENCE_END_RE: re.Pattern[str] = re.compile(r"\n?```\s*$", re.MULTILINE)

    # Tokenizer for negation detection
    _TOKEN_RE: re.Pattern[str] = re.compile(r"\w+|\w+'\w+|[^\w\s]")

    _NEGATION_WORDS: frozenset[str] = frozenset(
        {"not", "n't", "no", "never", "neither", "nor", "without", "hardly", "barely"}
    )

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-3-haiku-20240307",
        max_tokens: int = 1024,
        temperature: float = 0.1,
    ) -> None:
        """
        Initialize entity extractor.

        Args:
            api_key: Anthropic API key (reads ANTHROPIC_API_KEY / OPENAI_API_KEY
                     env vars if not provided).
            model: Model to use for LLM extraction.
            max_tokens: Max tokens for LLM response.
            temperature: Sampling temperature (low = more deterministic).
        """
        self.api_key = api_key or ""
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature

    def _generate_entity_id(self, name: str, entity_type: str) -> str:
        """Generate a deterministic entity ID for deduplication."""
        normalized = name.lower().strip().replace(" ", "_")
        hash_part = hashlib.sha256(f"{entity_type}:{normalized}".encode()).hexdigest()[:12]
        return f"entity_{hash_part}"

    async def extract(self, content: str) -> ExtractionResult:
        """
        Extract entities and relationships from text.

        Uses LLM extraction when an API key is available, falling back
        to rule-based extraction otherwise or on LLM failure.

        Args:
            content: Text to analyze.

        Returns:
            ExtractionResult with entities and relationships.
        """
        if not content.strip():
            return ExtractionResult(entities=[], relationships=[])

        api_key = self.api_key or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if api_key:
            try:
                return await self.extract_with_llm(content)
            except Exception as exc:
                logger.warning("LLM extraction failed, falling back to rules: %s", exc)

        return self._extract_rules_based(content)

    def _extract_rules_based(self, content: str) -> ExtractionResult:
        """
        Rule-based extraction fallback (no API call).

        Uses pre-compiled class-level patterns for emotion and concept
        detection with simple negation handling.
        """
        entities: list[Entity] = []
        relationships: list[Relationship] = []

        content_lower = content.lower()
        tokens = self._TOKEN_RE.findall(content_lower)

        def _is_negated(idx: int) -> bool:
            start = max(0, idx - 3)
            return any(t in self._NEGATION_WORDS for t in tokens[start:idx])

        # Extract emotion entities
        for emotion, props in self._EMOTION_PATTERNS.items():
            if not self._EMOTION_RE[emotion].search(content_lower):
                continue
            token_idx = next((i for i, t in enumerate(tokens) if t == emotion), None)
            negated = token_idx is not None and _is_negated(token_idx)
            effective_name = f"not_{emotion}" if negated else emotion
            effective_props = {"intensity": "negated", "negates": emotion} if negated else props
            entities.append(
                Entity(
                    id=self._generate_entity_id(effective_name, "emotion"),
                    name=effective_name,
                    entity_type="emotion",
                    description=f"{'Negated ' if negated else ''}Emotion mentioned in text",
                    properties=effective_props,
                    confidence=0.6 if negated else 0.7,
                )
            )

        # Extract concept entities
        for concept, props in self._CONCEPT_PATTERNS.items():
            if self._CONCEPT_RE[concept].search(content_lower):
                entities.append(
                    Entity(
                        id=self._generate_entity_id(concept, "concept"),
                        name=concept,
                        entity_type="concept",
                        description="Concept mentioned in text",
                        properties=props,
                        confidence=0.6,
                    )
                )

        # Generate relationships between co-occurring entities
        concept_entities = [e for e in entities if e.entity_type == "concept"]
        emotion_entities = [e for e in entities if e.entity_type == "emotion"]

        relationships.extend(
            Relationship(
                source_entity_id=ce.id,
                target_entity_id=ee.id,
                relationship_type="relates_to",
                confidence=0.5,
            )
            for ce in concept_entities
            for ee in emotion_entities
        )
        relationships.extend(
            Relationship(
                source_entity_id=ea.id,
                target_entity_id=eb.id,
                relationship_type="relates_to",
                confidence=0.4,
            )
            for i, ea in enumerate(emotion_entities)
            for eb in emotion_entities[i + 1 :]
        )

        relationships.sort(key=lambda r: r.confidence, reverse=True)
        return ExtractionResult(entities=entities, relationships=relationships[:20])

    async def extract_with_llm(self, content: str) -> ExtractionResult:
        """
        Extract entities using the Anthropic API.

        Requires ANTHROPIC_API_KEY to be set (or passed to __init__).
        Falls back to rule-based extraction on any API error.

        Args:
            content: Text to analyze.

        Returns:
            ExtractionResult with entities and relationships.
        """
        if not HAS_HTTPX or httpx is None:
            raise RuntimeError("httpx is not installed. Install it with 'pip install httpx'")

        try:
            max_context = int(os.environ.get("FORESIGHT_ENTITY_EXTRACTION_MAX_CONTEXT", "1500"))
            prompt = self.ENTITY_EXTRACTION_PROMPT.format(text=content[:max_context])

            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "Content-Type": "application/json",
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01",
                    },
                    json={
                        "model": self.model,
                        "max_tokens": self.max_tokens,
                        "temperature": self.temperature,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                response.raise_for_status()

            data = response.json()
            json_text: str = data["content"][0]["text"]

            # Strip markdown code fences using pre-compiled patterns
            json_text = self._FENCE_START_RE.sub("", json_text)
            json_text = self._FENCE_END_RE.sub("", json_text)
            json_text = json_text.strip()

            parsed = json.loads(json_text)

            entities = [
                Entity(
                    id=self._generate_entity_id(e["name"], e["type"]),
                    name=e["name"],
                    entity_type=e["type"],  # type: ignore[arg-type]
                    description=e.get("description"),
                    properties=e.get("properties", {}),
                    confidence=1.0,
                )
                for e in parsed.get("entities", [])
            ]

            entity_name_to_id = {e.name.lower(): e.id for e in entities}

            relationships: list[Relationship] = []
            for r in parsed.get("relationships", []):
                src_id = entity_name_to_id.get(r["source"].lower()) or self._generate_entity_id(r["source"], "concept")
                tgt_id = entity_name_to_id.get(r["target"].lower()) or self._generate_entity_id(r["target"], "concept")
                relationships.append(
                    Relationship(
                        source_entity_id=src_id,
                        target_entity_id=tgt_id,
                        relationship_type=r["type"],  # type: ignore[arg-type]
                        confidence=r.get("confidence", 1.0),
                    )
                )

            relationships.sort(key=lambda r: r.confidence, reverse=True)
            return ExtractionResult(entities=entities, relationships=relationships[:20])

        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if status_code in (401, 403):
                logger.error("LLM auth error (%d): check API key. Falling back to rules.", status_code)
            elif status_code == 429:
                logger.warning("LLM rate limited (429). Falling back to rules.")
            else:
                logger.warning("LLM HTTP error (%d): %s. Falling back to rules.", status_code, exc)
            return self._extract_rules_based(content)

        except Exception as exc:
            logger.warning("LLM extraction failed, falling back to rules: %s", exc)
            return self._extract_rules_based(content)


# ---------------------------------------------------------------------------
# Global instance management (thread-safe)
# ---------------------------------------------------------------------------


class _EntityExtractorSingleton:
    """Module-level singleton for EntityExtractor."""

    _instance: EntityExtractor | None = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls, api_key: str | None = None, model: str = "claude-3-haiku-20240307") -> EntityExtractor:
        """Get or create the global entity extractor instance (thread-safe)."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = EntityExtractor(api_key=api_key, model=model)
            return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the global entity extractor (for testing)."""
        with cls._lock:
            cls._instance = None


def get_entity_extractor(
    api_key: str | None = None,
    model: str = "claude-3-haiku-20240307",
) -> EntityExtractor:
    """Get or create the global entity extractor instance (thread-safe)."""
    return _EntityExtractorSingleton.get_instance(api_key, model)


def reset_entity_extractor() -> None:
    """Reset the global entity extractor (for testing)."""
    _EntityExtractorSingleton.reset()
