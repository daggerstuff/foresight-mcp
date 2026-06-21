---
sidebar_label: Memory
title: Memory Architecture
---

# Memory Architecture

Foresight's core unit is the **Memory** - a persistent, contextual record with
emotional metadata and configurable retention.

## Memory Structure

```typescript
interface Memory {
  id: string // Unique identifier
  content: string // The memory content
  scope: MemoryScope // session | arc | trait | fact
  retention: RetentionPolicy // ephemeral | short_term | long_term | permanent
  category: string // User-defined category
  userId: string // Owner
  bankId: string // Memory bank
  createdAt: string // ISO timestamp
  updatedAt?: string // Last modified
  tags: string[] // Auto-generated tags
  emotionalContext?: EmotionalMetadata
  metrics?: EmpathyMetrics
  isGhost: boolean // Archived content
}
```

## Scopes

| Scope     | Description               | Use Case           |
| --------- | ------------------------- | ------------------ |
| `session` | Current conversation      | Temporary context  |
| `arc`     | Story arc / project phase | Multi-session work |
| `trait`   | Persistent characteristic | User preferences   |
| `fact`    | Standalone fact           | Reference data     |

## Retention Policies

| Policy       | Duration              | Auto-delete |
| ------------ | --------------------- | ----------- |
| `ephemeral`  | Session end           | Yes         |
| `short_term` | Arc completion        | Yes         |
| `long_term`  | Candidate for archive | No          |
| `permanent`  | Never                 | No          |

## Emotional Context

Memories can carry emotional metadata:

```python
store_memory(
    content="User frustrated with deployment issues",
    emotional_context={
        "valence": -0.6,      # Negative to positive
        "arousal": 0.8,       # Calm to excited
        "dominance": 0.3,     # Controlled to dominant
        "primary_emotion": "frustration",
        "intensity": 0.7
    }
)
```

## Example Usage

```python
from foresight_mcp import store_memory, get_memory, update_memory

# Store with full metadata
result = store_memory(
    content="User prefers dark mode in IDE",
    scope="trait",
    retention="permanent",
    category="preference",
    emotional_context={"valence": 0.5}
)

# Retrieve
memory = get_memory(result.id)

# Update
update_memory(result.id, tags=["ide", "preference"])
```

## Psychological Safety

Foresight includes a **Socratic Gate** that evaluates memories for psychological
safety:

- **Crisis Detection**: Identifies potential mental health crises
- **Anomaly Detection**: Domain-agnostic anomaly flagging
- **Gate Decision**: Allow, flag, or require review

```python
result = store_memory("I'm feeling overwhelmed")
# Gate Decision: allow
# Reason: No crisis indicators detected
# Tags: ["check-in"]
```

## Related

- [Blocks](./blocks) - Structured memory containers
- [Events](./events) - Memory lifecycle events
- [Storing Memories](../guides/storing-memories) - Full guide
