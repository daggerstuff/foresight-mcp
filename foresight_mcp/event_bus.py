"""
Event Bus for Memory Operations
Event sourcing with full audit trail for all memory operations.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, TypeVar
import json
import sqlite3
from pathlib import Path
from enum import Enum


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

    # Anomaly detection
    ANOMALY_DETECTED = "anomaly.detected"

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
    payload: Dict[str, Any]
    metadata: Dict[str, Any] = field(default_factory=dict)

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

    def __init__(self, db_path: Optional[str] = None):
        """Initialize event store.

        Args:
            db_path: Path to SQLite database (default: ~/.foresight/events.db)
        """
        if db_path is None:
            db_path = str(Path.home() / ".foresight" / "events.db")

        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database schema."""
        db_path = Path(self.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                actor TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                metadata TEXT DEFAULT '{}'
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp)")
        conn.commit()
        conn.close()

    def append(self, event: Event) -> None:
        """Append event to store."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO events (id, event_type, timestamp, actor, entity_id, payload, metadata) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event.id,
                event.event_type.value,
                event.timestamp.isoformat(),
                event.actor,
                event.entity_id,
                json.dumps(event.payload),
                json.dumps(event.metadata),
            )
        )
        conn.commit()
        conn.close()

    def get_by_entity(self, entity_id: str, limit: int = 100, offset: int = 0) -> List[Event]:
        """Get events by entity ID."""
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT * FROM events WHERE entity_id = ? ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (entity_id, limit, offset)
        ).fetchall()
        conn.close()
        return [self._row_to_event(row) for row in rows]

    def get_by_type(self, event_type: EventType, limit: int = 100, offset: int = 0) -> List[Event]:
        """Get events by type."""
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT * FROM events WHERE event_type = ? ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (event_type.value, limit, offset)
        ).fetchall()
        conn.close()
        return [self._row_to_event(row) for row in rows]

    def get_by_time_range(
        self,
        start: datetime,
        end: datetime,
        limit: int = 100,
        offset: int = 0
    ) -> List[Event]:
        """Get events by time range."""
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT * FROM events WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (start.isoformat(), end.isoformat(), limit, offset)
        ).fetchall()
        conn.close()
        return [self._row_to_event(row) for row in rows]

    def get_all(self, limit: int = 100, offset: int = 0) -> List[Event]:
        """Get all events (paginated)."""
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT * FROM events ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()
        conn.close()
        return [self._row_to_event(row) for row in rows]

    def _row_to_event(self, row: tuple) -> Event:
        """Convert database row to Event."""
        return Event(
            id=row[0],
            event_type=EventType(row[1]),
            timestamp=datetime.fromisoformat(row[2]),
            actor=row[3],
            entity_id=row[4],
            payload=json.loads(row[5]),
            metadata=json.loads(row[6]),
        )


# =============================================================================
# Event Bus
# =============================================================================

T = TypeVar('T', bound=Event)


class EventBus:
    """
    Event bus for publishing and subscribing to events.

    Supports:
    - Synchronous event handlers
    - Event filtering by type
    - Event persistence
    - Error handling with continue-on-error
    """

    def __init__(self, store: Optional[EventStore] = None):
        """Initialize event bus.

        Args:
            store: Event store for persistence (default: in-memory store)
        """
        self._handlers: Dict[EventType, List[EventHandler]] = {}
        self._store = store

    def publish(self, event: Event) -> None:
        """
        Publish an event.

        All registered handlers for the event type will be called.
        """
        # Persist event
        if self._store:
            self._store.append(event)

        # Call handlers
        handlers = self._handlers.get(event.event_type, [])
        for handler in handlers:
            try:
                handler(event)
            except Exception as e:
                # Continue on error - don't let one handler break others
                pass  # Could log error here

    def subscribe(self, event_type: EventType, handler: EventHandler) -> None:
        """Subscribe to an event type."""
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

_event_bus: Optional[EventBus] = None
_event_store: Optional[EventStore] = None


def get_event_bus() -> EventBus:
    """Get the global event bus instance."""
    global _event_bus, _event_store
    if _event_bus is None:
        _event_store = EventStore()
        _event_bus = EventBus(_event_store)
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
    event_type: EventType,
    actor: str,
    entity_id: str,
    payload: Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None
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
