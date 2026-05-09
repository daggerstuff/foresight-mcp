 ---
sidebar_label: WebSocket Client
title: WebSocket Client Example
---

# WebSocket Client Example

Real-time event streaming example.

## Basic Connection

```typescript
import { ForesightWebSocketClient, EventType } from '@foresight/core'

const client = new ForesightWebSocketClient({
  url: 'ws://localhost:8765',
  userId: 'my-user-id',
})

// Connect
await client.connect()
console.log('Connected!')
```

## Subscribe to Events

```typescript
// Subscribe to memory events
const subId = await client.subscribe({
  eventTypes: [
    EventType.MemoryStored,
    EventType.MemoryUpdated,
    EventType.MemoryDeleted,
  ],
})

console.log(`Subscribed: ${subId}`)
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
      console.log('Subscription confirmed:', message.subscription_id)
      break

    case 'unsubscribed':
      console.log('Subscription removed:', message.subscription_id)
      break

    case 'pong':
      // Keepalive response
      break
  }
})
```

## Full Example: Live Dashboard

```typescript
import { ForesightWebSocketClient, EventType } from '@foresight/core'

class MemoryDashboard {
  private client: ForesightWebSocketClient
  private memories: Map<string, any> = new Map()

  constructor() {
    this.client = new ForesightWebSocketClient({
      url: 'ws://localhost:8765',
      userId: 'dashboard-user',
    })
  }

  async start() {
    await this.client.connect()

    await this.client.subscribe({
      eventTypes: [
        EventType.MemoryStored,
        EventType.MemoryUpdated,
        EventType.MemoryDeleted,
      ],
      entityFilter: 'memory:*',
    })

    this.client.onMessage(this.handleMessage.bind(this))

    // Keepalive
    setInterval(() => this.client.ping(), 30000)
  }

  private handleMessage(message: any) {
    if (message.type === 'event') {
      this.updateDashboard(message)
    }
  }

  private updateDashboard(event: any) {
    const { event_type, payload, timestamp } = event

    console.log(`[${timestamp}] ${event_type}:`, payload)

    // Update UI
    this.render()
  }

  private render() {
    // Render dashboard with current state
    console.log('Dashboard updated')
  }
}

// Start dashboard
const dashboard = new MemoryDashboard()
dashboard.start()
```

## Reconnection Handling

```typescript
const client = new ForesightWebSocketClient({
  url: 'ws://localhost:8765',
  reconnectInterval: 5000,
  maxReconnectAttempts: 5,
})

client.onMessage((msg) => {
  if (msg.type === 'connection_accepted') {
    console.log('Connection established')
  }
})

// Check state
const state = client.getState()
if (state === 'reconnecting') {
  console.log('Attempting to reconnect...')
}
```

## Related

- [Real-time Updates](../guides/real-time-updates)
- [WebSocket Concept](../concepts/websocket)
