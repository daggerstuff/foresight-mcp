"""Tests for stream producer integration."""
import pytest
from foresight_mcp.stream_producer import (
    StreamEvent,
    StreamType,
    MockProducer,
    KafkaProducer,
    KinesisProducer,
    create_stream_producer,
    StreamPublisher,
)
from foresight_mcp.event_bus import Event, EventType, memory_stored


class TestStreamEvent:
    """Test StreamEvent dataclass."""

    def test_create_stream_event(self):
        """Test creating a stream event."""
        event = StreamEvent(
            event_type="memory.stored",
            entity_id="test-123",
            payload={"content": "test"},
            timestamp="2026-04-16T00:00:00Z",
        )
        assert event.event_type == "memory.stored"
        assert event.entity_id == "test-123"
        assert event.payload == {"content": "test"}

    def test_stream_event_to_dict(self):
        """Test converting stream event to dictionary."""
        event = StreamEvent(
            event_type="memory.updated",
            entity_id="test-456",
            payload={"old": "a", "new": "b"},
            timestamp="2026-04-16T00:00:00Z",
            metadata={"actor": "user"},
        )
        result = event.to_dict()
        assert result["event_type"] == "memory.updated"
        assert result["entity_id"] == "test-456"
        assert result["payload"] == {"old": "a", "new": "b"}
        assert result["metadata"] == {"actor": "user"}


class TestMockProducer:
    """Test MockProducer for testing."""

    def test_mock_producer_publish(self):
        """Test mock producer publish."""
        producer = MockProducer()
        event = StreamEvent(
            event_type="memory.stored",
            entity_id="test",
            payload={},
            timestamp="2026-04-16T00:00:00Z",
        )
        result = producer.publish("foresight.dev.memory.stored", event)
        assert result is True
        assert len(producer.published) == 1

    def test_mock_producer_publish_batch(self):
        """Test mock producer batch publish."""
        producer = MockProducer()
        events = [
            ("topic1", StreamEvent("e1", "id1", {}, "2026-04-16T00:00:00Z")),
            ("topic2", StreamEvent("e2", "id2", {}, "2026-04-16T00:00:00Z")),
        ]
        count = producer.publish_batch(events)
        assert count == 2
        assert len(producer.published) == 2


class TestStreamPublisher:
    """Test StreamPublisher integration with EventBus."""

    def test_stream_publisher_publish_event(self):
        """Test publishing event through stream publisher."""
        mock_producer = MockProducer()
        publisher = StreamPublisher(mock_producer, environment="test")

        # Create an event
        event = memory_stored("mem-123", "test content", "user-1")

        # Publish
        result = publisher.publish_event(event)

        assert result is True
        assert publisher.published_count == 1
        assert len(mock_producer.published) == 1

        # Check topic naming
        topic, stream_event = mock_producer.published[0]
        assert "test" in topic  # environment in topic
        assert stream_event.event_type == "memory.stored"

    def test_stream_publisher_topic_naming(self):
        """Test topic naming convention."""
        mock_producer = MockProducer()
        publisher = StreamPublisher(mock_producer, environment="prod")

        event = memory_stored("mem-123", "content", "user")
        publisher.publish_event(event)

        topic, _ = mock_producer.published[0]
        assert topic == "foresight.prod.memory.memory.stored"


class TestCreateStreamProducer:
    """Test stream producer factory."""

    def test_create_mock_producer_default(self):
        """Test creating mock producer by default."""
        producer = create_stream_producer()
        assert isinstance(producer, MockProducer)

    def test_create_explicit_mock(self):
        """Test creating explicit mock producer."""
        producer = create_stream_producer(StreamType.MOCK)
        assert isinstance(producer, MockProducer)


class TestEventBusIntegration:
    """Test EventBus integration with stream producer."""

    def test_event_bus_with_stream_publisher(self):
        """Test EventBus publishes to stream."""
        from foresight_mcp.event_bus import EventBus, EventStore

        mock_producer = MockProducer()
        publisher = StreamPublisher(mock_producer, environment="dev")

        # Create event bus with stream publisher
        store = EventStore()
        bus = EventBus(store, stream_publisher=publisher)

        # Publish event
        event = memory_stored("mem-test", "content", "user")
        bus.publish(event)

        # Should persist to SQLite AND publish to stream
        assert len(mock_producer.published) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
