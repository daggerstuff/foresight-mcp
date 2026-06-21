# @foresight/core

TypeScript SDK for Foresight Memory Architecture - a domain-agnostic, composable
memory system for AI agents.

## Installation

```bash
pnpm add @foresight/core
# or
npm install @foresight/core
# or
yarn add @foresight/core
```

## Quick Start

```typescript
import { ForesightClient, MemoryScope, RetentionPolicy } from '@foresight/core'

// Initialize client
const client = new ForesightClient({
  userId: 'my-user-id',
  bankId: 'default',
})

// Store a memory
const result = await client.storeMemory(
  'User prefers TypeScript for backend development',
  {
    scope: MemoryScope.Fact,
    retention: RetentionPolicy.LongTerm,
    category: 'preference',
  },
)

console.log(`Stored memory: ${result.id}`)

// Query memories
const memories = await client.queryMemories('TypeScript')
console.log(`Found ${memories.length} memories`)

// List all memories
const allMemories = await client.listMemories({ limit: 10 })

// Get specific memory
const memory = await client.getMemory(result.id)

// Update memory
await client.updateMemory(result.id, {
  content: 'Updated content',
})

// Delete memory
await client.deleteMemory(result.id)
```

## API

### ForesightClient

Main client for memory operations.

#### Options

- `serverUrl` - MCP server URL (optional, defaults to local)
- `userId` - User identifier
- `bankId` - Memory bank identifier
- `timeout` - Request timeout in ms
- `fetch` - Optional fetch implementation for tests and Node runtimes
- `retry` - Retry/backoff configuration for transient request failures

#### Methods

- `storeMemory(content, options)` - Store a new memory
- `queryMemories(query, options)` - Search memories by content
- `listMemories(options)` - List all memories
- `getMemory(memoryId)` - Get specific memory
- `updateMemory(memoryId, updates)` - Update memory
- `deleteMemory(memoryId)` - Delete memory
- `synthesizeMemories()` - Run synthesis on memories
- `archiveMemory(memoryId)` - Archive to ghost node
- `getStatus()` - Get system status

### BlockManager

Manage composable memory block schemas.

```typescript
import { BlockManager, RetentionPolicy, MergeStrategy } from '@foresight/core'

const blockManager = new BlockManager()

// Register schema
blockManager.register({
  label: 'guidance',
  description: 'Active guidance for next session',
  retentionPolicy: RetentionPolicy.ShortTerm,
  mergeStrategy: MergeStrategy.Append,
})

// Create block
const block = blockManager.createBlock('guidance', 'Always use TDD')
```

### HookManager

Manage event hooks for extensibility.

```typescript
import { HookManager, EventType } from '@foresight/core'

const hookManager = new HookManager()

// Register HTTP webhook
await hookManager.registerHook({
  name: 'my-webhook',
  eventType: EventType.MemoryStored,
  url: 'https://example.com/webhook',
  retryCount: 3,
  timeout: 30,
})

// List hooks
const hooks = await hookManager.listHooks()

// Unregister hook
await hookManager.unregisterHook(hookId)
```

### EventStoreClient

Access event audit trail.

```typescript
import { EventStoreClient, EventType } from '@foresight/core'

const eventClient = new EventStoreClient()

// Get events by entity
const events = await eventClient.getByEntity('memory-123')

// Get events by type
const storedEvents = await eventClient.getByType(EventType.MemoryStored)

// Get events by time range
const recent = await eventClient.getByTimeRange(
  new Date(Date.now() - 3600000),
  new Date(),
)
```

## Types

### MemoryScope

- `Session` - Session-specific memory
- `Arc` - Arc-level memory
- `Trait` - Trait memory
- `Fact` - Fact memory

### RetentionPolicy

- `Ephemeral` - Deleted after session
- `ShortTerm` - Kept for arc duration
- `LongTerm` - Candidate for archival
- `Permanent` - Never archived

### MergeStrategy

- `Append` - Append to existing content
- `Replace` - Replace entire content
- `Synthesize` - LLM-based synthesis

### EventType

- `MemoryStored` - memory.stored
- `MemoryRetrieved` - memory.retrieved
- `MemoryUpdated` - memory.updated
- `MemoryDeleted` - memory.deleted
- `BlockCreated` - block.created
- `BlockUpdated` - block.updated
- `BlockDeleted` - block.deleted
- `AnomalyDetected` - anomaly.detected
- `SystemError` - system.error

## License

MIT
