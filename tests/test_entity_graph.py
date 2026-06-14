"""
Tests for entity extraction and graph store.
"""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from foresight_mcp.entity_extractor import Entity, EntityExtractor, Relationship
from foresight_mcp.graph_store import GraphStore


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    fd, path = tempfile.mkstemp(suffix=".db")
    yield path
    import os

    os.close(fd)
    os.unlink(path)


@pytest.fixture
def graph_store(temp_db):
    """Create graph store with initialized schema."""
    return GraphStore(temp_db)


class TestEntityExtractor:
    """Test entity extraction logic."""

    def test_generate_entity_id(self):
        """Entity IDs should be deterministic."""
        extractor = EntityExtractor()
        id1 = extractor._generate_entity_id("anxiety", "emotion")
        id2 = extractor._generate_entity_id("anxiety", "emotion")
        assert id1 == id2

    def test_extract_rules_based_emotions(self):
        """Rule-based extraction should find emotion entities."""
        extractor = EntityExtractor()
        result = extractor._extract_rules_based("I feel very anxious today")

        assert len(result.entities) > 0
        emotion_entities = [e for e in result.entities if e.entity_type == "emotion"]
        assert len(emotion_entities) > 0
        # Should find either "anxiety" or "anxious" as emotion
        emotion_names = [e.name for e in emotion_entities]
        assert "anxiety" in emotion_names or "anxious" in emotion_names

    def test_extract_rules_based_concepts(self):
        """Rule-based extraction should find concept entities."""
        extractor = EntityExtractor()
        result = extractor._extract_rules_based("I do CBT and meditation for stress")

        concept_entities = [e for e in result.entities if e.entity_type == "concept"]
        assert len(concept_entities) >= 2  # Should find CBT and meditation

    def test_extract_negation(self):
        """Negated emotions should produce 'not_X' entities."""
        extractor = EntityExtractor()
        result = extractor._extract_rules_based("I am not anxious about this")

        emotion_entities = [e for e in result.entities if e.entity_type == "emotion"]
        names = [e.name for e in emotion_entities]
        assert "not_anxious" in names
        negated = [e for e in emotion_entities if e.name == "not_anxious"]
        assert negated[0].properties.get("negates") == "anxious"

    def test_extract_generates_relationships(self):
        """Relationships should link concepts to emotions."""
        extractor = EntityExtractor()
        result = extractor._extract_rules_based("My work causes me a lot of stress")

        assert len(result.relationships) > 0
        # Should have a concept-emotion relationship
        rel_types = {r.relationship_type for r in result.relationships}
        assert "relates_to" in rel_types

    def test_extract_empty_content(self):
        """Empty content should return empty result."""
        extractor = EntityExtractor()
        result = extractor._extract_rules_based("")

        assert len(result.entities) == 0
        assert len(result.relationships) == 0


