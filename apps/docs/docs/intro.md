---
sidebar_label: Introduction
title: Foresight Memory Architecture
---

# Welcome to Foresight

**Domain-agnostic, composable memory for AI agents.**

Foresight is a persistent memory architecture that provides AI agents with
long-term context, emotional intelligence, and psychological safety features.
Built on the Model Context Protocol (MCP), it enables seamless integration with
any AI system.

## What is Foresight?

Foresight provides:

- **Persistent Memory**: Store and retrieve memories with configurable retention
  policies
- **Emotional Context**: Track emotional metadata and empathy metrics
- **Event Sourcing**: Full audit trail of all memory operations
- **Composable Blocks**: Dynamic memory block schemas for structured context
- **Real-time Updates**: WebSocket subscriptions for live event streaming
- **Extensible Hooks**: HTTP webhook integration for external systems

## Quick Example

```python
from foresight_mcp import ForesightClient

client = ForesightClient()

# Store a memory
result = client.store_memory(
    "User prefers TypeScript for backend development",
    scope="fact",
    retention="long_term"
)
print(f"Stored: {result.id}")

# Query memories
memories = client.query_memories("TypeScript")
for memory in memories:
    print(memory.content)
```

## TypeScript Example

```typescript
import { ForesightClient, MemoryScope, RetentionPolicy } from '@foresight/core'

const client = new ForesightClient()

// Store a memory
const result = await client.storeMemory('User prefers dark mode', {
  scope: MemoryScope.Fact,
  retention: RetentionPolicy.LongTerm,
})

// Subscribe to real-time updates
client.subscribeToEvents(['memory.stored', 'memory.updated'])
```

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                    Foresight MCP Server                  │
├─────────────────────────────────────────────────────────┤
│  Memory Store  │  Event Bus  │  Hook System  │  WebSocket│
│  Block Manager │  Subconscious│  Crisis Detect│  Audit   │
└─────────────────────────────────────────────────────────┘
                            │
         ┌──────────────────┼──────────────────┐
         │                  │                  │
    Python SDK       TypeScript SDK       CLI Tools
```

## Key Features

| Feature                | Description                                                                 |
| ---------------------- | --------------------------------------------------------------------------- |
| **Event Sourcing**     | Every operation is an event, stored in SQLite with full audit trail         |
| **Domain-Agnostic**    | Anomaly detection works for mental health, security, finance, or any domain |
| **Composable Schemas** | Define custom memory block types with validation                            |
| **Real-time Sync**     | WebSocket subscriptions for live updates                                    |
| **Multi-tenant Ready** | User isolation built into the architecture                                  |

## Next Steps

- [Installation](./installation) - Set up Foresight in your project
- [Quickstart](./quickstart) - 5-minute guide to storing memories
- [Core Concepts](./concepts/memory) - Understand memories, blocks, and events

## License

MIT License - see
[LICENSE](https://github.com/daggerstuff/foresight-mcp/blob/main/LICENSE)
