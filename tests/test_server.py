"""Tests for Foresight MCP server."""
from foresight_mcp import store_memory, memory_status

def test_store_memory():
    result = store_memory("test")
    assert "Stored" in result

def test_status():
    result = memory_status()
    assert "healthy" in result.lower()
