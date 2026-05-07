---
sidebar_label: Python API
title: Python API Reference
---

# Python API Reference

Complete Python API documentation.

## Memory Operations

### store_memory

```python
def store_memory(
    content: str,
    user_id: str | None = None,
    category: str = "fact",
    scope: str = "session",
    retention: str = "short_term",
    importance: float = 0.5,
    emotional_context: Optional[dict] = None,
    metrics: Optional[dict] = None,
) -> str
```

**Parameters:**

- `content` - Memory content to store
- `category` - Category label (default: "fact")
- `scope` - session | arc | trait | fact
- `retention` - ephemeral | short_term | long_term | permanent
- `emotional_context` - Optional emotional metadata
- `user_id` - Optional user ID override

**Returns:** Confirmation string with memory ID

### query_memories

```python
def query_memories(
    query: str,
    user_id: Optional[str] = None,
    limit: int = 10,
    use_hybrid: bool = True,
    min_importance: float = 0.1,
    offset: int = 0,
) -> str
```

### list_memories

```python
def list_memories(
    limit: int = 10,
    offset: int = 0,
    user_id: Optional[str] = None,
) -> str
```

### get_memory

```python
def get_memory(
    memory_id: str,
    user_id: Optional[str] = None,
    min_importance: float = 0.1
) -> str
```

### update_memory

```python
def update_memory(
    memory_id: str,
    user_id: Optional[str] = None,
    content: Optional[str] = None,
    category: Optional[str] = None,
    scope: Optional[str] = None,
    retention: Optional[str] = None,
    tags: Optional[List[str]] = None,
) -> str
```

### memory_status

```python
def memory_status(
    user_id: Optional[str] = None,
    include_trends: bool = False,
    timeframe: str = "30 days",
) -> str
```

### delete_memory

```python
def delete_memory(
    memory_id: str,
    user_id: Optional[str] = None
) -> str
```

## Block Operations

### get_subconscious_block

```python
def get_subconscious_block(
    label: str,
    user_id: Optional[str] = None
) -> str
```

### update_subconscious_block

```python
def update_subconscious_block(
    label: str,
    content: str,
    user_id: Optional[str] = None
) -> str
```

### add_subconscious_guidance

```python
def add_subconscious_guidance(
    line: str,
    user_id: Optional[str] = None
) -> str
```

## Hook Operations

### register_hook

```python
def register_hook(
    name: str,
    event_type: str,
    hook_type: str = "http",
    url: Optional[str] = None,
    retry_count: int = 3,
    timeout: int = 30
) -> str
```

### list_hooks

```python
def list_hooks() -> str
```

### unregister_hook

```python
def unregister_hook(hook_id: str) -> str
```

## Related

- [TypeScript API](./typescript-api)
- [CLI Reference](./cli-reference)
