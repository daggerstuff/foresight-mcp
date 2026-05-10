"""
Public Foresight-native context block helpers.

This module provides the renamed public surface for working with continuity
blocks while reusing the existing compatibility-backed implementation.
"""

from __future__ import annotations

from .subconscious import (
    CORE_DIRECTIVES,
    DEFAULT_MEMORY_BLOCKS,
    GUIDANCE,
    PENDING_ITEMS,
    PROJECT_CONTEXT,
    SELF_IMPROVEMENT,
    SESSION_PATTERNS,
    TOOL_GUIDELINES,
    USER_PREFERENCES,
    ContextBlockAgent,
    ContextBlockState,
    MemoryBlock,
    get_context_block_agent as _get_context_block_agent,
)

DEFAULT_CONTEXT_BLOCKS = DEFAULT_MEMORY_BLOCKS
ContextBlock = MemoryBlock


def get_context_block_agent(user_id: str, tenant_id: str = "default") -> ContextBlockAgent:
    """Return the Foresight-native context block agent facade."""
    return _get_context_block_agent(user_id, tenant_id)


def list_context_blocks(user_id: str, tenant_id: str = "default") -> list[dict]:
    """List non-empty context blocks for a user."""
    return get_context_block_agent(user_id, tenant_id).get_all_blocks()


def get_context_block(label: str, user_id: str, tenant_id: str = "default") -> str | None:
    """Return a single context block by label."""
    return get_context_block_agent(user_id, tenant_id).get_block(label)


def update_context_block(label: str, content: str, user_id: str, tenant_id: str = "default") -> None:
    """Update a context block."""
    agent = get_context_block_agent(user_id, tenant_id)
    agent.update_block(label, content)


def add_context_guidance(line: str, user_id: str, tenant_id: str = "default") -> None:
    """Append a line to the guidance block."""
    get_context_block_agent(user_id, tenant_id).add_guidance_line(line)


def reset_context_block(label: str, user_id: str, tenant_id: str = "default") -> None:
    """Reset a context block to its default content."""
    get_context_block_agent(user_id, tenant_id).reset_block(label)


def clear_context_block(label: str, user_id: str, tenant_id: str = "default") -> None:
    """Clear a context block."""
    get_context_block_agent(user_id, tenant_id).clear_block(label)


def get_context_whisper(user_id: str, tenant_id: str = "default") -> str:
    """Return the whisper-ready guidance block payload."""
    return get_context_block_agent(user_id, tenant_id).get_whisper()


def get_context_snapshot(user_id: str, tenant_id: str = "default") -> str:
    """Return the full XML snapshot of non-empty context blocks."""
    return get_context_block_agent(user_id, tenant_id).get_full_context()


def get_subconscious_block(label: str, user_id: str, tenant_id: str = "default") -> str | None:
    """Compatibility alias for older subconscious-named integrations."""
    return get_context_block(label, user_id, tenant_id)


def update_subconscious_block(label: str, content: str, user_id: str, tenant_id: str = "default") -> None:
    """Compatibility alias for older subconscious-named integrations."""
    update_context_block(label, content, user_id, tenant_id)


def add_subconscious_guidance(line: str, user_id: str, tenant_id: str = "default") -> None:
    """Compatibility alias for older subconscious-named integrations."""
    add_context_guidance(line, user_id, tenant_id)


def get_subconscious_whisper(user_id: str, tenant_id: str = "default") -> str:
    """Compatibility alias for older subconscious-named integrations."""
    return get_context_whisper(user_id, tenant_id)


def get_subconscious_context(user_id: str, tenant_id: str = "default") -> str:
    """Compatibility alias for older subconscious-named integrations."""
    return get_context_snapshot(user_id, tenant_id)


def reset_subconscious_block(label: str, user_id: str, tenant_id: str = "default") -> None:
    """Compatibility alias for older subconscious-named integrations."""
    reset_context_block(label, user_id, tenant_id)


def clear_subconscious_block(label: str, user_id: str, tenant_id: str = "default") -> None:
    """Compatibility alias for older subconscious-named integrations."""
    clear_context_block(label, user_id, tenant_id)
