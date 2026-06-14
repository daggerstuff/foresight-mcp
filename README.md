# Foresight 🧠

**Persistent memory for AI agents** — CLI, TUI, MCP server, and Python SDK.

[![PyPI](https://img.shields.io/pypi/v/foresight-mcp?color=blue&logo=pypi&logoColor=white)](https://pypi.org/project/foresight-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/foresight-mcp?logo=python&logoColor=white)](https://python.org)
[![License](https://img.shields.io/pypi/l/foresight-mcp?color=green)](LICENSE)
[![CI](https://github.com/daggerstuff/foresight-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/daggerstuff/foresight-mcp/actions)
[![Downloads](https://img.shields.io/pypi/dm/foresight-mcp?color=purple)](https://pypi.org/project/foresight-mcp/)

---

### One-liner install

```bash
pip install foresight-mcp[all]
```

That's it. Now run:

```bash
foresight init          # First-time setup — creates config + database
foresight doctor        # Health check — verify everything works
foresight tui           # Launch the interactive TUI
```

Three commands. You're live.

---

### What you get

| Surface                 | What                                   | How                                                                     |
| ----------------------- | -------------------------------------- | ----------------------------------------------------------------------- |
| **`foresight`**         | Full CLI with 20+ commands             | `foresight store "hello"`, `foresight list`, `foresight query "search"` |
| **`foresight --agent`** | Machine-parseable output for AI agents | `foresight --agent status → [JSON] {...}`                               |
| **`foresight tui`**     | Interactive Textual TUI                | Browse, search, edit memories — keyboard-first                          |
| **`foresight-mcp`**     | MCP server for agent tool integration  | Add to Claude Code, Cursor, Goose, any MCP client                       |
| **Python SDK**          | Import directly for custom tooling     | `from foresight_mcp import store_memory, query_memories`                |

---

### Install walkthrough

#### Step 1 — Install

```bash
pip install foresight-mcp[all]
```

> **Extras breakdown** — install only what you need:
> | Extra | Includes |
> |---|---|
> | `(none)` | MCP server only — no CLI, no TUI |
> | `[cli]` | CLI (`typer` + `rich`) — no TUI |
> | `[tui]` | CLI + TUI (`textual`) — no MCP |
> | `[all]` | Everything — CLI + TUI + MCP server |

On macOS/Linux with uv installed, `uv pip install foresight-mcp[all]` is ~3x faster.

---

#### Step 2 — Init

One command. No config file to write. No `.env` to hunt.

```bash
$ foresight init

╭────────────────────────── Setup Complete ───────────────────────────╮
│ Foresight is ready. Try:                                            │
│   foresight status          # Check health                          │
│   foresight store 'hello'  # Store a memory                        │
│   foresight list            # List memories                         │
│   foresight tui             # Launch the TUI                        │
╰────────────────────────────────────────────────────────────────────╯
```

The setup wizard creates `~/.foresight/config.json` and initializes your SQLite database.  
Done in under a second.

---

#### Step 3 — Doctor

Before you trust it, verify it.

```bash
$ foresight doctor

Foresight Diagnostics

  ✓ Python 3.11+
  ✓ Config dir exists
  ✓ Config file exists
  ✓ Database file exists
  ✓ User ID configured
  ✓ Bank ID configured
  ✓ Database responsive

All 7 checks passed (3 env overrides)
Active env overrides:
  FORESIGHT_DB_PATH=/home/vivi/.foresight/memory.db
  FORESIGHT_USER_ID=vivi
  FORESIGHT_BANK_ID=pixelated
```

7 green checks. Your memory system is healthy and ready.

---

#### Step 4 — Store, list, retrieve

```bash
$ foresight store "First real memory from the CLI walkthrough"

Memory stored: Stored memory 4d9440b9fb5c6961. Gate: auto. Reason: Normal
information flow.
```

Memory saved. Now list them all:

```bash
$ foresight list

Memories (4 found):
- [4d9440b9fb5c6961] (session/short_term) This is a test memory from the real
  CLI walkthrough
- [ee8983a30fa0305f] (session/short_term) verification test memory from
  end-to-end check
- [fa11bc68adc42b64] (session/short_term) Test memory from CLI build
- [1bed10c017d0b083] (trait/long_term) Purged 100K test artifacts from the
  default tenant's Foresight memory store.
```

Retrieve a specific one by ID:

```bash
$ foresight get 4d9440b9fb5c6961

╭───────────────────────── Memory 4d9440b9fb5c6961 ──────────────────────────╮
│ [4d9440b9fb5c6961] (session/short_term)                                    │
│ Content: This is a test memory from the real CLI walkthrough               │
│ Tags: RISK_NONE, fact                                                       │
╰─────────────────────────────────────────────────────────────────────────────╯
```

Search by keyword:

```bash
$ foresight query "test"

Found 4 memories (hybrid search):
- [fa11bc68adc42b64] Test memory from CLI build... (score=0.026, signals=keyword, temporal)
- [1bed10c017d0b083] Purged 100K test artifacts... (score=0.026, signals=keyword, temporal)
- [ee8983a30fa0305f] verification test memory from end-to-end check... (score=0.026, ...)
- [4d9440b9fb5c6961] This is a test memory from the real CLI walkthrough... (score=0.025, ...)
```

---

#### Step 5 — TUI

```bash
$ foresight tui
```

Full-screen Textual terminal UI. Three tabs:

| Tab           | What it does                                   |
| ------------- | ---------------------------------------------- |
| **Dashboard** | Live stat cards, recent activity, memory chart |
| **Memories**  | Browse every memory, search, store inline      |
| **Blocks**    | View and edit context blocks live              |

Keyboard navigation: `Tab` between tabs, `/` to search, `q` to quit.  
It feels like a mission control dashboard for your brain. Everything refreshes live.

---

#### Step 6 — Agent mode (machine output)

When Foresight calls your agent, it uses `--agent` for pipe-safe, parseable output:

```bash
$ foresight --agent status

[JSON] {"status": "healthy", "memory_count": 4, "by_scope": {"session": 3, "trait": 1}, "tenant_id": "default"}
```

Or pure JSON with `--json`:

```bash
$ foresight --json status

{
  "status": "healthy",
  "memory_count": 4,
  "by_scope": {
    "session": 3,
    "trait": 1
  },
  "tenant_id": "default"
}
```

---

#### Step 7 — Wire it to your AI agent

Add to any MCP-compatible agent. Here's the Claude Code config:

```json
// ~/.claude.json or claude_desktop_config.json
{
  "mcpServers": {
    "foresight": {
      "command": "uvx",
      "args": ["foresight-mcp"]
    }
  }
}
```

**Cursor** → Settings → MCP Servers → Add new:

```bash
Command: uvx
Arguments: foresight-mcp
```

**Goose** — same pattern, same command. Any stdio MCP client works.

Once connected, your agent gets Foresight as a built-in tool. It can store memories from conversations, search across everything you've told it, and pull context from three sessions ago — automatically, without you asking.

---

### Quick reference

```bash
# Store & retrieve
foresight store "text"                    # Store a memory
foresight get <id>                         # Get memory by ID
foresight list                             # List all memories (newest first)
foresight query "search term"              # Keyword + hybrid search
foresight search "term"                    # Advanced search with signals/scoring

# Analysis
foresight synthesize                      # Find patterns & contradictions
foresight reflect --period weekly          # Time-windowed reflection
foresight profile                          # Build user profile (static + dynamic)

# Data portability
foresight export memories.json             # Export to JSON file
foresight import memories.json             # Import from JSON file

# System
foresight doctor                           # 7-point diagnostics
foresight stats                            # Memory count, scope breakdown
foresight config                           # View/set config values
foresight init --force                     # Reinitialize (wipes data)

# Output modes
foresight --agent status                  # Machine-parseable: [JSON] {...}
foresight --json status                   # Pure JSON to stdout
foresight -o json status                  # Same as --json (short form)

# TUI
foresight tui                             # Full-screen Textual terminal UI
```

---

### Extras

- **Shell completion**: `foresight --install-completion`
- **Database path**: `export FORESIGHT_DB_PATH=/custom/path/memory.db`
- **Config file**: `~/.foresight/config.json`
- **Docker databases**: See [Installation Guide](https://foresight.vectorize.io/installation)

---

## Architecture

Foresight combines three layers:

### Core memory system

- **Structured memory storage** with scope, retention, tags, and emotional
  metadata
- **Safety-aware ingestion** with crisis detection and gate decisions
- **Synthesis and reflection** pipelines for trends, contradictions, and stance
  shifts
- **Versioning and archival** for long-lived memory maintenance

### Context blocks

Context blocks are the Foresight-native continuity surface for active guidance
and project state. They are persisted in SQLite and isolated by
`(user_id, tenant_id)`, so the same user can carry different continuity state
across tenants without leakage.

Default blocks:

- `core_directives`
- `guidance`
- `pending_items`
- `project_context`
- `session_patterns`
- `user_preferences`
- `self_improvement`
- `tool_guidelines`

### Curation runs

Curation runs are asynchronous jobs that reorganize an existing memory bank into
either a separate reviewable output bank or, when explicitly allowed, back into
the source bank through a staging-and-promotion flow.

- **Source bank preserved** by default in `reviewable_output` mode
- **Reviewable output bank** created automatically unless `output_mode=in_place`
- **Curator controls** for policy mode, tool access, and freeform instructions
- **Transcript-aware curation** when transcript bundles are provided with
  `tool_access=operate`
- **Safe in-place promotion**: `in_place` runs always use an auto-generated
  staging bank, then archive originals and promote staged rows only after a
  successful commit
- **Terminal-state reviewability** so failed or canceled runs leave any staged
  output untouched for inspection and do not overwrite the source bank

## Quick start

```bash
uv run foresight-mcp
uv run foresight --help
```

## Beginner friendly setup

1. Install **uv** if needed:

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. Clone the repository:

   ```bash
   git clone https://github.com/yourorg/foresight-mcp.git
   cd foresight-mcp
   ```

3. Install the package:

   ```bash
   pip install foresight-mcp[all]
   ```

4. Initialize your memory store:

   ```bash
   foresight init
   ```

5. Start the MCP server:

   ```bash
   uvx foresight-mcp
   ```

6. Explore the CLI:

   ```bash
   foresight --help
   foresight blocks --help
   foresight curate --help
   ```

## Add to your MCP client

### Claude Code

```json
{
  "mcpServers": {
    "foresight": {
      "command": "uv",
      "args": ["run", "-m", "foresight_mcp"],
      "cwd": "/path/to/foresight-mcp",
      "env": {
        "FORESIGHT_DB_PATH": "/home/user/.foresight/memory.db",
        "FORESIGHT_USER_ID": "username"
      }
    }
  }
}
```

### Goose

```yaml
extensions:
  foresight:
    args: ['run', '-m', 'foresight_mcp']
    cwd: /path/to/foresight-mcp
    env:
      FORESIGHT_DB_PATH: /home/user/.foresight/memory.db
      FORESIGHT_USER_ID: username
    type: stdio
```

### Other MCP clients

Use the same stdio pattern with `uv run -m foresight_mcp`.

## Public surfaces

### Memory tools

These are the actual MCP tool names exposed by the server:

- `manage_memories` — store, update, delete, or archive a memory
- `search_memories` — unified search/retrieval (ID lookup, keyword, hybrid)
- `manage_memory_versions` — diff and rollback memory versions
- `analyze_memories` — synthesize patterns or reflect over a time period
- `manage_context_blocks` — list, get, update, reset, or clear context blocks
- `manage_curation_runs` — create, get, list, cancel, or archive curation runs
- `inject_context` — surface relevant memories for a conversation
- `process_session_transcript` — extract memories from a session transcript

### Context block helpers

- `list_context_blocks`
- `get_context_block`
- `update_context_block`
- `add_context_guidance`
- `reset_context_block`
- `clear_context_block`
- `get_context_whisper`
- `get_context_snapshot`
- `manage_context_blocks`

### Curation workflow

- `manage_curation_runs`
- `ContextBlockAction`
- `CurationRunAction`
- CLI group: `foresight curate ...`

### Tool response contract

`manage_context_blocks` and `manage_curation_runs` return stable JSON envelopes:

```json
{
  "ok": true,
  "action": "get",
  "label": "guidance",
  "content": "Keep updates short and concrete."
}
```

Errors use the same envelope shape with `ok: false` and an `error.message`
field. The CLI `--json` mode prints these envelopes directly.

## Example usage

### Store a memory

```python
from foresight_mcp import store_memory

store_memory(
    content="User prefers short direct progress updates.",
    scope="session",
    retention="short_term",
    category="preference",
)
```

### Update continuity context

```python
from foresight_mcp import add_context_guidance, get_context_whisper

add_context_guidance("Keep updates short and concrete.", user_id="vivi")
whisper = get_context_whisper(user_id="vivi")
print(whisper)
```

### Create a reviewable curation run

```python
from foresight_mcp import CurationRunAction, manage_curation_runs

result = manage_curation_runs(
    CurationRunAction(
        action="create",
        source_bank_id="default",
        policy_mode="rebalance",
        tool_access="observe",
        output_mode="reviewable_output",
        instructions="Preserve durable preferences and merge duplicates.",
    ),
    user_id="vivi",
)
print(result)
```

### Run curation from the CLI

```bash
foresight curate create   --source-bank-id default   --policy-mode rebalance   --tool-access observe   --output-mode reviewable_output   --instructions "Preserve durable preferences and merge duplicates"
```

## Migration notes

Foresight now centers **context block** and **curation** terminology on the
public surface.

| Legacy name                 | Foresight-native name   |
| --------------------------- | ----------------------- |
| `manage_subconscious`       | `manage_context_blocks` |
| `get_subconscious_block`    | `get_context_block`     |
| `update_subconscious_block` | `update_context_block`  |
| `add_subconscious_guidance` | `add_context_guidance`  |
| `get_subconscious_whisper`  | `get_context_whisper`   |
| `get_subconscious_context`  | `get_context_snapshot`  |
| `reset_subconscious_block`  | `reset_context_block`   |
| `clear_subconscious_block`  | `clear_context_block`   |

Compatibility aliases remain in place for older clients, but new integrations
should use the Foresight-native names above.

## License

MIT
