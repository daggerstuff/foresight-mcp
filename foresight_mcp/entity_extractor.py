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
from typing import Any, Literal

logger = logging.getLogger("foresight_entity_extractor")

EntityType = Literal["person", "place", "concept", "event", "emotion", "object"]
RelationshipType = Literal[
    "mentions", "located_at", "experienced", "caused",
    "relates_to", "contradicts", "supports", "part_of", "created"
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

    def __post_init__(self):
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
    def from_dict(cls, data: dict) -> 'Entity':
        """Create from dictionary."""
        return cls(
            id=data["id"],
            name=data["name"],
            entity_type=data["entity_type"],  # type: ignore
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

    def __post_init__(self):
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
    def from_dict(cls, data: dict) -> 'Relationship':
        """Create from dictionary."""
        return cls(
            source_entity_id=data["source_entity_id"],
            target_entity_id=data["target_entity_id"],
            relationship_type=data["relationship_type"],  # type: ignore
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

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-3-haiku-20240307",
        max_tokens: int = 1024,
        temperature: float = 0.1,
    ):
        """
        Initialize entity extractor.

        Args:
            api_key: Anthropic API key (from env if not provided)
            model: Model to use for extraction
            max_tokens: Max tokens for response
            temperature: Temperature for generation (low for consistency)
        """
        self.api_key = api_key or ""
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature

    def _generate_entity_id(self, name: str, entity_type: str) -> str:
        """Generate deterministic entity ID for deduplication."""
        normalized = name.lower().strip().replace(" ", "_")
        hash_part = hashlib.sha256(f"{entity_type}:{normalized}".encode()).hexdigest()[:12]
        return f"entity_{hash_part}"

    async def extract(self, content: str) -> ExtractionResult:
        """
        Extract entities and relationships from text.

        Uses LLM extraction when an API key is available, falling back
        to rule-based extraction otherwise or on LLM failure.

        Args:
            content: Text to analyze

        Returns:
            ExtractionResult with entities and relationships
        """
        if not content.strip():
            return ExtractionResult(entities=[], relationships=[])

        api_key = self.api_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
        if api_key:
            try:
                return await self.extract_with_llm(content)
            except Exception as e:
                logger.warning(f"LLM extraction failed, falling back to rules: {e}")

        return self._extract_rules_based(content)

    def _extract_rules_based(self, content: str) -> ExtractionResult:
        """
        Rule-based extraction as fallback (no API call).

        This is a simplified implementation that extracts:
        - Common emotion words
        - Simple patterns

        In production, replace with actual LLM-based extraction.
        """
        entities: list[Entity] = []
        relationships: list[Relationship] = []

        # Common emotion patterns
        emotion_patterns = {
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

        content_lower = content.lower()

        # Tokenize for negation detection
        negation_words = {"not", "n't", "no", "never", "neither", "nor", "without", "hardly", "barely"}
        tokens = re.findall(r"\w+|\w+'\w+|[^\w\s]", content_lower)

        def _is_negated(idx: int) -> bool:
            start = max(0, idx - 3)
            return any(t in negation_words for t in tokens[start:idx])

        # Extract emotion entities (with negation handling)
        for emotion, props in emotion_patterns.items():
            match = re.search(rf"\b{re.escape(emotion)}\b", content_lower)
            if not match:
                continue
            token_idx = next((i for i, t in enumerate(tokens) if t == emotion), None)
            negated = token_idx is not None and _is_negated(token_idx)
            effective_name = f"not_{emotion}" if negated else emotion
            effective_props = {"intensity": "negated", "negates": emotion} if negated else props
            entity = Entity(
                id=self._generate_entity_id(effective_name, "emotion"),
                name=effective_name,
                entity_type="emotion",
                description=f"{'Negated ' if negated else ''}Emotion mentioned in text",
                properties=effective_props,
                confidence=0.6 if negated else 0.7,
            )
            entities.append(entity)

        # Extract concept entities (common therapeutic concepts)
        concept_patterns = {
            "therapy": {"category": "treatment"},
            "CBT": {"category": "technique"},
            "meditation": {"category": "practice"},
            "mindfulness": {"category": "practice"},
            "sleep": {"category": "health"},
            "work": {"category": "life_area"},
            "family": {"category": "relationship"},
            "health": {"category": "life_area"},
        }

        for concept, props in concept_patterns.items():
            if re.search(rf"\b{re.escape(concept.lower())}\b", content_lower):
                entity = Entity(
                    id=self._generate_entity_id(concept, "concept"),
                    name=concept,
                    entity_type="concept",
                    description="Concept mentioned in text",
                    properties=props,
                    confidence=0.6,
                )
                entities.append(entity)

        # Generate relationships between co-occurring entities
        concept_entities = [e for e in entities if e.entity_type == "concept"]
        emotion_entities = [e for e in entities if e.entity_type == "emotion"]
        for ce in concept_entities:
            for ee in emotion_entities:
                relationships.append(
                    Relationship(
                        source_entity_id=ce.id,
                        target_entity_id=ee.id,
                        relationship_type="relates_to",
                        confidence=0.5,
                    )
                )
        for i, ea in enumerate(emotion_entities):
            for eb in emotion_entities[i + 1:]:
                relationships.append(
                    Relationship(
                        source_entity_id=ea.id,
                        target_entity_id=eb.id,
                        relationship_type="relates_to",
                        confidence=0.4,
                    )
                )

        # Limit relationships to top 20 by confidence
        relationships.sort(key=lambda r: r.confidence, reverse=True)
        relationships = relationships[:20]

        return ExtractionResult(entities=entities, relationships=relationships)

    async def extract_with_llm(self, content: str) -> ExtractionResult:
        """
        Extract using LLM (production implementation).

        Enable by setting the OPENAI_API_KEY or ANTHROPIC_API_KEY environment
        variable and passing it as the ``api_key`` argument to EntityExtractor.
        When enabled, call this method instead of ``extract()`` which currently
        delegates to the rule-based fallback.

        Args:
            content: Text to analyze

        Returns:
            ExtractionResult with entities and relationships
        """
        import httpx

        try:
            prompt = self.ENTITY_EXTRACTION_PROMPT.format(text=content[:3000]) # Truncate if too long

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
            json_text = data["content"][0]["text"]

            # Strip markdown code fencing if present
            json_text = re.sub(r'^```(?:json)?\s*\n?', '', json_text)
            json_text = re.sub(r'\n?```\s*$', '', json_text)
            json_text = json_text.strip()

            # Parse JSON
            parsed = json.loads(json_text)

            entities = [
                Entity(
                    id=self._generate_entity_id(e["name"], e["type"]),
                    name=e["name"],
                    entity_type=e["type"], # type: ignore
                    description=e.get("description"),
                    properties=e.get("properties", {}),
                    confidence=1.0,
                )
                for e in parsed.get("entities", [])
            ]

            # Build name->id map from extracted entities for correct relationship IDs
            entity_name_to_id = {e.name.lower(): e.id for e in entities}

            relationships = []
            for r in parsed.get("relationships", []):
                src_name = r["source"].lower()
                tgt_name = r["target"].lower()
                src_id = entity_name_to_id.get(src_name) or self._generate_entity_id(r["source"], "concept")
                tgt_id = entity_name_to_id.get(tgt_name) or self._generate_entity_id(r["target"], "concept")
                relationships.append(
                    Relationship(
                        source_entity_id=src_id,
                        target_entity_id=tgt_id,
                        relationship_type=r["type"], # type: ignore
                        confidence=r.get("confidence", 1.0),
                    )
                )

            # Limit relationships to top 20 by confidence
            relationships.sort(key=lambda r: r.confidence, reverse=True)
            relationships = relationships[:20]

            return ExtractionResult(entities=entities, relationships=relationships)

        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code
            if status_code in (401, 403):
                logger.error(f"LLM auth error ({status_code}): check API key. Falling back to rules.")
            elif status_code == 429:
                logger.warning(f"LLM rate limited (429): backing off. Falling back to rules.")
            else:
                logger.warning(f"LLM HTTP error ({status_code}): {e}. Falling back to rules.")
            return self._extract_rules_based(content)

        except Exception as e:
            logger.warning(f"LLM extraction failed, falling back to rules: {e}")
            return self._extract_rules_based(content)


# Global instance management
_entity_extractor: EntityExtractor | None = None
_entity_extractor_lock = threading.Lock()


def get_entity_extractor(
    api_key: str | None = None,
    model: str = "claude-3-haiku-20240307",
) -> EntityExtractor:
    """Get or create global entity extractor instance (thread-safe)."""
    global _entity_extractor
    with _entity_extractor_lock:
        if _entity_extractor is None:
            _entity_extractor = EntityExtractor(api_key=api_key, model=model)
    return _entity_extractor


def reset_entity_extractor() -> None:
    """Reset global entity extractor (for testing)."""
    global _entity_extractor
    with _entity_extractor_lock:
        _entity_extractor = None
