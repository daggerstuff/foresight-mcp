---
sidebar_label: Blocks
title: Memory Blocks
---

# Memory Blocks

Memory Blocks provide structured, composable context containers with
configurable retention and merge strategies.

## What are Blocks?

Blocks are named containers that hold contextual information with:

- Defined schema (label, description, retention policy)
- Merge strategies (append, replace, synthesize)
- Injection points (pre-prompt, post-prompt, whisper-only)
- Scope (global, project, session)

## Default Blocks

Foresight ships with these default blocks:

| Block              | Purpose                | Retention  | Injection  |
| ------------------ | ---------------------- | ---------- | ---------- |
| `guidance`         | Active guidance        | Short-term | Pre-prompt |
| `pending_items`    | Unfinished work        | Short-term | Pre-prompt |
| `project_context`  | Codebase decisions     | Long-term  | Pre-prompt |
| `session_patterns` | Observed patterns      | Long-term  | Pre-prompt |
| `user_preferences` | Coding style           | Long-term  | Pre-prompt |
| `self_improvement` | Architecture evolution | Permanent  | Whisper    |
| `tool_guidelines`  | Tool usage patterns    | Permanent  | Whisper    |

## Creating Custom Blocks

```python
from foresight_mcp.block_registry import BlockRegistry, MemoryBlockSchema, RetentionPolicy, MergeStrategy, InjectionPoint, BlockScope

registry = get_registry()

# Define schema
schema = MemoryBlockSchema(
    label="api_keys",
    description="API keys and credentials",
    retention_policy=RetentionPolicy.PERMANENT,
    merge_strategy=MergeStrategy.REPLACE,
    injection_point=InjectionPoint.WHISPER_ONLY,
    scope=BlockScope.GLOBAL,
    char_limit=1000
)

# Register
registry.register(schema)
```

## Merge Strategies

| Strategy     | Behavior         | Use Case          |
| ------------ | ---------------- | ----------------- |
| `append`     | Add to existing  | Session patterns  |
| `replace`    | Replace entirely | Configuration     |
| `synthesize` | LLM merge        | Complex evolution |

## Block Operations

```python
from foresight_mcp import (
    get_subconscious_block,
    update_subconscious_block,
    add_subconscious_guidance,
    get_subconscious_whisper
)

# Get block content
content = get_subconscious_block("guidance")

# Update block
update_subconscious_block("guidance", "Always use TDD")

# Add guidance line
add_subconscious_guidance("Prefer early returns")

# Get full context (XML format)
whisper = get_subconscious_whisper()
```

## Injection Points

Blocks inject at specific points:

- **Pre-prompt**: Beginning of prompt
- **Post-prompt**: End of prompt
- **Whisper-only**: System messages only

## Related

- [Memory](./memory) - Core memory structure
- [Events](./events) - Block lifecycle events
- [Managing Blocks](../guides/managing-blocks) - Full guide
