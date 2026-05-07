---
sidebar_label: Managing Blocks
title: Guide - Managing Memory Blocks
---

# Managing Memory Blocks

Create, update, and query memory blocks.

## Get Block Content

```python
from foresight_mcp import get_subconscious_block

# Get guidance block
guidance = get_subconscious_block("guidance")
print(guidance)
```

## Update Block

```python
from foresight_mcp import update_subconscious_block

update_subconscious_block(
    label="guidance",
    content="Always use TDD. Write tests first."
)
```

## Add Guidance Line

```python
from foresight_mcp import add_subconscious_guidance

add_subconscious_guidance("Prefer early returns over nested conditionals")
```

## Get Full Context

```python
from foresight_mcp import get_subconscious_context

# Get all blocks as XML
context = get_subconscious_context()
print(context)
```

## Whisper (XML Format)

```python
from foresight_mcp import get_subconscious_whisper

whisper = get_subconscious_whisper()
# Returns XML formatted context for LLM injection
```

## CLI Commands

```bash
# List all block schemas
foresight block list

# Get block content
foresight block get guidance

# Create block
foresight block create my-block --content "Initial content"
```

## TypeScript SDK

```typescript
import { BlockManager } from '@foresight/core'

const blockManager = new BlockManager()

// Get block
const block = blockManager.get('guidance')

// Update content
blockManager.updateContent('guidance', 'New guidance')

// List all
const blocks = blockManager.list()
```

## Related

- [Blocks Concept](../concepts/blocks)
- [Events](../concepts/events)
