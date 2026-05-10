---
sidebar_label: Python API
title: Python API Reference
---

# Python API Reference

Complete Python API documentation for Foresight memory, context blocks, and
curation workflows.

## Memory operations

### store_memory

```python
def store_memory(
    content: str,
    user_id: str | None = None,
    category: str = "fact",
    scope: str = "session",
    retention: str = "short_term",
    importance: float = 0.5,
    emotional_context: dict | None = None,
    metrics: dict | None = None,
) -> str
```

Stores a new memory record.

### query_memories

```python
def query_memories(
    query: str,
    user_id: str | None = None,
    limit: int = 10,
    use_hybrid: bool = True,
    min_importance: float = 0.1,
    offset: int = 0,
) -> str
```

Searches memories by text and ranking signals.

### list_memories

```python
def list_memories(
    limit: int = 10,
    offset: int = 0,
    user_id: str | None = None,
) -> str
```

Lists memories for a user.

### get_memory

```python
def get_memory(
    memory_id: str,
    user_id: str | None = None,
    min_importance: float = 0.1,
) -> str
```

Retrieves one memory by ID.

### update_memory

```python
def update_memory(
    memory_id: str,
    user_id: str | None = None,
    content: str | None = None,
    category: str | None = None,
    scope: str | None = None,
    retention: str | None = None,
    tags: list[str] | None = None,
) -> str
```

Updates memory content or metadata.

### delete_memory

```python
def delete_memory(memory_id: str, user_id: str | None = None) -> str
```

Deletes a memory.

### memory_status

```python
def memory_status(
    user_id: str | None = None,
    include_trends: bool = False,
    timeframe: str = "30 days",
) -> str
```

Returns system status and optional trend data.

## Context block helpers

### list_context_blocks

```python
def list_context_blocks(user_id: str, tenant_id: str = "default") -> list[dict]
```

Lists non-empty persisted context blocks for a `(user_id, tenant_id)` pair.

### get_context_block

```python
def get_context_block(
    label: str,
    user_id: str,
    tenant_id: str = "default",
) -> str | None
```

Reads one context block.

### update_context_block

```python
def update_context_block(
    label: str,
    content: str,
    user_id: str,
    tenant_id: str = "default",
) -> None
```

Replaces a block's content.

### add_context_guidance

```python
def add_context_guidance(
    line: str,
    user_id: str,
    tenant_id: str = "default",
) -> None
```

Appends a line to the `guidance` block.

### reset_context_block

```python
def reset_context_block(
    label: str,
    user_id: str,
    tenant_id: str = "default",
) -> None
```

Restores a block's default content.

### clear_context_block

```python
def clear_context_block(
    label: str,
    user_id: str,
    tenant_id: str = "default",
) -> None
```

Clears a block.

### get_context_whisper

```python
def get_context_whisper(
    user_id: str,
    tenant_id: str = "default",
) -> str
```

Returns the whisper-ready XML payload.

### get_context_snapshot

```python
def get_context_snapshot(
    user_id: str,
    tenant_id: str = "default",
) -> str
```

Returns the full XML snapshot of non-empty blocks.

## MCP-style actions

### ContextBlockAction

```python
class ContextBlockAction(BaseModel):
    action: Literal["list", "get", "update", "reset", "clear"]
    label: str | None = None
    content: str | None = None
```

### manage_context_blocks

```python
def manage_context_blocks(
    options: ContextBlockAction,
    user_id: str | None = None,
) -> str
```

Manages context blocks through the same action-oriented contract exposed by the
MCP server.

Returns a JSON envelope string such as:

```json
{
  "ok": true,
  "action": "list",
  "blocks": [{ "label": "project_context", "content": "..." }]
}
```

### CurationRunAction

```python
class CurationRunAction(BaseModel):
    action: Literal["create", "get", "list", "cancel", "archive"]
    run_id: str | None = None
    source_bank_id: str | None = None
    output_bank_id: str | None = None
    policy_mode: Literal["preserve", "rebalance", "rebuild"] = "rebalance"
    tool_access: Literal["disabled", "observe", "operate"] = "observe"
    output_mode: Literal["reviewable_output", "in_place"] = "reviewable_output"
    instructions: str | None = None
    transcript_bundle: list[dict[str, Any]] | None = None
    session_id: str | None = None
    project_path: str | None = None
    limit: int = 20
```

### manage_curation_runs

```python
def manage_curation_runs(
    options: CurationRunAction,
    user_id: str | None = None,
) -> str
```

Creates and manages asynchronous curation runs.

**Behavior notes**

- `create` defaults to a separate reviewable output bank
- `output_mode="in_place"` requires `tool_access="operate"`
- `output_mode="in_place"` always uses an auto-generated staging bank and
  rejects `output_bank_id` overrides
- transcript bundles require `tool_access="operate"`
- `in_place` runs stage into an auto-generated bank, archive source rows on
  success, and then promote staged rows into the source bank
- `failed` and `canceled` runs leave any already-written staged output untouched
  for inspection
- `archive` only works after a run reaches a terminal state

Tool responses are JSON envelope strings:

```json
{
  "ok": true,
  "action": "get",
  "run": {
    "id": "cur_abc123def456",
    "status": "completed"
  }
}
```

## Migration note

Legacy `subconscious` helper names remain available as compatibility aliases,
but new code should use the Foresight-native context block helpers above.

## Related

- [CLI Reference](./cli-reference)
- [TypeScript API](./typescript-api)