class TestGraphStore:
    """Test graph store operations."""

    def test_upsert_entity(self, graph_store):
        """Should insert and update entities."""
        entity = Entity(
            id="entity_test123",
            name="anxiety",
            entity_type="emotion",
            description="Feeling of worry",
            properties={"intensity": "high"},
        )

        result_id = graph_store.upsert_entity(entity, "test_user")
        assert result_id == "entity_test123"

        # Verify retrieval
        retrieved = graph_store.get_entity("entity_test123", "test_user")
        assert retrieved is not None
        assert retrieved.name == "anxiety"

    def test_upsert_entity_duplicates(self, graph_store):
        """Should update on duplicate."""
        entity1 = Entity(id="entity_test123", name="anxiety", entity_type="emotion", description="First description")
        graph_store.upsert_entity(entity1, "test_user")

        entity2 = Entity(id="entity_test456", name="anxiety", entity_type="emotion", description="Updated description")
        graph_store.upsert_entity(entity2, "test_user")

        # Should still have same ID (deterministic)
        retrieved = graph_store.get_entity("entity_test123", "test_user")
        assert retrieved is not None
        assert retrieved.description == "Updated description"

    def test_get_entities_by_type(self, graph_store):
        """Should filter entities by type."""
        graph_store.upsert_entity(Entity(id="entity1", name="anxiety", entity_type="emotion"), "test_user")
        graph_store.upsert_entity(Entity(id="entity2", name="therapy", entity_type="concept"), "test_user")

        emotions = graph_store.get_entities_by_type("test_user", "emotion")
        concepts = graph_store.get_entities_by_type("test_user", "concept")

        assert len(emotions) == 1
        assert len(concepts) == 1

    def test_add_relationship(self, graph_store):
        """Should add relationships between entities."""
        graph_store.upsert_entity(Entity(id="entity_person", name="John", entity_type="person"), "test_user")
        graph_store.upsert_entity(Entity(id="entity_emotion", name="anxiety", entity_type="emotion"), "test_user")

        relationship = Relationship(
            source_entity_id="entity_person",
            target_entity_id="entity_emotion",
            relationship_type="experienced",
            confidence=0.9,
        )

        graph_store.add_relationship(relationship, "test_user")

        # Verify retrieval
        relationships = graph_store.get_relationships("entity_person", "test_user")
        assert len(relationships) == 1
        assert relationships[0].relationship_type == "experienced"

    def test_traverse_graph(self, graph_store):
        """Should traverse connected entities."""
        # Create a simple graph: Person -> experiences -> Anxiety -> relates_to -> Stress
        graph_store.upsert_entity(Entity(id="entity_person", name="John", entity_type="person"), "test_user")
        graph_store.upsert_entity(Entity(id="entity_anxiety", name="anxiety", entity_type="emotion"), "test_user")
        graph_store.upsert_entity(Entity(id="entity_stress", name="stress", entity_type="emotion"), "test_user")

        graph_store.add_relationship(
            Relationship(
                source_entity_id="entity_person", target_entity_id="entity_anxiety", relationship_type="experienced"
            ),
            "test_user",
        )

        graph_store.add_relationship(
            Relationship(
                source_entity_id="entity_anxiety", target_entity_id="entity_stress", relationship_type="relates_to"
            ),
            "test_user",
        )

        # Traverse from person
        result = graph_store.traverse_graph("entity_person", "test_user", max_depth=2)

        assert len(result.nodes) >= 2  # Should find anxiety and stress
        assert len(result.edges) >= 2  # Should find both relationships

    def test_link_memory_to_entities(self, graph_store):
        """Should link memories to entities."""
        graph_store.upsert_entity(Entity(id="entity_anxiety", name="anxiety", entity_type="emotion"), "test_user")

        graph_store.link_memory_to_entities(memory_id="mem_123", entity_ids=["entity_anxiety"], user_id="test_user")

        # Verify link
        memories = graph_store.get_memories_for_entity("entity_anxiety", "test_user")
        assert "mem_123" in memories

    def test_find_related_memories(self, graph_store):
        """Should find memories via graph traversal."""
        graph_store.upsert_entity(Entity(id="entity_anxiety", name="anxiety", entity_type="emotion"), "test_user")
        graph_store.upsert_entity(Entity(id="entity_stress", name="stress", entity_type="emotion"), "test_user")

        # Link memories
        graph_store.link_memory_to_entities("mem_1", ["entity_anxiety"], "test_user")
        graph_store.link_memory_to_entities("mem_2", ["entity_stress"], "test_user")

        # Create relationship between entities
        graph_store.add_relationship(
            Relationship(
                source_entity_id="entity_anxiety", target_entity_id="entity_stress", relationship_type="relates_to"
            ),
            "test_user",
        )

        # Find related memories
        related = graph_store.find_related_memories("entity_anxiety", "test_user", depth=1)

        assert "mem_2" in related  # Should find via relationship


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
