"""
Foresight context blocks - persistent continuity blocks for Foresight sessions.
Compatibility kept for older subconscious-named integrations.

This module provides:
- Context block architecture (guidance, pending_items, project_context, user_preferences, session_patterns)
- Session transcript capture and delivery to Foresight
- Whisper injection mechanism for pre-prompt context
- Background curation of transcript-derived continuity
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .block_registry import MemoryBlockSchema as RegisteredMemoryBlockSchema, get_registry, initialize_default_blocks
from .config import DB_PATH
from .connection_pool import get_pool
from .memory_components import MemoryCrisisTagger, SocraticGate

logger = logging.getLogger("foresight_context_blocks")

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
    CORE_DIRECTIVES: """ROLE: Foresight Curator — background continuity and curation layer for Foresight.

WHAT I AM: A background curator that watches Foresight sessions, reads the codebase, and builds memory over time. I receive session transcripts asynchronously and have access to Foresight memory for persistence.

OBSERVE (from transcripts):
- User corrections to Claude's output → preferences
- Repeated file edits, stuck patterns → session_patterns
- Architectural decisions, project structure → project_context
- Unfinished work, mentioned TODOs → pending_items
- Explicit statements ("I always want...", "I prefer...") → user_preferences

PROVIDE (via context blocks):
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
    updated_at: datetime | None = None

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
class ContextBlockState:
    """State container for the Foresight context block agent."""

    blocks: dict[str, MemoryBlock] = field(default_factory=dict)
    last_sync: datetime | None = None
    session_count: int = 0
    user_id: str = "default"
    tenant_id: str = "default"

    def initialize_defaults(self) -> None:
        """Initialize context blocks with registered default schemas and content."""
        registry = initialize_default_blocks()
        for label, content in DEFAULT_MEMORY_BLOCKS.items():
            schema = registry.get_schema(label)
            self.blocks[label] = MemoryBlock(
                label=label,
                content=content,
                description=schema.description if schema else f"Memory block for {label}",
                char_limit=schema.char_limit or 5000 if schema else 5000,
            )

    def register_schema(self, schema: RegisteredMemoryBlockSchema) -> None:
        """Register a custom context block schema for this process."""
        get_registry().register(schema)

    def _schema_for(self, label: str) -> RegisteredMemoryBlockSchema | None:
        """Return the registered schema for a label, including defaults."""
        return initialize_default_blocks().get_schema(label)

    def _known_labels(self) -> list[str]:
        """Return sorted labels that may be addressed by update/reset operations."""
        labels = set(DEFAULT_MEMORY_BLOCKS) | set(self.blocks)
        labels.update(schema.label for schema in initialize_default_blocks().list_schemas())
        return sorted(labels)

    def _validate_block_content(self, label: str, content: str) -> None:
        """Validate content against a registered schema when one exists."""
        schema = self._schema_for(label)
        if schema is None:
            return
        is_valid, message = schema.validate_content(content)
        if not is_valid:
            raise ValueError(f"Invalid content for block {label!r}: {message}")

    def get_block(self, label: str) -> MemoryBlock | None:
        """Get a context block by label."""
        return self.blocks.get(label)

    def update_block(self, label: str, content: str) -> None:
        """Update a context block's content.

        ``label`` must be one of the predefined block names or an existing
        custom block already in ``self.blocks``.  Arbitrary labels are
        rejected to prevent typos silently creating orphan blocks.
        """
        schema = self._schema_for(label)
        if label not in DEFAULT_MEMORY_BLOCKS and label not in self.blocks and schema is None:
            raise ValueError(f"Unknown block label {label!r}. Must be one of: {self._known_labels()}")
        self._validate_block_content(label, content)
        if label in self.blocks:
            self.blocks[label].update(content)
        else:
            self.blocks[label] = MemoryBlock(
                label=label,
                content=content,
                description=schema.description if schema else f"Memory block for {label}",
                char_limit=schema.char_limit or 5000 if schema else 5000,
            )

    def append_to_block(self, label: str, content: str, max_items: int = 10) -> None:
        """Append content to a block (for pending items, preferences, etc.)."""
        block = self.get_block(label)
        if block:
            # Don't append if block is empty/default.
            if max_items != 10:
                logger.debug("append_to_block max_items is reserved for future trimming: %s", max_items)
            new_content = content.strip() if block.is_empty() else f"{block.content}\n{content.strip()}"
            self.update_block(label, new_content)

    def to_whisper_xml(self) -> str:
        """Convert guidance block to XML whisper format."""
        guidance = self.blocks.get(GUIDANCE)
        if not guidance or guidance.is_empty():
            return ""

        timestamp = datetime.now(timezone.utc).isoformat()
        return f"""<foresight_message from="Foresight Curator" timestamp="{timestamp}">
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

    def get_all_blocks(self) -> list[dict]:
        """Get all non-empty blocks as dictionaries."""
        return [block.to_dict() for block in self.blocks.values() if not block.is_empty()]


class ContextBlockAgent:
    """
    Foresight context block agent for Foresight sessions.

    This agent:
    - Receives session transcripts asynchronously
    - Processes them to extract preferences, patterns, and context
    - Stores memory in Foresight
    - Provides whisper injections for Claude Code prompts
    """

    def __init__(self, user_id: str = "default", tenant_id: str = "default"):
        """Initialize the context block agent.

        Args:
            user_id: User identifier for memory storage
            tenant_id: Tenant identifier for memory isolation
        """
        self.user_id = user_id
        self.tenant_id = tenant_id
        self._lock = threading.RLock()
        self.state = ContextBlockState(user_id=user_id, tenant_id=tenant_id)
        self.state.initialize_defaults()
        self._load_persisted_blocks()
        self._tagger = MemoryCrisisTagger()
        self._gate = SocraticGate(self._tagger)

    def _connect(self):
        return get_pool(DB_PATH).acquire()

    def _ensure_storage(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS context_blocks (
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    user_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    content TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, user_id, label)
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_context_blocks_lookup "
                "ON context_blocks(tenant_id, user_id, updated_at DESC)"
            )
            conn.commit()
        finally:
            conn.close()

    def _load_persisted_blocks(self) -> None:
        """Overlay persisted blocks onto the default in-memory state."""
        self._ensure_storage()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT label, content, updated_at FROM context_blocks WHERE tenant_id = ? AND user_id = ?",
                (self.tenant_id, self.user_id),
            ).fetchall()
        finally:
            conn.close()

        for row in rows:
            label = row["label"]
            block = self.state.get_block(label)
            updated_at = datetime.fromisoformat(row["updated_at"])
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            if block:
                block.content = row["content"]
                block.chars_current = len(row["content"])
                block.updated_at = updated_at
            else:
                schema = self.state._schema_for(label)
                self.state.blocks[label] = MemoryBlock(
                    label=label,
                    content=row["content"],
                    description=schema.description if schema else f"Memory block for {label}",
                    char_limit=schema.char_limit or 5000 if schema else 5000,
                    updated_at=updated_at,
                )

    def _persist_block(self, label: str) -> None:
        """Persist one block for the current user and tenant."""
        self._ensure_storage()
        block = self.state.get_block(label)
        if block is None:
            return
        updated_at = (block.updated_at or datetime.now(timezone.utc)).isoformat()
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO context_blocks (tenant_id, user_id, label, content, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, user_id, label)
                DO UPDATE SET content = excluded.content, updated_at = excluded.updated_at""",
                (self.tenant_id, self.user_id, label, block.content, updated_at),
            )
            conn.commit()
        finally:
            conn.close()

    async def process_transcript(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        project_path: str | None = None,
    ) -> None:
        """
        Process a session transcript.

        Args:
            session_id: Unique session identifier
            messages: List of message dicts — each must have 'role' (str) and
                      'content' (str) keys. Extra keys are ignored.
            project_path: Optional project path for context
        """
        if not session_id:
            raise ValueError("session_id is required")
        if not messages:
            logger.warning("No messages to process")
            return

        if project_path:
            logger.debug("Processing transcript with project path: %s", project_path)

        valid_roles = {"user", "assistant", "system", "tool"}
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                raise ValueError(f"messages[{i}] must be a dict, got {type(msg).__name__}")
            if "role" not in msg or not isinstance(msg.get("role"), str):
                raise ValueError(f"messages[{i}] missing required 'role' string field")
            if "content" not in msg or not isinstance(msg.get("content"), str):
                raise ValueError(f"messages[{i}] missing required 'content' string field")
            if msg["role"] not in valid_roles:
                raise ValueError(
                    f"messages[{i}] has invalid role {msg['role']!r}; must be one of {sorted(valid_roles)}"
                )

        touched_labels: set[str] = set()
        with self._lock:
            for msg in messages:
                if msg["role"] == "user":
                    touched_labels.update(self._process_user_message(msg["content"], session_id))

            self.state.session_count += 1
            self.state.last_sync = datetime.now(timezone.utc)
            for label in touched_labels:
                self._persist_block(label)
        logger.info("Processed transcript for session %s", session_id)

    def _process_user_message(self, content: str, session_id: str) -> set[str]:
        """Process a user message for preferences and pending items."""
        touched_labels: set[str] = set()
        # Extract preferences
        if any(phrase in content.lower() for phrase in ["i always", "i prefer", "i want", "don't ever", "never do"]):
            touched_labels.add(self._extract_preference(content))

        # Extract pending items (TODOs, unfinished work)
        if any(phrase in content.upper() for phrase in ["TODO", "TO-DO", "NEED TO", "SHOULD", "MUST"]):
            touched_labels.add(self._extract_pending_item(content, session_id))
        return touched_labels

    def _extract_preference(self, content: str) -> str:
        """Extract user preference from message content."""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        self.state.append_to_block(USER_PREFERENCES, f"- [{timestamp}] {content.strip()}")
        logger.info(f"Extracted preference: {content[:50]}...")
        return USER_PREFERENCES

    def _extract_pending_item(self, content: str, session_id: str) -> str:
        """Extract TODO/pending item from content."""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        self.state.append_to_block(PENDING_ITEMS, f"- [{timestamp}] {content.strip()} (session: {session_id})")
        logger.info(f"Extracted pending item: {content[:50]}...")
        return PENDING_ITEMS

    def get_whisper(self) -> str:
        """Get the current whisper injection (guidance block in XML format)."""
        return self.state.to_whisper_xml()

    def get_full_context(self) -> str:
        """Get all context blocks as XML context."""
        return self.state.to_full_xml()

    def update_guidance(self, new_guidance: str) -> None:
        """Update the guidance block directly."""
        with self._lock:
            self.state.update_block(GUIDANCE, new_guidance)
            self._persist_block(GUIDANCE)
        logger.info("Updated guidance block")

    def register_schema(self, schema: RegisteredMemoryBlockSchema) -> None:
        """Register a custom context block schema for this process."""
        with self._lock:
            self.state.register_schema(schema)

    def update_block(self, label: str, content: str) -> None:
        """Update any context block and persist the change."""
        if label == GUIDANCE:
            self.update_guidance(content)
            return
        with self._lock:
            self.state.update_block(label, content)
            self._persist_block(label)
        logger.info("Updated block %s", label)

    def add_guidance_line(self, line: str) -> None:
        """Add a line to the guidance block."""
        with self._lock:
            block = self.state.get_block(GUIDANCE)
            if block and not block.is_empty():
                self.state.update_block(GUIDANCE, f"{block.content}\n{line}")
            else:
                self.state.update_block(GUIDANCE, line)
            self._persist_block(GUIDANCE)

    def get_block(self, label: str) -> str | None:
        """Get a specific block's content."""
        with self._lock:
            block = self.state.get_block(label)
            return block.content if block else None

    def get_all_blocks(self) -> list[dict]:
        """Get all non-empty context blocks."""
        with self._lock:
            return self.state.get_all_blocks()

    def reset_block(self, label: str) -> None:
        """Reset a block to its default content."""
        if label in DEFAULT_MEMORY_BLOCKS:
            with self._lock:
                self.state.update_block(label, DEFAULT_MEMORY_BLOCKS[label])
                self._persist_block(label)
            logger.info(f"Reset block {label} to default")
            return
        schema = self.state._schema_for(label)
        if schema is not None:
            with self._lock:
                self.state.update_block(label, "")
                self._persist_block(label)
            logger.info("Reset custom block %s to empty content", label)
            return
        raise ValueError(f"Unknown block label {label!r}. Must be one of: {self.state._known_labels()}")

    def clear_block(self, label: str) -> None:
        """Clear a block's content."""
        with self._lock:
            self.state.update_block(label, "")
            self._persist_block(label)
        logger.info(f"Cleared block {label}")


SubconsciousState = ContextBlockState
SubconsciousAgent = ContextBlockAgent


# Global instances keyed by user and tenant for isolation
_context_block_agents: dict[tuple[str, str], ContextBlockAgent] = {}
_CONTEXT_BLOCK_AGENTS_LOCK = threading.Lock()


def _normalize_tenant_id(tenant_id: str | None) -> str:
    """Normalize optional tenant IDs into a stable cache key."""
    normalized = (tenant_id or "").strip()
    return normalized or "default"


def get_context_block_agent(user_id: str, tenant_id: str = "default") -> ContextBlockAgent:
    """Get or create the context block agent instance for one user+tenant."""
    key = (user_id, _normalize_tenant_id(tenant_id))
    with _CONTEXT_BLOCK_AGENTS_LOCK:
        agent = _context_block_agents.get(key)
        if agent is None:
            agent = ContextBlockAgent(user_id=user_id, tenant_id=key[1])
            _context_block_agents[key] = agent
        return agent


def get_subconscious_agent(user_id: str, tenant_id: str = "default") -> ContextBlockAgent:
    """Compatibility wrapper for older subconscious-named integrations."""
    return get_context_block_agent(user_id, tenant_id)
