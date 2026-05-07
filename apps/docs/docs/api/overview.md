---
sidebar_label: Overview
title: API Reference Overview
---

# API Reference

Complete API documentation for Foresight Memory Architecture.

## Available APIs

| API                                | Description              |
| ---------------------------------- | ------------------------ |
| [Python API](./python-api)         | Python SDK reference     |
| [TypeScript API](./typescript-api) | TypeScript SDK reference |
| [CLI Reference](./cli-reference)   | Command-line interface   |

## Quick Links

### Memory Operations

- `store_memory(content, user_id=None, category="fact", scope="session", retention="short_term", importance=0.5, emotional_context=None, metrics=None)` -
  Store new memory
- `query_memories(query, user_id=None, limit=10, use_hybrid=True, min_importance=0.1, offset=0)` -
  Search memories
- `list_memories(limit=10, offset=0, user_id=None)` - List all memories
- `get_memory(id)` - Get specific memory
- `update_memory(id, user_id=None, content=None, category=None, scope=None, retention=None, tags=None)` -
  Update memory
- `delete_memory(id)` - Delete memory
- `memory_status(user_id=None, include_trends=False, timeframe="30 days")` - Get
  system status

### Block Operations

- `get_subconscious_block(label)` - Get block content
- `update_subconscious_block(label, content)` - Update block
- `add_subconscious_guidance(line)` - Add guidance line

### Hook Operations

- `register_hook(name, event_type, url, **options)` - Register hook
- `list_hooks()` - List all hooks
- `unregister_hook(hook_id)` - Remove hook

### WebSocket Operations

- `ws_subscribe(subscription_id, event_types, **options)` - Subscribe
- `ws_unsubscribe(subscription_id)` - Unsubscribe
- `ws_status()` - Get status

## Versioning

API follows semantic versioning. Breaking changes will increment the major
version.
