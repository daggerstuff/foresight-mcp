"""
Stream Producer for Event Bus

Publishes events to Kafka/Kinesis topics for stream processing.
Supports:
- Kafka topic publishing with schema validation
- Topic naming convention: foresight.{env}.{entity}.{event}
- Dead letter queue for failed messages
- Consumer group coordination
"""
from __future__ import annotations
import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from enum import Enum


class StreamType(str, Enum):
    """Stream backend type."""
    KAFKA = "kafka"
    KINESIS = "kinesis"
    MOCK = "mock"  # For testing


@dataclass
class StreamEvent:
    """Event for stream publishing."""
    event_type: str
    entity_id: str
    payload: Dict[str, Any]
    timestamp: str
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata: Dict[str, Any] = {}

    def to_dict(self) -> dict:
        return {
            "event_type": self.event_type,
            "entity_id": self.entity_id,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }


class StreamProducer(ABC):
    """Abstract base class for stream producers."""

    @abstractmethod
    def publish(self, topic: str, event: StreamEvent) -> bool:
        """Publish event to topic. Returns True if successful."""
        pass

    @abstractmethod
    def publish_batch(self, events: List[tuple[str, StreamEvent]]) -> int:
        """Publish batch of events. Returns count of successful publishes."""
        pass

    @abstractmethod
    def close(self) -> None:
        """Close producer connection."""
        pass


class MockProducer(StreamProducer):
    """Mock producer for testing - logs events but doesn't publish."""

    def __init__(self):
        self.published: List[tuple[str, StreamEvent]] = []
        self.failures: List[tuple[str, StreamEvent]] = []

    def publish(self, topic: str, event: StreamEvent) -> bool:
        self.published.append((topic, event))
        return True

    def publish_batch(self, events: List[tuple[str, StreamEvent]]) -> int:
        self.published.extend(events)
        return len(events)

    def close(self) -> None:
        pass


