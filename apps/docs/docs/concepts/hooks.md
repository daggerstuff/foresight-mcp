---
sidebar_label: Hooks
title: Event Hooks
---

# Event Hooks

Hooks enable extensibility by triggering external actions when events occur.

## Hook Types

| Type | Description |
| --- | --- |
| `callable` | Python function |
| `http` | HTTP webhook |
| `async` | Async Python function |

## Registering HTTP Hooks

```bash
# Via CLI
foresight hook register "My Webhook" "memory.stored" \
  --url https://example.com/webhook \
  --retry 3 \
  --timeout 30
```

```python
# Via Python
from foresight_mcp.hooks import register_hook, EventType

hook = register_hook(
    name="Slack Notification",
    event_type=EventType.MEMORY_STORED,
    url="https://hooks.slack.com/services/xxx",
    retry_count=3,
    timeout=30
)
```

## Hook Payload

HTTP hooks receive this payload:

```json
{
  "event_id": "evt_abc123",
  "event_type": "memory.stored",
  "timestamp": "2026-04-15T12:00:00Z",
  "actor": "user",
  "entity_id": "mem_xyz789",
  "payload": {
    "content": "Memory content preview..."
  },
  "metadata": {}
}
```

## Retry Logic

Failed HTTP hooks retry with exponential backoff:

```
Attempt 1: t=0s
Attempt 2: t=2s (2^1)
Attempt 3: t=4s (2^2)
```

## Listing Hooks

```bash
# List all hooks
foresight hook list
```

```python
from foresight_mcp.hooks import list_hooks

hooks = list_hooks()
for hook in hooks:
    print(f"{hook.name}: {hook.event_type}")
```

## Unregistering

```bash
foresight hook unregister <hook_id>
```

```python
from foresight_mcp.hooks import unregister_hook

unregister_hook(hook_id)
```

## Use Cases

### Slack Notifications

```python
register_hook(
    name="slack-alerts",
    event_type=EventType.ANOMALY_DETECTED,
    url="https://hooks.slack.com/services/xxx"
)
```

### Audit Logging

```python
register_hook(
    name="audit-log",
    event_type=EventType.MEMORY_STORED,
    url="https://audit.example.com/log"
)
```

### Cache Invalidation

```python
register_hook(
    name="cache-flush",
    event_type=EventType.MEMORY_UPDATED,
    url="https://cache.example.com/flush"
)
```

## Related

- [Events](./events) - Event system
- [WebSocket](./websocket) - Real-time alternative
