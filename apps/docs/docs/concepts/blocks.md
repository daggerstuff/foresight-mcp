---
sidebar_label: Blocks
title: Context Blocks
---

# Context Blocks

Context blocks are Foresight's structured continuity layer for active guidance,
project state, and durable preferences.

## What context blocks provide

Context blocks are named containers that hold high-signal context with:

- stable labels for retrieval and updates
- explicit default content
- SQLite-backed persistence
- tenant isolation by `(user_id, tenant_id)`
- XML snapshot and whisper views for prompt injection
- clear separation between active continuity and long-term memory banks

## Default context blocks

| Block | Purpose |
| --- | --- |
| `core_directives` | Role definition and operating principles |
| `guidance` | Active guidance for the next session or turn |
| `pending_items` | Open work that should survive context resets |
| `project_context` | Architecture notes, constraints, and repo-specific state |
| `session_patterns` | Repeated patterns across sessions |
| `user_preferences` | User workflow and communication preferences |
| `self_improvement` | Lessons about the memory system itself |
| `tool_guidelines` | Tool-usage reminders and constraints |

## Working with blocks in Python

```python
from foresight_mcp import (
    add_context_guidance,
    get_context_block,
    get_context_snapshot,
    get_context_whisper,
    update_context_block,
)

content = get_context_block("guidance", user_id="vivi")
update_context_block("guidance", "Write code first, then docs.", user_id="vivi")
add_context_guidance("Keep updates short.", user_id="vivi")
whisper = get_context_whisper(user_id="vivi")
snapshot = get_context_snapshot(user_id="vivi")
```

## Working with blocks from the CLI

```bash
foresight blocks list
foresight blocks get guidance
foresight blocks update guidance "Write code first, then docs."
foresight blocks reset guidance
foresight blocks clear guidance
```

## Relationship to curation

Context blocks are not the same as curation outputs:

- **Context blocks** hold active continuity state
- **Memory banks** hold stored memories
- **Curation runs** reorganize a memory bank into a new reviewable output bank
  or, with stronger permissions, back into the source bank through a staging
  bank and promotion step

This separation keeps continuity lightweight while letting long-lived memory
maintenance remain asynchronous and inspectable.

## Migration note

Older Foresight integrations and upstream inspiration may refer to these as
`subconscious` blocks. The public Foresight term is now **context blocks**.
