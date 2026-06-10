"""
Event Bus for Memory Operations
Event sourcing with full audit trail for all memory operations.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from .connection_pool import get_pool
from .tenant_context import get_current_tenant_id

logger = logging.getLogger(__name__)

# =============================================================================
# Event Types
# =============================================================================


class EventType(str, Enum):
    """Types of events in the system."""

    # Memory lifecycle
    MEMORY_STORED = "memory.stored"
    MEMORY_RETRIEVED = "memory.retrieved"
    MEMORY_UPDATED = "memory.updated"
    MEMORY_DELETED = "memory.deleted"

    # Block lifecycle
    BLOCK_CREATED = "block.created"
    BLOCK_UPDATED = "block.updated"
    BLOCK_DELETED = "block.deleted"

    # Curation lifecycle
    CURATION_CREATED = "curation.created"
    CURATION_UPDATED = "curation.updated"
    CURATION_COMPLETED = "curation.completed"
    CURATION_FAILED = "curation.failed"
    CURATION_CANCELED = "curation.canceled"

    # Anomaly detection
    ANOMALY_DETECTED = "anomaly.detected"

    # Event publication
    EVENT_PUBLISHED = "event.published"

    # System
    SYSTEM_ERROR = "system.error"


# =============================================================================
# Event Base Class
# =============================================================================


@dataclass
class Event:
    """
    Base event class.

    All events have:
    - Unique ID
    - Event type
    - Timestamp
    - Actor (user/system)
    - Entity ID (what the event is about)
    - Payload (event-specific data)
    - Metadata (correlation, causation)
    """

    id: str
    event_type: EventType
    timestamp: datetime
    actor: str
    entity_id: str
    payload: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Convert to dictionary for storage."""
        return {
            "id": self.id,
            "event_type": self.event_type.value,
            "timestamp": self.timestamp.isoformat(),
            "actor": self.actor,
            "entity_id": self.entity_id,
            "payload": self.payload,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Event:
        """Create from dictionary."""
        return cls(
            id=data["id"],
            event_type=EventType(data["event_type"]),
            timestamp=datetime.fromisoformat(data["timestamp"]),
            actor=data["actor"],
            entity_id=data["entity_id"],
            payload=data["payload"],
            metadata=data.get("metadata", {}),
        )


# =============================================================================
# Event Handlers
# =============================================================================

EventHandler = Callable[[Event], None]


# =============================================================================
# Event Store (SQLite-based)
# =============================================================================


class EventStore:
    """
    Persistent event store using SQLite.

    Stores all events in append-only log.
    Supports temporal queries and event replay.
    """

    def __init__(self, db_path: str | None = None):
        """Initialize event store.

        Args:
            db_path: Path to SQLite database (default: ~/.foresight/events.db)
        """
        if db_path is None:
            db_path = str(Path.home() / ".foresight" / "events.db")

        self.db_path = db_path
        self._init_db()

    def _connect(self):
        """Get a pooled connection."""
        return get_pool(self.db_path).acquire()

    def _init_db(self) -> None:
        """Initialize database schema."""
        db_path = Path(self.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        conn = self._connect()
        try:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                event_type TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                actor TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                metadata TEXT DEFAULT '{}'
            )
            """)
            # Migration: add tenant_id if table exists without it
            try:
                cols = [row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()]
                if cols and "tenant_id" not in cols:
                    conn.execute("ALTER TABLE events ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'")
            except Exception:
                pass
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_tenant ON events(tenant_id)")
            conn.commit()
        finally:
            conn.close()

    def append(self, event: Event, tenant_id: str | None = None) -> None:
        """Append event to store."""
        tid = tenant_id or get_current_tenant_id()
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO events (id, tenant_id, event_type, timestamp, actor, entity_id, payload, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.id,
                    tid,
                    event.event_type.value,
                    event.timestamp.isoformat(),
                    event.actor,
                    event.entity_id,
                    json.dumps(event.payload),
                    json.dumps(event.metadata),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_by_entity(
        self, entity_id: str, limit: int = 100, offset: int = 0, tenant_id: str | None = None
    ) -> list[Event]:
        """Get events by entity ID."""
        tid = tenant_id or get_current_tenant_id()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM events WHERE entity_id = ? AND tenant_id = ? ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                (entity_id, tid, limit, offset),
            ).fetchall()
        finally:
            conn.close()
        events = []
        for row in rows:
            event = self._row_to_event(row)
            if event is not None:
                events.append(event)
        return events

    def get_by_type(
        self, event_type: EventType, limit: int = 100, offset: int = 0, tenant_id: str | None = None
    ) -> list[Event]:
        """Get events by type."""
        tid = tenant_id or get_current_tenant_id()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM events WHERE event_type = ? AND tenant_id = ? ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                (event_type.value, tid, limit, offset),
            ).fetchall()
        finally:
            conn.close()
        events = []
        for row in rows:
            event = self._row_to_event(row)
            if event is not None:
                events.append(event)
        return events

    def get_by_time_range(
        self, start: datetime, end: datetime, limit: int = 100, offset: int = 0, tenant_id: str | None = None
    ) -> list[Event]:
        """Get events by time range."""
        tid = tenant_id or get_current_tenant_id()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM events WHERE timestamp BETWEEN ? AND ? AND tenant_id = ? ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                (start.isoformat(), end.isoformat(), tid, limit, offset),
            ).fetchall()
        finally:
            conn.close()
        events = []
        for row in rows:
            event = self._row_to_event(row)
            if event is not None:
                events.append(event)
        return events

    def get_all(self, limit: int = 100, offset: int = 0, tenant_id: str | None = None) -> list[Event]:
        """Get all events (paginated)."""
        tid = tenant_id or get_current_tenant_id()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM events WHERE tenant_id = ? ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                (tid, limit, offset),
            ).fetchall()
        finally:
            conn.close()
        events = []
        for row in rows:
            event = self._row_to_event(row)
            if event is not None:
                events.append(event)
        return events

    def purge_old_events(self, retention_days: int = 90, tenant_id: str | None = None) -> int:
        """
        Delete events older than ``retention_days`` to prevent unbounded growth.

        Args:
            retention_days: Events older than this many days are deleted.
            tenant_id: Scope deletion to a specific tenant, or all tenants if None.

        Returns:
            Number of rows deleted.
        """
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        conn = self._connect()
        try:
            if tenant_id:
                cursor = conn.execute(
                    "DELETE FROM events WHERE timestamp < ? AND tenant_id = ?",
                    (cutoff, tenant_id),
                )
            else:
                cursor = conn.execute(
                    "DELETE FROM events WHERE timestamp < ?",
                    (cutoff,),
                )
            deleted = cursor.rowcount
            conn.commit()
            logger.info("Purged %d events older than %d days", deleted, retention_days)
            return deleted
        finally:
            conn.close()

    def _row_to_event(self, row: tuple) -> Event | None:
        """Convert database row to Event, returning None for corrupt rows."""
        try:
            # row[1] is tenant_id (not stored in Event dataclass)
            # Handle both old (7 cols) and new (8 cols) schema
            if len(row) >= 8:
                offset = 1  # tenant_id at row[1]
            else:
                offset = 0
            return Event(
                id=row[0],
                event_type=EventType(row[1 + offset]),
                timestamp=datetime.fromisoformat(row[2 + offset]),
                actor=row[3 + offset],
                entity_id=row[4 + offset],
                payload=json.loads(row[5 + offset]),
                metadata=json.loads(row[6 + offset]),
            )
        except (IndexError, ValueError, KeyError, json.JSONDecodeError) as e:
            logger.warning(f"Skipping corrupt event row: {e}")
            return None


# =============================================================================
# Event Bus
# =============================================================================

T = TypeVar("T", bound=Event)


class EventBus:
    """
    Event bus for publishing and subscribing to events.

    Supports:
    - Synchronous event handlers
    - Event filtering by type
    - Event persistence
    - Error handling with continue-on-error
    - Stream publishing (Kafka/Kinesis)
    """

    def __init__(
        self,
        store: EventStore | None = None,
        stream_publisher: Any | None = None,
    ):
        """Initialize event bus.

        Args:
            store: Event store for persistence (default: in-memory store)
            stream_publisher: Optional StreamPublisher for publishing to Kafka/Kinesis
        """
        self._handlers: dict[EventType, list[EventHandler]] = {}
        self._store = store
        self._stream_publisher = stream_publisher
        self._lock = threading.Lock()

    def set_stream_publisher(self, stream_publisher: Any | None) -> None:
        """Attach or replace the stream publisher after bus initialization."""
        with self._lock:
            self._stream_publisher = stream_publisher

    def publish(self, event: Event) -> None:
        """
        Publish an event.

        All registered handlers for the event type will be called.
        """
        # Persist event
        if self._store:
            self._store.append(event)

        # Publish to stream (Kafka/Kinesis) if configured
        if self._stream_publisher:
            try:
                self._stream_publisher.publish_event(event)
            except Exception as e:
                logger.warning(f"Stream publishing failed: {e}")
                # Don't let stream publishing failures block the event

        # Snapshot handlers under lock, invoke outside to avoid holding lock during I/O
        with self._lock:
            handlers = list(self._handlers.get(event.event_type, []))
        for handler in handlers:
            try:
                handler(event)
            except Exception as e:
                logger.error(f"Event handler failed for {event.event_type}: {e}", exc_info=True)

    def subscribe(self, event_type: EventType, handler: EventHandler) -> None:
        """Subscribe to an event type."""
        with self._lock:
            if event_type not in self._handlers:
                self._handlers[event_type] = []
            self._handlers[event_type].append(handler)

    def unsubscribe(self, event_type: EventType, handler: EventHandler) -> None:
        """Unsubscribe from an event type."""
        if event_type in self._handlers:
            try:
                self._handlers[event_type].remove(handler)
            except ValueError:
                pass  # Handler not found

    def replay(self, entity_id: str, handler: EventHandler) -> None:
        """Replay events for an entity."""
        if not self._store:
            return
        events = self._store.get_by_entity(entity_id)
        for event in events:
            handler(event)


# =============================================================================
# Global Event Bus
# =============================================================================

_event_bus: EventBus | None = None
_event_store: EventStore | None = None


_event_bus_lock = threading.Lock()


def get_event_bus(stream_publisher: Any | None = None) -> EventBus:
    """Get the global event bus instance.

    Args:
        stream_publisher: Optional StreamPublisher for publishing to Kafka/Kinesis
    """
    global _event_bus, _event_store
    with _event_bus_lock:
        if _event_bus is None:
            _event_store = EventStore()
            _event_bus = EventBus(_event_store, stream_publisher)
        elif stream_publisher is not None:
            _event_bus.set_stream_publisher(stream_publisher)
    return _event_bus


def reset_event_bus() -> None:
    """Reset the global event bus (for testing)."""
    global _event_bus, _event_store
    _event_bus = None
    _event_store = None


# =============================================================================
# Event Factory Functions
# =============================================================================


def _make_event(
    event_type: EventType, actor: str, entity_id: str, payload: dict[str, Any], metadata: dict[str, Any] | None = None
) -> Event:
    """Create a new event with standard metadata."""
    import uuid

    return Event(
        id=str(uuid.uuid4()),
        event_type=event_type,
        timestamp=datetime.now(timezone.utc),
        actor=actor,
        entity_id=entity_id,
        payload=payload,
        metadata=metadata or {},
    )


# Memory events
def memory_stored(memory_id: str, content: str, actor: str = "system") -> Event:
    """Emit memory stored event."""
    return _make_event(
        EventType.MEMORY_STORED,
        actor,
        memory_id,
        {"content": content[:100]},  # Truncate for event
    )


def memory_retrieved(memory_id: str, query_context: str = "", actor: str = "system") -> Event:
    """Emit memory retrieved event."""
    return _make_event(
        EventType.MEMORY_RETRIEVED,
        actor,
        memory_id,
        {"query_context": query_context},
    )


def memory_updated(memory_id: str, old_content: str, new_content: str, actor: str = "system") -> Event:
    """Emit memory updated event."""
    return _make_event(
        EventType.MEMORY_UPDATED,
        actor,
        memory_id,
        {"old_content": old_content[:100], "new_content": new_content[:100]},
    )


def memory_deleted(memory_id: str, actor: str = "system") -> Event:
    """Emit memory deleted event."""
    return _make_event(
        EventType.MEMORY_DELETED,
        actor,
        memory_id,
        {},
    )


# Block events
def block_created(block_label: str, content: str, actor: str = "system") -> Event:
    """Emit block created event."""
    return _make_event(
        EventType.BLOCK_CREATED,
        actor,
        block_label,
        {"content": content[:100]},
    )


def block_updated(block_label: str, old_content: str, new_content: str, actor: str = "system") -> Event:
    """Emit block updated event."""
    return _make_event(
        EventType.BLOCK_UPDATED,
        actor,
        block_label,
        {"old_content": old_content[:100], "new_content": new_content[:100]},
    )


def block_deleted(block_label: str, actor: str = "system") -> Event:
    """Emit block deleted event."""
    return _make_event(
        EventType.BLOCK_DELETED,
        actor,
        block_label,
        {},
    )


def curation_status_changed(
    run_id: str,
    status: str,
    payload: dict[str, Any] | None = None,
    actor: str = "system",
) -> Event:
    """Emit curation lifecycle events."""
    event_type_map = {
        "pending": EventType.CURATION_CREATED,
        "running": EventType.CURATION_UPDATED,
        "completed": EventType.CURATION_COMPLETED,
        "failed": EventType.CURATION_FAILED,
        "canceled": EventType.CURATION_CANCELED,
    }
    event_type = event_type_map.get(status, EventType.CURATION_UPDATED)
    event_payload = {"status": status, **(payload or {})}
    return _make_event(event_type, actor, run_id, event_payload)


# Anomaly events
def anomaly_detected(category: str, risk_level: str, actor: str = "system") -> Event:
    """Emit anomaly detected event."""
    return _make_event(
        EventType.ANOMALY_DETECTED,
        actor,
        f"anomaly:{category}",
        {"category": category, "risk_level": risk_level},
    )


# System events
def system_error(error_type: str, message: str, actor: str = "system") -> Event:
    """Emit system error event."""
    return _make_event(
        EventType.SYSTEM_ERROR,
        actor,
        f"error:{error_type}",
        {"error_type": error_type, "message": message},
    )
