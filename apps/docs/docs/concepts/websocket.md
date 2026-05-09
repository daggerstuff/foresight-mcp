 ---
sidebar_label: WebSocket
title: WebSocket Subscriptions
---

# WebSocket Subscriptions

Real-time event streaming via WebSocket connections.

## Connection

```typescript
import { ForesightWebSocketClient } from '@foresight/core'

const client = new ForesightWebSocketClient({
  url: 'ws://localhost:8765',
  userId: 'my-user-id',
  reconnectInterval: 5000,
  maxReconnectAttempts: 5,
})

await client.connect()
```

## Subscribing to Events

```typescript
// Subscribe to specific event types
const subscriptionId = await client.subscribe({
  eventTypes: ['memory.stored', 'memory.updated'],
  entityFilter: 'memory:*', // Optional wildcard filter
})

// Unsubscribe
await client.unsubscribe(subscriptionId)
```

## Event Filters

| Filter       | Description       |
| ------------ | ----------------- |
| `*`          | All entities      |
| `memory:*`   | All memory events |
| `memory:123` | Specific memory   |

## Message Format

```typescript
client.onMessage((message) => {
  if (message.type === 'event') {
    console.log({
      subscriptionId: message.subscription_id,
      eventType: message.event_type,
      timestamp: message.timestamp,
      payload: message.payload,
    })
  }
})
```

## Connection States

```typescript
type ConnectionState =
  | 'disconnected'
  | 'connecting'
  | 'connected'
  | 'reconnecting'

// Check state
const state = client.getState()

// Handle reconnection
client.onMessage((msg) => {
  if (msg.type === 'connection_accepted') {
    console.log('Connected!')
  }
})
```

## Keepalive

```typescript
// Ping to keep connection alive
setInterval(() => client.ping(), 30000)
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

# Check status
status = ws_status()
print(status)

# List subscriptions
subscriptions = ws_list_subscriptions()
```

## Related

- [Events](./events) - Event system
- [Hooks](./hooks) - HTTP webhooks
