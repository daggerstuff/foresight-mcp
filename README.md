# Foresight MCP Server

Persistent memory for Claude Code via MCP protocol.

## Quick Start

```bash
uv run foresight-mcp
```

## Add to Claude Code

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
