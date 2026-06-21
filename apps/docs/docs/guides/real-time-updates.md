---
sidebar_label: Real-time Updates
title: Guide - Real-time Updates via WebSocket
---

# Real-time Updates with WebSocket

Subscribe to memory events in real-time.

## Connect

```typescript
import { ForesightWebSocketClient } from '@foresight/core'

const client = new ForesightWebSocketClient({
  url: 'ws://localhost:8765',
  userId: 'my-user-id',
})

await client.connect()
```

## Subscribe to Events

```typescript
// Subscribe to all memory events
const subId = await client.subscribe({
  eventTypes: ['memory.stored', 'memory.updated', 'memory.deleted'],
})

// With entity filter
await client.subscribe({
  eventTypes: ['memory.stored'],
  entityFilter: 'memory:abc123', // Only this memory
})

// Wildcard filter
await client.subscribe({
  eventTypes: ['memory.stored'],
  entityFilter: 'memory:*', // All memories
})
```

## Handle Events

```typescript
client.onMessage((message) => {
  switch (message.type) {
    case 'event':
      console.log('Event received:', {
        type: message.event_type,
        payload: message.payload,
        timestamp: message.timestamp,
      })
      break
    case 'subscribed':
      console.log('Subscribed:', message.subscription_id)
      break
    case 'unsubscribed':
      console.log('Unsubscribed:', message.subscription_id)
      break
  }
})
```

## Unsubscribe

```typescript
await client.unsubscribe(subId)
```

## Reconnection

```typescript
// Auto-reconnects with configurable attempts
const client = new ForesightWebSocketClient({
  url: 'ws://localhost:8765',
  reconnectInterval: 5000, // 5 seconds
  maxReconnectAttempts: 5,
})

// Check connection state
const state = client.getState()
// 'disconnected' | 'connecting' | 'connected' | 'reconnecting'
```

## Python MCP Tools

```python
from foresight_mcp import ws_subscribe, ws_unsubscribe, ws_status

# Subscribe
ws_subscribe(
    subscription_id="my-sub",
    event_types=["memory.stored", "memory.updated"],
    entity_filter="memory:*"
)

# Status
status = ws_status()
print(status)
```

## Example: Live Dashboard

```typescript
// Update UI on memory events
client.onMessage((msg) => {
  if (msg.type === 'event') {
    updateDashboard({
      type: msg.event_type,
      content: msg.payload?.content,
      time: msg.timestamp,
    })
  }
})
```

## Related

- [Events](../concepts/events)
- [Hooks](../concepts/hooks)
