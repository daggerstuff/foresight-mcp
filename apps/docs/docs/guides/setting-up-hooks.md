---
sidebar_label: Setting Up Hooks
title: Guide - Setting Up Hooks
---

# Setting Up Hooks

Configure HTTP webhooks for event-driven integrations.

## Register a Hook

```bash
foresight hook register "My Webhook" "memory.stored" \
  --url https://example.com/webhook \
  --retry 3 \
  --timeout 30
```

## Python API

```python
from foresight_mcp.hooks import register_hook, EventType

hook = register_hook(
    name="Audit Logger",
    event_type=EventType.MEMORY_STORED,
    url="https://audit.example.com/log",
    retry_count=3,
    timeout=30,
    metadata={"source": "production"}
)
print(f"Registered: {hook.id}")
```

## List Hooks

```bash
foresight hook list
```

```python
from foresight_mcp.hooks import list_hooks

hooks = list_hooks()
for hook in hooks:
    print(f"{hook.name} -> {hook.handler}")
```

## Unregister Hook

```bash
foresight hook unregister <hook_id>
```

```python
from foresight_mcp.hooks import unregister_hook

unregister_hook(hook_id)
```

## Webhook Payload

Your endpoint receives:

```json
{
  "event_id": "evt_abc123",
  "event_type": "memory.stored",
  "timestamp": "2026-04-15T12:00:00Z",
  "actor": "user",
  "entity_id": "mem_xyz",
  "payload": {
    "content": "..."
  },
  "metadata": {}
}
```

## Example: Slack Integration

```python
register_hook(
    name="slack-crisis",
    event_type=EventType.ANOMALY_DETECTED,
    url="https://hooks.slack.com/services/XXX/YYY/ZZZ",
    retry_count=5
)
```

## Example: Zapier Trigger

```python
register_hook(
    name="zapier-sync",
    event_type=EventType.MEMORY_STORED,
    url="https://hooks.zapier.com/hooks/catch/123456/abcdef"
)
```

## Retry Behavior

Failed hooks retry with exponential backoff:

| Attempt | Delay |
| --- | --- |
| 1 | 0s |
| 2 | 2s |
| 3 | 4s |

## Related

- [Events](../concepts/events)
- [WebSocket](../concepts/websocket)
