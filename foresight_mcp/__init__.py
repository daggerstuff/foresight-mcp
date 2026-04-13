"""
Foresight MCP Server - Full memory system with psychological safety features.
Restored from src/lib/ai/memory/ architecture.
"""
from .server import (
    mcp,
    store_memory,
    query_memories,
    list_memories,
    get_memory,
    update_memory,
    delete_memory,
    memory_status,
    synthesize_memories,
    archive_memory,
)

__version__ = "1.0.0"
__all__ = [
    "mcp",
    "store_memory",
    "query_memories",
    "list_memories",
    "get_memory",
    "update_memory",
    "delete_memory",
    "memory_status",
    "synthesize_memories",
    "archive_memory",
]
