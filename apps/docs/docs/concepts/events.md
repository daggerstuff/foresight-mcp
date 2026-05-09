 ---
sidebar_label: Events
title: Event Sourcing
---

# Event Sourcing

Foresight uses event sourcing to maintain a complete audit trail of all memory
operations.

## Event Types

| Event              | Description             |
| ------------------ | ----------------------- |
| `memory.stored`    | New memory created      |
| `memory.retrieved` | Memory accessed         |
| `memory.updated`   | Memory modified         |
| `memory.deleted`   | Memory removed          |
| `block.created`    | Block schema registered |
| `block.updated`    | Block content changed   |
| `block.deleted`    | Block removed           |
| `anomaly.detected` | Anomaly flagged         |
| `system.error`     | System error occurred   |

## Event Structure

```typescript
interface Event {
  id: string // Unique event ID
  eventType: EventType
  timestamp: string // ISO timestamp
  actor: string // User/system ID
  entityId: string // Related memory/block ID
  payload: Record<string, unknown>
  metadata: Record<string, unknown>
}
```

## Event Persistence

Events are stored in SQLite at `~/.foresight/events.db`:

```sql
CREATE TABLE events (
  id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  actor TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  metadata TEXT DEFAULT '{}'
);
```

## Subscribing to Events

### Python

```python
from foresight_mcp.event_bus import get_event_bus, EventType

event_bus = get_event_bus()

def on_memory_stored(event):
    print(f"Memory stored: {event.entity_id}")

event_bus.subscribe(EventType.MEMORY_STORED, on_memory_stored)
```

### WebSocket

```typescript
import { ForesightWebSocketClient, EventType } from '@foresight/core'

const client = new ForesightWebSocketClient({
  url: 'ws://localhost:8765',
})

await client.connect()

// Subscribe to events
await client.subscribe({
  eventTypes: [EventType.MemoryStored, EventType.MemoryUpdated],
})

// Handle events
client.onMessage((message) => {
  if (message.type === 'event') {
    console.log('Event received:', message.payload)
  }
})
```

## Querying Events

```python
from foresight_mcp.event_bus import get_event_bus

event_bus = get_event_bus()
store = event_bus._store

# Get events by entity
events = store.get_by_entity("memory:abc123")

# Get events by type
events = store.get_by_type(EventType.MEMORY_STORED)

# Get events by time range
from datetime import datetime, timedelta
start = datetime.now() - timedelta(hours=1)
events = store.get_by_time_range(start, datetime.now())
```

## Use Cases

### Audit Trail

```python
# Track all memory operations for compliance
events = store.get_by_entity(memory_id)
for event in events:
    log_audit(event)
```

### Projections

```python
# Build a timeline view
timeline = build_timeline(events)
```

### Triggers

```python
# Trigger actions on specific events
if event.event_type == EventType.MEMORY_STORED:
    await notify_subscribers(event)
```

## Related

- [Memory](./memory) - Memory lifecycle
- [Hooks](./hooks) - Event-driven extensions
- [WebSocket](./websocket) - Real-time subscriptions
