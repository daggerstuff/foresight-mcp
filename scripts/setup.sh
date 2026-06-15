#!/usr/bin/env bash

# Exit on any error
set -e

echo "Setting up Foresight MCP..."

# Ensure uv is installed (skip if already present)
if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found, installing..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

# Install Python dependencies with all extras (CLI + TUI)
uv sync --extra all

# Create symlink for one-liner access (optional)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -d "$HOME/.local/bin" ]; then
  if [ ! -f "$HOME/.local/bin/foresight" ]; then
    ln -sf "$SCRIPT_DIR/foresight" "$HOME/.local/bin/foresight"
    echo "  → Created symlink: ~/.local/bin/foresight"
  fi
fi

# Create memory directory with secure permissions
MEMORY_DIR="$HOME/.foresight"
mkdir -p "$MEMORY_DIR"
chmod 700 "$MEMORY_DIR"

# Initialize foresight config and DB
python -m foresight_cli.cli init 2>/dev/null || true

echo ""
echo "Setup complete! 🚀"
echo ""
echo "  Interactive TUI:    foresight tui"
echo "  CLI commands:       foresight --help"
echo "  Agent mode:         foresight --agent status"
echo "  JSON mode:          foresight --json status"
echo "  MCP server:         uv run foresight-mcp"
echo ""
