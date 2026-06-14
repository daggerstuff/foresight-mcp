"""
Consumer Group for Kafka Event Processing

Consumes events from Kafka topics for processing.
Supports:
- Consumer group coordination
- Offset management
- Event replay from specific offsets
- Dead letter queue processing
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Optional kafka dependency
try:
    from kafka import KafkaConsumer, TopicPartition

    HAS_KAFKA = True
except Exception:
    KafkaConsumer = None
    TopicPartition = None
    HAS_KAFKA = False


@dataclass
class ConsumerRecord:
    """A single record from Kafka."""

    topic: str
    partition: int
    offset: int
    key: str | None
    value: dict[str, Any]
    timestamp: datetime
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class ConsumerStats:
    """Statistics for consumer processing."""

    records_processed: int = 0
    records_failed: int = 0
    last_offset: dict[int, int] = field(default_factory=dict)  # partition -> offset
    start_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def uptime(self) -> float:
        """Get uptime in seconds."""
        return (datetime.now(timezone.utc) - self.start_time).total_seconds()

    @property
    def records_per_second(self) -> float:
        """Get average records processed per second."""
        uptime = self.uptime
        if uptime == 0:
            return 0
        return self.records_processed / uptime


@dataclass
class ConsumerGroupConfig:
    """Configuration for Kafka consumer group."""

    bootstrap_servers: str = "localhost:9092"
    group_id: str = "foresight-consumer"
    topics: list[str] | None = None
    auto_commit: bool = True
    auto_commit_interval: int = 5000  # ms
    max_poll_records: int = 500
    session_timeout: int = 30000  # ms
    heartbeat_interval: int = 10000  # ms


class ConsumerState:
    """Manages consumer state (offsets, etc.)."""

    def __init__(self, state_file: str | None = None):
        """Initialize consumer state.

        Args:
            state_file: Path to store state (default: ~/.foresight/consumer_state.json)
        """
        if state_file is None:
            state_file = str(Path.home() / ".foresight" / "consumer_state.json")

        # Validate state_file path to prevent directory traversal
        state_path = Path(state_file).expanduser().resolve()
        foresight_dir = Path.home() / ".foresight"
        if not str(state_path).startswith(str(foresight_dir)):
            raise ValueError(f"state_file must be under {foresight_dir}")

        self.state_file = state_file
        self._offsets: dict[str, dict[int, int]] = {}  # topic -> {partition -> offset}
        self._load_state()

    def _load_state(self) -> None:
        """Load state from file."""
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file) as f:
                    data = json.load(f)
                    self._offsets = {
                        topic: {int(k): v for k, v in partitions.items()}
                        for topic, partitions in data.get("offsets", {}).items()
                    }
        except Exception:
            self._offsets = {}

    def save_state(self) -> None:
        """Save state to file."""
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            with open(self.state_file, "w") as f:
                json.dump({"offsets": self._offsets}, f, indent=2)
        except Exception as eh_err:
            logger.error(f"Error handler failed: {eh_err}", exc_info=True)

    def get_offset(self, topic: str, partition: int) -> int | None:
        """Get last committed offset for topic/partition."""
        return self._offsets.get(topic, {}).get(partition)

    def set_offset(self, topic: str, partition: int, offset: int) -> None:
        """Set offset for topic/partition."""
        if topic not in self._offsets:
            self._offsets[topic] = {}
        self._offsets[topic][partition] = offset

    def get_all_offsets(self) -> dict[str, dict[int, int]]:
        """Get all offsets."""
        return self._offsets.copy()


class KafkaConsumerGroup:
    """
    Kafka consumer group for processing events.

    Consumes events from Kafka topics and processes them.
    """

    def __init__(
        self,
        config: ConsumerGroupConfig | None = None,
        **overrides,
    ):
        """Initialize Kafka consumer group.

        Args:
            config: Configuration for the consumer group. If None, uses defaults.
            **overrides: Override specific configuration fields.
        """
        # Start with default config
        if config is None:
            config = ConsumerGroupConfig()

        # Apply overrides
        config_dict = {
            "bootstrap_servers": config.bootstrap_servers,
            "group_id": config.group_id,
            "topics": config.topics,
            "auto_commit": config.auto_commit,
            "auto_commit_interval": config.auto_commit_interval,
            "max_poll_records": config.max_poll_records,
            "session_timeout": config.session_timeout,
            "heartbeat_interval": config.heartbeat_interval,
        }
        config_dict.update(overrides)

        self.bootstrap_servers = config_dict["bootstrap_servers"]
        self.group_id = config_dict["group_id"]
        self.topics = config_dict["topics"] or []
        self.auto_commit = config_dict["auto_commit"]
        self.auto_commit_interval = config_dict["auto_commit_interval"]
        self.max_poll_records = config_dict["max_poll_records"]
        self.session_timeout = config_dict["session_timeout"]
        self.heartbeat_interval = config_dict["heartbeat_interval"]

        self._consumer = None
        self._running = False
        self._state = ConsumerState()
        self._stats = ConsumerStats()
        self._handlers: list[Callable[[ConsumerRecord], None]] = []
        self._error_handlers: list[Callable[[Exception, ConsumerRecord], None]] = []

    def _get_consumer(self):
        """Lazy-load Kafka consumer."""
        if self._consumer is None:
            if not HAS_KAFKA or KafkaConsumer is None:
                raise RuntimeError("kafka-python is not installed. Install it with 'pip install kafka-python'")

            self._consumer = KafkaConsumer(
                *self.topics,
                bootstrap_servers=self.bootstrap_servers.split(","),
                group_id=self.group_id,
                auto_offset_reset="earliest",
                enable_auto_commit=self.auto_commit,
                auto_commit_interval_ms=self.auto_commit_interval,
                max_poll_records=self.max_poll_records,
                session_timeout_ms=self.session_timeout,
                heartbeat_interval_ms=self.heartbeat_interval,
                value_deserializer=lambda v: json.loads(v.decode("utf-8")) if v else None,
            )

        return self._consumer

    def add_handler(self, handler: Callable[[ConsumerRecord], None]) -> None:
        """Add event handler."""
        self._handlers.append(handler)

    def add_error_handler(self, handler: Callable[[Exception, ConsumerRecord], None]) -> None:
        """Add error handler."""
        self._error_handlers.append(handler)

    def start(self, topics: list[str] | None = None) -> None:
        """
        Start consuming from topics.

        Args:
            topics: Optional list of topics to consume from
        """
        if topics:
            self.topics = topics

        consumer = self._get_consumer()
        self._running = True
        backoff_seconds = 1

        while self._running:
            try:
                records = consumer.poll(timeout_ms=1000)
                for _topic_partition, messages in records.items():
                    for message in messages:
                        record = ConsumerRecord(
                            topic=message.topic,
                            partition=message.partition,
                            offset=message.offset,
                            key=message.key,
                            value=message.value,
                            timestamp=datetime.fromtimestamp(message.timestamp / 1000, tz=timezone.utc),
                            headers={k: v.decode() for k, v in message.headers} if message.headers else {},
                        )
                        self._process_record(record)
                        self._stats.records_processed += 1
                        self._state.set_offset(message.topic, message.partition, message.offset)

                self._state.save_state()

            except Exception as e:
                self._stats.records_failed += 1
                if not self._running:
                    break
                logger.error(f"Consumer poll failed, retrying in {backoff_seconds}s: {e}")

                time.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 60)  # Exponential backoff, max 60s

    def _process_record(self, record: ConsumerRecord) -> None:
        """Process a single record."""
        for handler in self._handlers:
            try:
                handler(record)
            except Exception as e:
                for error_handler in self._error_handlers:
                    try:
                        error_handler(e, record)
                    except Exception as eh_err:
                        logger.error(f"Error handler failed: {eh_err}", exc_info=True)

    def stop(self) -> None:
        """Stop consuming."""
        self._running = False
        if self._consumer:
            self._consumer.close()
            self._consumer = None
        self._state.save_state()

    def get_stats(self) -> ConsumerStats:
        """Get consumer statistics."""
        return self._stats

    def seek_to_beginning(self, topic: str, partition: int = 0) -> None:
        """Seek to beginning of topic/partition."""
        consumer = self._get_consumer()
        if not HAS_KAFKA or TopicPartition is None:
            raise RuntimeError("kafka-python is not installed")

        tp = TopicPartition(topic, partition)
        consumer.assign([tp])
        consumer.seek_to_beginning(tp)

    def seek_to_end(self, topic: str, partition: int = 0) -> None:
        """Seek to end of topic/partition."""
        consumer = self._get_consumer()
        if not HAS_KAFKA or TopicPartition is None:
            raise RuntimeError("kafka-python is not installed")

        tp = TopicPartition(topic, partition)
        consumer.assign([tp])
        consumer.seek_to_end(tp)

    def seek_to_offset(self, topic: str, partition: int, offset: int) -> None:
        """Seek to specific offset."""
        consumer = self._get_consumer()
        if not HAS_KAFKA or TopicPartition is None:
            raise RuntimeError("kafka-python is not installed")

        tp = TopicPartition(topic, partition)
        consumer.assign([tp])
        consumer.seek(tp, offset)
