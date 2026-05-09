 ---
sidebar_label: CLI Reference
title: CLI Reference
---

# CLI Reference

Command-line interface for Foresight memory operations.

## Installation

```bash
# The CLI is included with the foresight-mcp package
# Or run directly from the scripts directory
python scripts/foresight-cli.py --help
```

## Commands

### Memory Commands

```bash
# Store a new memory
foresight store <content> [options]

# Query memories by content
foresight query <query> [options]

# List recent memories
foresight list [options]

# Get a specific memory by ID
foresight get <memory_id> [options]

# Update a memory
foresight update <memory_id> [options]

# Delete a memory
foresight delete <memory_id> [options]

# Synthesize memories into insights
foresight synthesize [options]

# Archive a memory
foresight archive <memory_id> [options]

# Rollback a memory to a previous version
foresight rollback <memory_id> <version> [options]

# Show diff between two memory versions
foresight diff <memory_id> <version1> <version2> [options]

# Show memory system status
foresight status
```

### Subconscious Block Commands

```bash
# List subconscious blocks
foresight subconscious list [options]

# Reset a subconscious block
foresight subconscious reset <label> [options]

# Clear a subconscious block
foresight subconscious clear <label> [options]
```

## Options

### Global Options

| Option         | Description    | Default       |
| -------------- | -------------- | ------------- |
| `--help`       | Show help      | -             |
| `--json`, `-j` | Output as JSON | false         |
| `--user`, `-u` | User ID        | auto-detected |

### Store Options

| Option              | Description               | Default    |
| ------------------- | ------------------------- | ---------- |
| `--category`, `-c`  | Memory category           | fact       |
| `--scope`, `-s`     | session, arc, trait, fact | session    |
| `--retention`, `-r` | short_term, long_term     | short_term |

### Query/List Options

| Option          | Description | Default               |
| --------------- | ----------- | --------------------- |
| `--limit`, `-n` | Max results | 10 (query), 20 (list) |

## Examples

```bash
# Store a memory
foresight store "Learning CBT techniques has been helpful" --category fact --user alice

# Query memories
foresight query "therapy" --limit 5

# List recent memories
foresight list --limit 10

# Get specific memory
foresight get mem_abc123

# Synthesize insights
foresight synthesize

# JSON output for scripting
foresight status --json

# Rollback to previous version
foresight rollback mem_abc123 3

# Show diff between versions
foresight diff mem_abc123 1 2

# List subconscious blocks
foresight subconscious list
```

## Output Format

The CLI uses rich terminal output by default with colored panels, tables, and
syntax highlighting. Use `--json` for machine-readable output suitable for
scripting.

## Related

- [Python API](./python-api)
- [TypeScript API](./typescript-api)
- [Quickstart](../quickstart)
