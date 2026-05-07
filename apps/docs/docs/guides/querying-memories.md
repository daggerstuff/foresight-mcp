---
sidebar_label: Querying Memories
title: Guide - Querying Memories
---

# Querying Memories

Search and retrieve stored memories.

## Basic Query

```python
from foresight_mcp import query_memories

results = query_memories("TypeScript")
print(results)
# Found 2 memories:
# - [abc123] (fact/long_term) User prefers TypeScript...
# - [def456] (fact/long_term) TypeScript config...
```

## List All Memories

```python
from foresight_mcp import list_memories

# Default: first 10
memories = list_memories()

# With pagination
memories = list_memories(limit=20, offset=10)
```

## Get Specific Memory

```python
from foresight_mcp import get_memory

memory = get_memory("abc123")
print(memory)
```

## Using the CLI

```bash
# Query by content
foresight query "TypeScript" --limit 5

# List memories
foresight list --limit 10 --offset 0

# Get specific memory
foresight get abc123
```

## TypeScript SDK

```typescript
const client = new ForesightClient()

// Query
const results = await client.queryMemories('TypeScript', { limit: 5 })

// List
const memories = await client.listMemories({ limit: 10, offset: 0 })

// Get specific
const memory = await client.getMemory('abc123')
```

## Filtering

```python
# By user
results = query_memories("TypeScript", user_id="user123")

# With limit
results = query_memories("TypeScript", limit=5)
```

## Related

- [Storing Memories](./storing-memories)
- [Memory Architecture](../concepts/memory)
