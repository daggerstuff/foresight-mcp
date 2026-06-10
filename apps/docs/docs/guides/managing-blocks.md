---
sidebar_label: Managing Blocks
title: Guide - Managing Context Blocks
---

# Managing Context Blocks

Create, inspect, update, and reset Foresight context blocks.

Context blocks are persisted per `(user_id, tenant_id)`. `foresight blocks list`
shows only non-empty blocks, while `get` can still return an empty string for a
recently cleared block.

## Read a block

```python
from foresight_mcp import get_context_block

guidance = get_context_block("guidance", user_id="vivi")
print(guidance)
```

## Update a block

```python
from foresight_mcp import update_context_block

update_context_block(
    label="guidance",
    content="Always run focused verification before declaring completion.",
    user_id="vivi",
)
```

## Append a guidance line

```python
from foresight_mcp import add_context_guidance

add_context_guidance("Prefer small, reviewable diffs.", user_id="vivi")
```

## Get the full continuity snapshot

```python
from foresight_mcp import get_context_snapshot, get_context_whisper

snapshot = get_context_snapshot(user_id="vivi")
whisper = get_context_whisper(user_id="vivi")
print(snapshot)
print(whisper)
```

## Reset or clear a block

```python
from foresight_mcp import clear_context_block, reset_context_block

clear_context_block("guidance", user_id="vivi")
reset_context_block("guidance", user_id="vivi")
```

## CLI commands

```bash
# List non-empty blocks
foresight blocks list

# Read one block
foresight blocks get guidance

# Replace block content
foresight blocks update guidance "Prefer early returns over nested conditionals."

# Reset to default content
foresight blocks reset guidance

# Clear content entirely
foresight blocks clear guidance
```

## MCP / JSON contract

The MCP-facing `manage_context_blocks` tool returns stable JSON envelopes:

```json
{
  "ok": true,
  "action": "clear",
  "label": "guidance",
  "message": "Cleared block 'guidance'"
}
```

Failures return the same shape with `ok: false` and an `error.message` field.

## Migration note

If you are upgrading older automation, map the legacy names as follows:

| Legacy | Current |
| --- | --- |
| `get_subconscious_block` | `get_context_block` |
| `update_subconscious_block` | `update_context_block` |
| `add_subconscious_guidance` | `add_context_guidance` |
| `get_subconscious_whisper` | `get_context_whisper` |
| `get_subconscious_context` | `get_context_snapshot` |
| `reset_subconscious_block` | `reset_context_block` |
| `clear_subconscious_block` | `clear_context_block` |
