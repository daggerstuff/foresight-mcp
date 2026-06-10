# Foresight MCP Server

**Persistent memory for AI agents with safety-aware memory storage,
Foresight-native context blocks, and reviewable curation runs.**

Foresight provides a local MCP server plus Python and CLI helpers for storing
memories, maintaining continuity context, and curating long-lived memory banks
into cleaner reviewable outputs.

Compatible with Claude Code, Goose, Cursor, and other MCP-compatible AI agents.

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

3. Run setup:

   ```bash
   ./scripts/setup.sh
   ```

4. Start the MCP server:

   ```bash
   uv run foresight-mcp
   ```

5. Explore the CLI:

   ```bash
   uv run foresight --help
   uv run foresight blocks --help
   uv run foresight curate --help
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

- `store_memory`
- `query_memories`
- `list_memories`
- `get_memory`
- `update_memory`
- `delete_memory`
- `memory_status`
- `synthesize_memories`
- `reflect_on_memories`
- `process_session_transcript`

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

| Legacy name | Foresight-native name |
| --- | --- |
| `manage_subconscious` | `manage_context_blocks` |
| `get_subconscious_block` | `get_context_block` |
| `update_subconscious_block` | `update_context_block` |
| `add_subconscious_guidance` | `add_context_guidance` |
| `get_subconscious_whisper` | `get_context_whisper` |
| `get_subconscious_context` | `get_context_snapshot` |
| `reset_subconscious_block` | `reset_context_block` |
| `clear_subconscious_block` | `clear_context_block` |

Compatibility aliases remain in place for older clients, but new integrations
should use the Foresight-native names above.

## License

MIT
