"""Foresight MCP Server - Persistent memory for Claude Code."""
from .server import mcp, store_memory, query_memories, list_memories, get_memory, update_memory, delete_memory, memory_status

__version__ = "1.0.0"
__all__ = ["mcp", "store_memory", "query_memories", "list_memories", "get_memory", "update_memory", "delete_memory", "memory_status"]
