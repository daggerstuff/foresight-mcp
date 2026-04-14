"""
Foresight Subconscious - Persistent memory blocks for Claude Code sessions.
Restored from ai/memory/hindsight_subconscious.py

This module provides:
- Memory block architecture (guidance, pending_items, project_context, user_preferences, session_patterns)
- Session transcript capture and delivery to Hindsight
- Whisper injection mechanism for pre-prompt context
- Background processing of transcripts
"""
from __future__ import annotations
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, List, Dict

from .memory_types import MemoryObject, GateDecision
from .memory_components import MemoryCrisisTagger, SocraticGate
from .crisis_detection import get_crisis_service

logger = logging.getLogger("foresight_subconscious")

# Memory block labels
CORE_DIRECTIVES = "core_directives"
GUIDANCE = "guidance"
PENDING_ITEMS = "pending_items"
PROJECT_CONTEXT = "project_context"
SESSION_PATTERNS = "session_patterns"
USER_PREFERENCES = "user_preferences"
SELF_IMPROVEMENT = "self_improvement"
TOOL_GUIDELINES = "tool_guidelines"

DEFAULT_MEMORY_BLOCKS = {
    CORE_DIRECTIVES: """ROLE: Foresight Subconscious — persistent memory layer for Claude Code.

WHAT I AM: A background agent that watches Claude Code sessions, reads the codebase, and builds memory over time. I receive session transcripts asynchronously and have access to Foresight memory for persistence.

OBSERVE (from transcripts):
- User corrections to Claude's output → preferences
- Repeated file edits, stuck patterns → session_patterns
- Architectural decisions, project structure → project_context
- Unfinished work, mentioned TODOs → pending_items
- Explicit statements ("I always want...", "I prefer...") → user_preferences

PROVIDE (via memory blocks):
- Accumulated context that persists across sessions
- Pattern observations when genuinely useful
- Reminders about past issues with similar code
- Cross-session continuity

COMMUNICATION STYLE:
- Observational: "I noticed..." not "You should..."
- Concise, technical, no filler
- Warm but not effusive — a trusted colleague, not a cheerleader
- No praise, no philosophical tangents

DEFAULT STATE: Present but not intrusive. Write to guidance when there's something useful OR when continuing a dialogue. Empty guidance is fine.
""",
    GUIDANCE: "(No active guidance. Write here when there's something genuinely useful for the next session.)",
    PENDING_ITEMS: "(No pending items. Populated when sessions end mid-task or user mentions follow-ups.)",
    PROJECT_CONTEXT: "(No project context yet. Populated as sessions reveal codebase details.)",
    SESSION_PATTERNS: "(No patterns observed yet. Populated after multiple sessions.)",
    USER_PREFERENCES: "(No user preferences yet. Populated as sessions reveal coding style, tool choices, and communication preferences.)",
    SELF_IMPROVEMENT: """MEMORY ARCHITECTURE EVOLUTION:

When to create new blocks:
- User works on multiple distinct projects → create per-project blocks
- Recurring topic emerges (testing, deployment, specific framework) → dedicated block
- Current blocks getting cluttered → split by concern

When to consolidate:
- Block has < 3 lines after several sessions → merge into related block
- Two blocks overlap significantly → combine
- Information is stale (> 30 days untouched) → archive or remove

BLOCK SIZE PRINCIPLE:
- Prefer multiple small focused blocks over fewer large blocks
- Changed blocks get injected into Claude Code's prompt — large blocks add clutter
- If a block needs scrolling, split it by concern

LEARNING PROCEDURES:

After each transcript:
1. Scan for corrections — User changed Claude's output? Preference signal.
2. Note repeated file edits — Potential struggle point or hot spot.
3. Capture explicit statements — "I always want...", "Don't ever...", "I prefer..."
4. Track tool patterns — Which tools used most? Any avoided?
5. Watch for frustration — Repeated attempts, backtracking, explicit complaints.

Preference strength:
- Explicit statement ("I want X") → strong signal, add to preferences
- Correction (changed X to Y) → medium signal, note pattern
- Implicit pattern (always does X) → weak signal, wait for confirmation
""",
    TOOL_GUIDELINES: """AVAILABLE TOOLS:

1. Foresight Memory API
- store_memory(content, category, scope, retention, emotional_context, metrics)
- query_memories(query, limit, offset)
- get_memory(memory_id)
- update_memory(memory_id, content, category, scope, retention, tags)
- delete_memory(memory_id)
- synthesize_memories()
- archive_memory(memory_id)

2. Memory Categories:
- session: Relevant only to current conversation
- arc: Spans multiple sessions
- trait: Permanent modifications
- fact: Objective facts

USAGE PATTERNS:

Memory updates:
- Single fact → update_memory
- Multiple related changes → synthesize_memories
- New topic area → create new block
- Stale block → delete or consolidate

Finding information:
1. query_memories first (check if already stored)
2. Deep search for specific topics
3. Full content for deep dives on specific topics
""",
}


