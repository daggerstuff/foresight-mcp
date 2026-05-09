 ---
sidebar_label: Event Hooks
title: Event Hooks Examples
---

# Event Hooks Examples

Integrate Foresight with external systems.

## Slack Notifications

```python
from foresight_mcp.hooks import register_hook, EventType

# Notify Slack on anomalies
register_hook(
    name="slack-crisis-alert",
    event_type=EventType.ANOMALY_DETECTED,
    url="https://hooks.slack.com/services/XXX/YYY/ZZZ",
    retry_count=5,
    timeout=30
)
```

## Audit Logging

```python
from foresight_mcp.hooks import register_hook, EventType

# Log all memory operations
register_hook(
    name="audit-log",
    event_type=EventType.MEMORY_STORED,
    url="https://audit.example.com/log",
    retry_count=3
)

register_hook(
    name="audit-log",
    event_type=EventType.MEMORY_DELETED,
    url="https://audit.example.com/log"
)
```

## Zapier Integration

```python
from foresight_mcp.hooks import register_hook, EventType

# Trigger Zapier workflow
register_hook(
    name="zapier-sync",
    event_type=EventType.MEMORY_STORED,
    url="https://hooks.zapier.com/hooks/catch/123456/abcdef"
)
```

## Custom HTTP Handler

```python
import httpx
from foresight_mcp.hooks import register_hook, EventType

# Your custom endpoint
async def handle_event(payload):
    async with httpx.AsyncClient() as client:
        await client.post("https://api.example.com/events", json=payload)

register_hook(
    name="custom-handler",
    event_type=EventType.MEMORY_STORED,
    url="https://api.example.com/events"
)
```

## CLI Registration

```bash
# Register via CLI
foresight hook register "My Hook" "memory.stored" \
  --url https://example.com/webhook \
  --retry 3 \
  --timeout 30

# List hooks
foresight hook list

# Unregister
foresight hook unregister <hook_id>
```

## Related

- [Setting Up Hooks](../guides/setting-up-hooks)
- [Events](../concepts/events)
