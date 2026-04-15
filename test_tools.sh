#!/bin/bash
# Test script to call Foresight MCP tools without going through the MCP client

cd /home/vivi/pixelated/foresight-mcp

echo "Testing memory_status..."
uv run python -c "
from foresight_mcp.server import memory_status
print(memory_status())
"

echo ""
echo "Testing store_memory..."
uv run python -c "
from foresight_mcp.server import store_memory
result = store_memory('Test from shell script', category='debug', scope='session')
print(result)
"

echo ""
echo "Testing list_memories..."
uv run python -c "
from foresight_mcp.server import list_memories
print(list_memories(limit=5))
"