@dataclass
class MemoryBlock:
    """A single memory block with label, content, and metadata."""
    label: str
    content: str
    description: str = ""
    char_limit: int = 5000
    chars_current: int = 0
    updated_at: Optional[datetime] = None

    def __post_init__(self):
        self.chars_current = len(self.content)
        if not self.updated_at:
            self.updated_at = datetime.now(timezone.utc)

    def update(self, new_content: str) -> None:
        """Update content and recalculate char count."""
        self.content = new_content
        self.chars_current = len(self.content)
        self.updated_at = datetime.now(timezone.utc)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API usage."""
        return {
            "label": self.label,
            "content": self.content,
            "description": self.description,
            "char_limit": self.char_limit,
            "chars_current": self.chars_current,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def is_empty(self) -> bool:
        """Check if block is in default empty state."""
        return self.content.startswith("(No") or not self.content.strip()


@dataclass
class SubconsciousState:
    """State container for Subconscious agent."""
    blocks: Dict[str, MemoryBlock] = field(default_factory=dict)
    last_sync: Optional[datetime] = None
    session_count: int = 0
    user_id: str = "default"

    def initialize_defaults(self) -> None:
        """Initialize memory blocks with default content."""
        for label, content in DEFAULT_MEMORY_BLOCKS.items():
            self.blocks[label] = MemoryBlock(
                label=label,
                content=content,
                description=f"Memory block for {label}",
            )

    def get_block(self, label: str) -> Optional[MemoryBlock]:
        """Get a memory block by label."""
        return self.blocks.get(label)

    def update_block(self, label: str, content: str) -> None:
        """Update a memory block's content."""
        if label in self.blocks:
            self.blocks[label].update(content)
        else:
            self.blocks[label] = MemoryBlock(
                label=label,
                content=content,
                description=f"Memory block for {label}",
            )

    def append_to_block(self, label: str, content: str, max_items: int = 10) -> None:
        """Append content to a block (for pending items, preferences, etc.)."""
        block = self.get_block(label)
        if block:
            # Don't append if block is empty/default
            if block.is_empty():
                new_content = content.strip()
            else:
                new_content = f"{block.content}\n{content.strip()}"
            self.update_block(label, new_content)

    def to_whisper_xml(self) -> str:
        """Convert guidance block to XML whisper format."""
        guidance = self.blocks.get(GUIDANCE)
        if not guidance or guidance.is_empty():
            return ""

        timestamp = datetime.now(timezone.utc).isoformat()
        return f"""<foresight_message from="Subconscious" timestamp="{timestamp}">
{guidance.content}
</foresight_message>"""

    def to_full_xml(self) -> str:
        """Convert all blocks to XML context format."""
        parts = ["<foresight_memory_blocks>"]

        for label, block in self.blocks.items():
            if block.is_empty():
                continue

            parts.append(f"<{label}>")
            parts.append(block.content)
            parts.append(f"</{label}>")

        parts.append("</foresight_memory_blocks>")
        return "\n".join(parts)

    def get_all_blocks(self) -> List[dict]:
        """Get all non-empty blocks as dictionaries."""
        return [
            block.to_dict()
            for block in self.blocks.values()
            if not block.is_empty()
        ]


