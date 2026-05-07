# Installing Foresight MCP

## Option 1: Install from PyPI (recommended)

```bash
# Install with pip
pip install foresight-mcp

# Or with uv
uv add foresight-mcp
```

## Option 2: Run directly from repo

```bash
# Clone the repo
git clone https://github.com/your-org/foresight-mcp.git
cd foresight-mcp

# Install with uv
uv sync

# Run the server
uv run foresight-mcp
```

## Option 3: Development mode

```bash
git clone https://github.com/your-org/foresight-mcp.git
cd foresight-mcp

# Install in editable mode
uv sync --dev

# Run tests
uv run pytest

# Run server
uv run foresight-mcp
```

## Add to Claude Code

After installation, add to your `~/.claude/settings.json` or project's
`.mcp.json`:

```json
{
  "mcpServers": {
    "foresight": {
      "command": "uv",
      "args": ["run", "foresight-mcp"],
      "env": {
        "FORESIGHT_DB_PATH": "/home/user/.foresight/memory.db",
        "FORESIGHT_USER_ID": "username"
      }
    }
  }
}
```

## Verify installation

```bash
# Check version
uv run foresight-mcp --version

# Or test connection
foresight-mcp --health
```
