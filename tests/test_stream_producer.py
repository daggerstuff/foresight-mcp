"""Tests for stream producer integration."""

import pytest
from foresight_mcp.event_bus import memory_stored
from foresight_mcp.stream_producer import (
    KafkaProducer,
    KinesisProducer,
    MockProducer,
    StreamEvent,
    StreamPublisher,
    StreamType,
    create_stream_producer,
)


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


class TestKafkaProducer:
    """Test KafkaProducer with mocked kafka client."""

    def test_kafka_producer_init(self):
        """Test KafkaProducer initialization."""
        producer = KafkaProducer(
            bootstrap_servers="localhost:9092",
            environment="test",
            send_timeout=60,
        )
        assert producer.bootstrap_servers == "localhost:9092"
        assert producer.environment == "test"
        assert producer.send_timeout == 60
        assert producer._producer is None  # Lazy loading

    def test_kafka_producer_topic_naming(self):
        """Test topic name generation."""
        producer = KafkaProducer(environment="test")
        assert producer._get_topic("memory", "stored") == "foresight.test.memory.stored"
        # Dots are valid in Kafka topic names, spaces become underscores
        assert producer._get_topic("my.entity", "my.event") == "foresight.test.my.entity.my.event"
        assert producer._get_topic("entity/with/slashes", "event") == "foresight.test.entity_with_slashes.event"

    def test_kafka_producer_schema_validation(self):
        """Test event schema validation."""
        producer = KafkaProducer()
        valid_event = StreamEvent(
            event_type="memory.stored",
            entity_id="test-123",
            payload={"key": "value"},
            timestamp="2026-04-16T00:00:00Z",
        )
        assert producer._validate_event(valid_event) is True
        bad_payload_event = StreamEvent(
            event_type="memory.stored",
            entity_id="test-123",
            payload="not a dict",
            timestamp="2026-04-16T00:00:00Z",
        )
        assert producer._validate_event(bad_payload_event) is False
        empty_type_event = StreamEvent(
            event_type="",
            entity_id="test-123",
            payload={},
            timestamp="2026-04-16T00:00:00Z",
        )
        assert producer._validate_event(empty_type_event) is False

    def test_kafka_producer_send_to_dlq(self):
        """Test DLQ send on failure."""
        producer = KafkaProducer()
        event = StreamEvent(
            event_type="memory.stored",
            entity_id="test",
            payload={},
            timestamp="2026-04-16T00:00:00Z",
        )
        producer._send_to_dlq("test-topic", event, "test error")


class TestKinesisProducer:
    """Test KinesisProducer with mocked boto3 client."""

    def test_kinesis_producer_init(self):
        """Test KinesisProducer initialization."""
        producer = KinesisProducer(
            stream_name="test-stream",
            region="us-west-2",
            environment="test",
        )
        assert producer.stream_name == "test-stream"
        assert producer.region == "us-west-2"
        assert producer.environment == "test"

    def test_kinesis_producer_partition_key(self):
        """Test partition key generation."""
        producer = KinesisProducer()
        event = StreamEvent(
            event_type="memory.stored",
            entity_id="test-123",
            payload={},
            timestamp="2026-04-16T00:00:00Z",
        )
        assert producer._get_partition_key(event) == "test-123"
        event_no_id = StreamEvent(
            event_type="memory.stored",
            entity_id=None,
            payload={},
            timestamp="2026-04-16T00:00:00Z",
        )
        assert producer._get_partition_key(event_no_id) == "default"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
