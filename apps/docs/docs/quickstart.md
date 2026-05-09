 ---
sidebar_label: Quickstart
title: Quickstart - 5 Minutes to Foresight
---

# Quickstart Guide

Get up and running with Foresight in 5 minutes.

## Prerequisites

- Python 3.11+
- Node.js 18+ (for TypeScript SDK)
- uv or pip for Python package management

## Step 1: Install

```bash
# Install the Python package
cd foresight-mcp
uv pip install -e .
```

## Step 2: Store Your First Memory

```python
from foresight_mcp import store_memory, list_memories

# Store a memory
result = store_memory(
    content="User prefers TypeScript over Python for new projects",
    scope="fact",
    retention="long_term",
    category="preference"
)
print(result)
# Stored memory abc123: User prefers TypeScript...
# Gate Decision: allow
# Reason: No crisis indicators detected
```

## Step 3: Query Memories

```python
from foresight_mcp import query_memories

# Search memories
results = query_memories("TypeScript")
print(results)
# Found 1 memories:
# - [abc123] (fact/long_term) User prefers TypeScript over Python...
```

## Step 4: List All Memories

```python
from foresight_mcp import list_memories

# List recent memories
all_memories = list_memories(limit=10)
print(all_memories)
```

## Step 5: Use the CLI

```bash
# Check status
foresight status

# Store via CLI
foresight store "Remember to use TDD" --scope fact --retention long_term

# List memories
foresight list --limit 5

# Query memories
foresight query "TDD"

# View hooks
foresight hook list
```

## Step 6: Set Up Event Hooks (Optional)

Register an HTTP webhook to receive events:

```bash
foresight hook register "My Webhook" "memory.stored" \
  --url https://example.com/webhook \
  --retry 3 \
  --timeout 30
```

## What's Next?

- [Core Concepts](./concepts/memory) - Learn about memories, blocks, and events
- [Guides](./guides/storing-memories) - Deep dive into operations
- [API Reference](./api/overview) - Full API documentation
