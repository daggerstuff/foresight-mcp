"""
Foresight MCP Server - Full memory system with psychological safety features.
Restored from src/lib/ai/memory/ architecture.

Includes:
- MemoryObject with emotional context and empathy metrics
- Socratic Gate for psychological safety
- Anomaly Detection (mental health, extensible to other domains)
- Memory Synthesizer for reconciliation and stance shift detection
- Memory Linker for vector store and ghost nodes
- Composable memory block schemas with BlockRegistry
- Subconscious memory blocks (guidance, pending_items, preferences, patterns)
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
    # Subconscious tools
    get_subconscious_blocks,
    get_subconscious_block,
    update_subconscious_block,
    add_subconscious_guidance,
    get_subconscious_whisper,
    get_subconscious_context,
    reset_subconscious_block,
    clear_subconscious_block,
    process_session_transcript,
)
# Block registry exports
from .block_registry import (
    BlockRegistry,
    MemoryBlockSchema,
    MemoryBlock,
    RetentionPolicy,
    MergeStrategy,
    InjectionPoint,
    BlockScope,
    get_registry,
    initialize_default_blocks,
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
    # Subconscious
    "get_subconscious_blocks",
    "get_subconscious_block",
    "update_subconscious_block",
    "add_subconscious_guidance",
    "get_subconscious_whisper",
    "get_subconscious_context",
    "reset_subconscious_block",
    "clear_subconscious_block",
    "process_session_transcript",
    # Block registry
    "BlockRegistry",
    "MemoryBlockSchema",
    "MemoryBlock",
    "RetentionPolicy",
    "MergeStrategy",
    "InjectionPoint",
    "BlockScope",
    "get_registry",
    "initialize_default_blocks",
]
