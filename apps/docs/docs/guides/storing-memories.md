 ---
sidebar_label: Storing Memories
title: Guide - Storing Memories
---

# Storing Memories

Complete guide to storing memories with Foresight.

## Basic Storage

```python
from foresight_mcp import store_memory

result = store_memory(
    content="User prefers dark mode",
    scope="fact",
    retention="long_term"
)
print(result)
```

## With Emotional Context

```python
result = store_memory(
    content="User frustrated with deployment pipeline",
    emotional_context={
        "valence": -0.5,
        "arousal": 0.7,
        "primary_emotion": "frustration",
        "intensity": 0.6
    }
)
```

## Categories

Use categories to organize memories:

```python
# Preference
store_memory("TypeScript > Python", category="preference")

# Fact
store_memory("Project uses pnpm", category="fact")

# Observation
store_memory("User prefers morning standups", category="observation")
```

## Using the CLI

```bash
# Store a memory
foresight store "Remember this" --scope fact --retention long_term

# With category
foresight store "Uses dark mode" --category preference
```

## TypeScript SDK

```typescript
import { ForesightClient, MemoryScope, RetentionPolicy } from '@foresight/core'

const client = new ForesightClient()

const result = await client.storeMemory('User prefers TypeScript', {
  scope: MemoryScope.Fact,
  retention: RetentionPolicy.LongTerm,
  category: 'preference',
})
```

## Best Practices

### 1. Use Appropriate Scope

```python
# Session-specific (temporary)
store_memory("Working on auth feature", scope="session")

# Project-level (arc)
store_memory("Using JWT for auth", scope="arc")

# User trait (persistent)
store_memory("Prefers async/await", scope="trait")

# Standalone fact
store_memory("API uses REST", scope="fact")
```

### 2. Set Retention Policies

```python
# Ephemeral - auto-deletes
store_memory("Current task in progress", retention="ephemeral")

# Long-term for reference
store_memory("Database schema", retention="long_term")
```

### 3. Add Emotional Context

```python
# Track user sentiment
store_memory(
    "User excited about new feature",
    emotional_context={"primary_emotion": "excitement", "intensity": 0.8}
)
```

## Related

- [Memory Architecture](../concepts/memory)
- [Querying Memories](./querying-memories)
