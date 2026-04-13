# Foresight MCP Server

Persistent memory for AI agents via MCP protocol.

Compatible with Claude Code, Goose, Cursor, and any MCP-compatible AI agent.

## Quick Start

```bash
uv run foresight-mcp
```

## Add to Your MCP Client

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

Add to your Goose configuration (`~/.config/goose/config.yaml`):

```yaml
extensions:
  foresight:
    args: ["run", "-m", "foresight_mcp"]
    cwd: /path/to/foresight-mcp
    env:
      FORESIGHT_DB_PATH: /home/user/.foresight/memory.db
      FORESIGHT_USER_ID: username
    type: stdio
```

### Cursor / Other MCP Clients

Use the same configuration pattern as Claude Code, adjusting for your client's specific config format.

## Tools

- `store_memory` - Store memory
- `query_memories` - Search memories
- `list_memories` - List memories
- `get_memory` - Get memory
- `update_memory` - Update memory
- `delete_memory` - Delete memory
- `memory_status` - System status

## License

MIT