class KafkaProducer(StreamProducer):
    """
    Kafka stream producer.

    Publishes events to Kafka topics with schema validation.
    Topic naming: foresight.{env}.{entity}.{event}
    """

    def __init__(
        self,
        bootstrap_servers: str = "localhost:9092",
        environment: str = "dev",
        dlq_topic: str = "foresight.dlq",
        enable_schema_validation: bool = True,
    ):
        """Initialize Kafka producer.

        Args:
            bootstrap_servers: Kafka bootstrap servers (comma-separated)
            environment: Environment name (dev, staging, prod)
            dlq_topic: Dead letter queue topic for failed messages
            enable_schema_validation: Validate events against schema
        """
        self.bootstrap_servers = bootstrap_servers
        self.environment = environment
        self.dlq_topic = dlq_topic
        self.enable_schema_validation = enable_schema_validation
        self._producer = None
        self._failed_messages: List[Dict[str, Any]] = []

        # Lazy import to avoid requiring kafka-python when not used
        self._kafka = None

    def _get_producer(self):
        """Lazy-load Kafka producer."""
        if self._producer is None:
            try:
                from kafka import KafkaProducer
                self._kafka = KafkaProducer
                self._producer = KafkaProducer(
                    bootstrap_servers=self.bootstrap_servers.split(','),
                    value_serializer=lambda v: json.dumps(v).encode('utf-8'),
                    key_serializer=lambda k: k.encode('utf-8') if k else None,
                    retries=3,
                    retry_backoff_ms=100,
                )
            except ImportError:
                raise ImportError(
                    "kafka-python not installed. Install with: pip install kafka-python"
                )
        return self._producer

    def _get_topic(self, entity: str, event: str) -> str:
        """Generate topic name with naming convention."""
        # Normalize entity and event for topic naming
        entity_clean = entity.replace('.', '_').replace(' ', '_').lower()
        event_clean = event.replace('.', '_').replace(' ', '_').lower()
        return f"foresight.{self.environment}.{entity_clean}.{event_clean}"

    def _validate_event(self, event: StreamEvent) -> bool:
        """Validate event against schema."""
        required_fields = ['event_type', 'entity_id', 'payload', 'timestamp']
        event_dict = event.to_dict()
        return all(field in event_dict for field in required_fields)

    def _send_to_dlq(self, topic: str, event: StreamEvent, error: str) -> None:
        """Send failed message to dead letter queue."""
        dlq_event = StreamEvent(
            event_type="dlq.original",
            entity_id="dlq",
            payload={
                "original_topic": topic,
                "original_event": event.to_dict(),
                "error": error,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        try:
            producer = self._get_producer()
            future = producer.send(self.dlq_topic, value=dlq_event.to_dict())
            # Don't block on DLQ send
        except Exception:
            # If DLQ fails, store locally for later recovery
            self._failed_messages.append({
                "topic": self.dlq_topic,
                "event": dlq_event.to_dict(),
                "error": error,
            })

    def publish(self, topic: str, event: StreamEvent) -> bool:
        """
        Publish event to Kafka topic.

        Args:
            topic: Topic name (or use entity.event format)
            event: Event to publish

        Returns:
            True if successful, False otherwise
        """
        try:
            producer = self._get_producer()

            # Validate event
            if self.enable_schema_validation and not self._validate_event(event):
                self._send_to_dlq(topic, event, "Schema validation failed")
                return False

            # Send to Kafka
            event_dict = event.to_dict()
            key = event.entity_id
            future = producer.send(topic, value=event_dict, key=key)

            # Wait for send to complete (with timeout)
            future.get(timeout=10)
            return True

        except Exception as e:
            # Send to DLQ on failure
            self._send_to_dlq(topic, event, str(e))
            return False

    def publish_batch(self, events: List[tuple[str, StreamEvent]]) -> int:
        """
        Publish batch of events to Kafka.

        Args:
            events: List of (topic, event) tuples

        Returns:
            Number of successfully published events
        """
        if not events:
            return 0

        try:
            producer = self._get_producer()

            # Send all events
            futures = []
            for topic, event in events:
                if self.enable_schema_validation and not self._validate_event(event):
                    self._send_to_dlq(topic, event, "Schema validation failed")
                    continue

                event_dict = event.to_dict()
                key = event.entity_id
                future = producer.send(topic, value=event_dict, key=key)
                futures.append((topic, event, future))

            # Wait for all to complete
            success_count = 0
            for topic, event, future in futures:
                try:
                    future.get(timeout=10)
                    success_count += 1
                except Exception:
                    self._send_to_dlq(topic, event, "Batch send failed")

            return success_count

        except Exception:
            # Store failed batch
            for topic, event in events:
                self._failed_messages.append({
                    "topic": topic,
                    "event": event.to_dict(),
                })
            return 0

    def flush(self) -> None:
        """Flush all pending messages."""
        if self._producer:
            self._producer.flush()

    def close(self) -> None:
        """Close producer connection."""
        if self._producer:
            self._producer.close()
            self._producer = None


class KinesisProducer(StreamProducer):
    """
    AWS Kinesis stream producer.

    Publishes events to Kinesis streams.
    """

    def __init__(
        self,
        stream_name: str = "foresight-events",
        region: str = "us-east-1",
        environment: str = "dev",
    ):
        """Initialize Kinesis producer.

        Args:
            stream_name: Kinesis stream name
            region: AWS region
            environment: Environment name
        """
        self.stream_name = stream_name
        self.region = region
        self.environment = environment
        self._client = None
        self._failed_messages: List[Dict[str, Any]] = []

    def _get_client(self):
        """Lazy-load Kinesis client."""
        if self._client is None:
            try:
                import boto3
                self._client = boto3.client('kinesis', region_name=self.region)
            except ImportError:
                raise ImportError(
                    "boto3 not installed. Install with: pip install boto3"
                )
        return self._client

    def _get_partition_key(self, event: StreamEvent) -> str:
        """Generate partition key for Kinesis."""
        return event.entity_id or "default"

    def publish(self, topic: str, event: StreamEvent) -> bool:
        """Publish event to Kinesis stream."""
        try:
            client = self._get_client()

            event_dict = event.to_dict()
            partition_key = self._get_partition_key(event)

            # Put record
            response = client.put_record(
                StreamName=self.stream_name,
                Data=json.dumps(event_dict).encode('utf-8'),
                PartitionKey=partition_key,
            )

            return response['ResponseMetadata']['HTTPStatusCode'] == 200

        except Exception:
            self._failed_messages.append({
                "stream": self.stream_name,
                "event": event.to_dict(),
            })
            return False

    def publish_batch(self, events: List[tuple[str, StreamEvent]]) -> int:
        """Publish batch of events to Kinesis."""
        if not events:
            return 0

        try:
            client = self._get_client()

            # Batch into groups of 500 (Kinesis limit)
            batch_size = 500
            success_count = 0

            for i in range(0, len(events), batch_size):
                batch = events[i:i + batch_size]
                records = []

                for topic, event in batch:
                    records.append({
                        'Data': json.dumps(event.to_dict()).encode('utf-8'),
                        'PartitionKey': self._get_partition_key(event),
                    })

                # Send batch
                response = client.put_records(
                    StreamName=self.stream_name,
                    Records=records,
                )

                success_count += len(records)

            return success_count

        except Exception:
            for topic, event in events:
                self._failed_messages.append({
                    "stream": self.stream_name,
                    "event": event.to_dict(),
                })
            return 0

    def close(self) -> None:
        """Close producer connection."""
        self._client = None


# =============================================================================
# Stream Producer Factory
# =============================================================================

def create_stream_producer(
    stream_type: Optional[StreamType] = None,
    environment: str = "dev",
) -> StreamProducer:
    """
    Create stream producer based on configuration.

    Args:
        stream_type: Type of stream (Kafka, Kinesis, Mock)
        environment: Environment name

    Returns:
        StreamProducer instance
    """
    if stream_type is None:
        # Auto-detect from environment
        kafka_servers = os.environ.get('KAFKA_BOOTSTRAP_SERVERS')
        kinesis_stream = os.environ.get('KINESIS_STREAM_NAME')

        if kafka_servers:
            stream_type = StreamType.KAFKA
        elif kinesis_stream:
            stream_type = StreamType.KINESIS
        else:
            stream_type = StreamType.MOCK

    if stream_type == StreamType.KAFKA:
        bootstrap_servers = os.environ.get('KAFKA_BOOTSTRAP_SERVERS', 'localhost:9092')
        return KafkaProducer(
            bootstrap_servers=bootstrap_servers,
            environment=environment,
        )
    elif stream_type == StreamType.KINESIS:
        stream_name = os.environ.get('KINESIS_STREAM_NAME', 'foresight-events')
        region = os.environ.get('AWS_REGION', 'us-east-1')
        return KinesisProducer(
            stream_name=stream_name,
            region=region,
            environment=environment,
        )
    else:
        return MockProducer()


# =============================================================================
# EventBus Integration
# =============================================================================

class StreamPublisher:
    """
    Publishes EventBus events to stream.

    Integrates with EventBus to publish all events to Kafka/Kinesis.
    """

    def __init__(
        self,
        producer: StreamProducer,
        environment: str = "dev",
    ):
        """Initialize stream publisher.

        Args:
            producer: Stream producer instance
            environment: Environment name
        """
        self.producer = producer
        self.environment = environment
        self._published_count = 0

    def publish_event(self, event) -> bool:
        """
        Publish event from EventBus to stream.

        Args:
            event: Event from EventBus

        Returns:
            True if published successfully
        """
        topic = f"foresight.{self.environment}.memory.{event.event_type.value}"
        stream_event = StreamEvent(
            event_type=event.event_type.value,
            entity_id=event.entity_id,
            payload=event.payload,
            timestamp=event.timestamp.isoformat(),
            metadata=event.metadata,
        )

        success = self.producer.publish(topic, stream_event)
        if success:
            self._published_count += 1
        return success

    @property
    def published_count(self) -> int:
        """Get count of successfully published events."""
        return self._published_count

    def close(self) -> None:
        """Close producer connection."""
        self.producer.close()