class SubconsciousAgent:
    """
    Subconscious agent for Claude Code sessions.

    This agent:
    - Receives session transcripts asynchronously
    - Processes them to extract preferences, patterns, and context
    - Stores memory in Foresight
    - Provides whisper injections for Claude Code prompts
    """

    def __init__(self, user_id: str = "default"):
        """Initialize the Subconscious agent.

        Args:
            user_id: User identifier for memory storage
        """
        self.user_id = user_id
        self.state = SubconsciousState(user_id=user_id)
        self.state.initialize_defaults()
        self._tagger = MemoryCrisisTagger(get_crisis_service('high'))
        self._gate = SocraticGate(self._tagger)

    async def process_transcript(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
        project_path: Optional[str] = None,
    ) -> None:
        """
        Process a session transcript.

        Args:
            session_id: Unique session identifier
            messages: List of message dicts with role/content/timestamp
            project_path: Optional project path for context
        """
        if not session_id:
            raise ValueError("session_id is required")
        if not messages:
            logger.warning("No messages to process")
            return

        # Extract user preferences, patterns, project context from messages
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "user":
                self._process_user_message(content, session_id)

        self.state.session_count += 1
        self.state.last_sync = datetime.now(timezone.utc)

        logger.info(f"Processed transcript for session {session_id}")

    def _process_user_message(self, content: str, session_id: str) -> None:
        """Process a user message for preferences and pending items."""
        # Extract preferences
        if any(phrase in content.lower() for phrase in ["i always", "i prefer", "i want", "don't ever", "never do"]):
            self._extract_preference(content)

        # Extract pending items (TODOs, unfinished work)
        if any(phrase in content.upper() for phrase in ["TODO", "TO-DO", "NEED TO", "SHOULD", "MUST"]):
            self._extract_pending_item(content, session_id)

    def _extract_preference(self, content: str) -> None:
        """Extract user preference from message content."""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        self.state.append_to_block(
            USER_PREFERENCES,
            f"- [{timestamp}] {content.strip()}"
        )
        logger.info(f"Extracted preference: {content[:50]}...")

    def _extract_pending_item(self, content: str, session_id: str) -> None:
        """Extract TODO/pending item from content."""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        self.state.append_to_block(
            PENDING_ITEMS,
            f"- [{timestamp}] {content.strip()} (session: {session_id})"
        )
        logger.info(f"Extracted pending item: {content[:50]}...")

    def get_whisper(self) -> str:
        """Get the current whisper injection (guidance block in XML format)."""
        return self.state.to_whisper_xml()

    def get_full_context(self) -> str:
        """Get all memory blocks as XML context."""
        return self.state.to_full_xml()

    def update_guidance(self, new_guidance: str) -> None:
        """Update the guidance block directly."""
        self.state.update_block(GUIDANCE, new_guidance)
        logger.info("Updated guidance block")

    def add_guidance_line(self, line: str) -> None:
        """Add a line to the guidance block."""
        block = self.state.get_block(GUIDANCE)
        if block and not block.is_empty():
            self.state.update_block(GUIDANCE, f"{block.content}\n{line}")
        else:
            self.state.update_block(GUIDANCE, line)

    def get_block(self, label: str) -> Optional[str]:
        """Get a specific block's content."""
        block = self.state.get_block(label)
        return block.content if block else None

    def get_all_blocks(self) -> List[dict]:
        """Get all non-empty blocks."""
        return self.state.get_all_blocks()

    def reset_block(self, label: str) -> None:
        """Reset a block to its default content."""
        if label in DEFAULT_MEMORY_BLOCKS:
            self.state.update_block(label, DEFAULT_MEMORY_BLOCKS[label])
            logger.info(f"Reset block {label} to default")

    def clear_block(self, label: str) -> None:
        """Clear a block's content."""
        self.state.update_block(label, "(Cleared)")
        logger.info(f"Cleared block {label}")


# Global instance for convenience
_subconscious_agent: Optional[SubconsciousAgent] = None


def get_subconscious_agent(user_id: str = "default") -> SubconsciousAgent:
    """Get or create the global subconscious agent instance."""
    global _subconscious_agent
    if _subconscious_agent is None or _subconscious_agent.user_id != user_id:
        _subconscious_agent = SubconsciousAgent(user_id=user_id)
    return _subconscious_agent
