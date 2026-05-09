 ---
sidebar_label: Basic Usage
title: Basic Usage Examples
---

# Basic Usage Examples

Common patterns for using Foresight.

## Store and Retrieve

```python
from foresight_mcp import store_memory, query_memories, list_memories

# Store a memory
result = store_memory(
    content="User prefers TypeScript",
    scope="trait",
    retention="permanent",
    category="preference"
)
print(f"Stored: {result}")

# Query
results = query_memories("TypeScript")
print(results)

# List all
all_memories = list_memories(limit=10)
print(all_memories)
```

## With Emotional Context

```python
from foresight_mcp import store_memory

result = store_memory(
    content="User frustrated with deployment",
    emotional_context={
        "valence": -0.5,
        "arousal": 0.7,
        "primary_emotion": "frustration",
        "intensity": 0.6
    }
)
```

## TypeScript Example

```typescript
import { ForesightClient, MemoryScope, RetentionPolicy } from '@foresight/core'

const client = new ForesightClient()

// Store
const result = await client.storeMemory('User prefers dark mode', {
  scope: MemoryScope.Fact,
  retention: RetentionPolicy.LongTerm,
})

// Query
const memories = await client.queryMemories('dark mode')
console.log(memories)
```

## CLI Usage

```bash
# Store
foresight store "Remember this" --scope fact --retention long_term

# Query
foresight query "remember"

# List
foresight list --limit 5

# Status
foresight status --json
```

## Related

- [Storing Memories](../guides/storing-memories)
- [Querying Memories](../guides/querying-memories)
