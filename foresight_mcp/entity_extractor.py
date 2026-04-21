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
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Literal
import hashlib

logger = logging.getLogger("foresight_entity_extractor")

EntityType = Literal['person', 'place', 'concept', 'event', 'emotion', 'object']
RelationshipType = Literal[
    'mentions', 'located_at', 'experienced', 'caused',
    'relates_to', 'contradicts', 'supports', 'part_of', 'created'
]


@dataclass
class Entity:
    """Represents a named entity extracted from text."""
    id: str
    name: str
    entity_type: EntityType
    description: Optional[str] = None
    properties: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            'id': self.id,
            'name': self.name,
            'entity_type': self.entity_type,
            'description': self.description,
            'properties': self.properties,
            'confidence': self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'Entity':
        """Create from dictionary."""
        return cls(
            id=data['id'],
            name=data['name'],
            entity_type=data['entity_type'],  # type: ignore
            description=data.get('description'),
            properties=data.get('properties', {}),
            confidence=data.get('confidence', 1.0),
        )


@dataclass
class Relationship:
    """Represents a relationship between two entities."""
    source_entity_id: str
    target_entity_id: str
    relationship_type: RelationshipType
    confidence: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            'source_entity_id': self.source_entity_id,
            'target_entity_id': self.target_entity_id,
            'relationship_type': self.relationship_type,
            'confidence': self.confidence,
            'metadata': self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'Relationship':
        """Create from dictionary."""
        return cls(
            source_entity_id=data['source_entity_id'],
            target_entity_id=data['target_entity_id'],
            relationship_type=data['relationship_type'],  # type: ignore
            confidence=data.get('confidence', 1.0),
            metadata=data.get('metadata', {}),
        )


@dataclass
class ExtractionResult:
    """Result of entity and relationship extraction."""
    entities: List[Entity]
    relationships: List[Relationship]

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            'entities': [e.to_dict() for e in self.entities],
            'relationships': [r.to_dict() for r in self.relationships],
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
        api_key: Optional[str] = None,
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

        Args:
            content: Text to analyze

        Returns:
            ExtractionResult with entities and relationships
        """
        if not content.strip():
            return ExtractionResult(entities=[], relationships=[])

        # For now, use simple rule-based extraction as fallback
        # In production, this would call the LLM API
        return self._extract_rules_based(content)

    def _extract_rules_based(self, content: str) -> ExtractionResult:
        """
        Rule-based extraction as fallback (no API call).

        This is a simplified implementation that extracts:
        - Common emotion words
        - Simple patterns

        In production, replace with actual LLM-based extraction.
        """
        entities: List[Entity] = []
        relationships: List[Relationship] = []

        # Common emotion patterns
        emotion_patterns = {
            'anxiety': {'intensity': 'moderate'},
            'anxious': {'intensity': 'moderate'},
            'stress': {'intensity': 'moderate'},
            'depression': {'intensity': 'high'},
            'happy': {'intensity': 'positive'},
            'sad': {'intensity': 'negative'},
            'angry': {'intensity': 'negative'},
            'fear': {'intensity': 'high'},
            'joy': {'intensity': 'positive'},
            'anger': {'intensity': 'negative'},
            'excitement': {'intensity': 'positive'},
        }

        content_lower = content.lower()

        # Extract emotion entities
        for emotion, props in emotion_patterns.items():
            if re.search(rf'\b{re.escape(emotion)}\b', content_lower):
                entity = Entity(
                    id=self._generate_entity_id(emotion, 'emotion'),
                    name=emotion,
                    entity_type='emotion',
                    description=f"Emotion mentioned in text",
                    properties=props,
                    confidence=0.7,
                )
                entities.append(entity)

        # Extract concept entities (common therapeutic concepts)
        concept_patterns = {
            'therapy': {'category': 'treatment'},
            'CBT': {'category': 'technique'},
            'meditation': {'category': 'practice'},
            'mindfulness': {'category': 'practice'},
            'sleep': {'category': 'health'},
            'work': {'category': 'life_area'},
            'family': {'category': 'relationship'},
            'health': {'category': 'life_area'},
        }

        for concept, props in concept_patterns.items():
            if re.search(rf'\b{re.escape(concept.lower())}\b', content_lower):
                entity = Entity(
                    id=self._generate_entity_id(concept, 'concept'),
                    name=concept,
                    entity_type='concept',
                    description=f"Concept mentioned in text",
                    properties=props,
                    confidence=0.6,
                )
                entities.append(entity)

        # Create simple relationships (person experienced emotion)
        # This would be more sophisticated with LLM extraction
        if len(entities) >= 2:
            # Link first person-like entity to emotions
            for entity in entities:
                if entity.entity_type == 'emotion':
                    # Create a generic "relates_to" relationship
                    pass  # Would need a person entity to link to

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
        try:
            import httpx

            prompt = self.ENTITY_EXTRACTION_PROMPT.format(text=content[:3000])  # Truncate if too long

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

                # Parse JSON
                parsed = json.loads(json_text)

                entities = [
                    Entity(
                        id=self._generate_entity_id(e["name"], e["type"]),
                        name=e["name"],
                        entity_type=e["type"],  # type: ignore
                        description=e.get("description"),
                        properties=e.get("properties", {}),
                        confidence=1.0,
                    )
                    for e in parsed.get("entities", [])
                ]

                relationships = [
                    Relationship(
                        source_entity_id=self._generate_entity_id(r["source"], "temp"),
                        target_entity_id=self._generate_entity_id(r["target"], "temp"),
                        relationship_type=r["type"],  # type: ignore
                        confidence=r.get("confidence", 1.0),
                    )
                    for r in parsed.get("relationships", [])
                ]

                return ExtractionResult(entities=entities, relationships=relationships)

        except Exception as e:
            logger.warning(f"LLM extraction failed, falling back to rules: {e}")
            return self._extract_rules_based(content)


# Global instance management
_entity_extractor: Optional[EntityExtractor] = None
_entity_extractor_lock = threading.Lock()


def get_entity_extractor(
    api_key: Optional[str] = None,
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
