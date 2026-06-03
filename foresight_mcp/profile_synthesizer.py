"""
User Profile Synthesis — auto-build static + dynamic profiles from
context blocks and memories.

Inspired by supermemory.ai's profile concept: a single ~50ms call that
compacts a user's state into static (stable facts) and dynamic (recent
context) layers, directly injectable into LLM system prompts.

Static sources:
  - user_preferences context block
  - Memories with scope=trait|fact and retention=long_term|permanent

Dynamic sources:
  - project_context, session_patterns, pending_items context blocks
  - Recent memories with scope=session|arc
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from .config import DB_PATH
from .connection_pool import get_pool
from .context_blocks import (
    PENDING_ITEMS,
    PROJECT_CONTEXT,
    SESSION_PATTERNS,
    USER_PREFERENCES,
    get_context_block_agent,
)
from .enhanced_synthesizer import get_enhanced_synthesizer
from .memory_types import EmotionalMetadata, MemoryObject

logger = logging.getLogger("foresight_profile")


@dataclass
class ProfileConfig:
    """Tuning parameters for profile synthesis."""

    max_static_memories: int = 20
    max_dynamic_memories: int = 10
    include_synthesis: bool = True
    max_static_lines: int = 30
    max_dynamic_lines: int = 20


# Block labels used for each profile layer
_STATIC_BLOCK_LABELS = [USER_PREFERENCES]
_DYNAMIC_BLOCK_LABELS = [PROJECT_CONTEXT, SESSION_PATTERNS, PENDING_ITEMS]

# Placeholder lines that should not be surfaced in a profile
_PLACEHOLDER_PREFIXES = (
    "(No",
    "(no",
    "No ",
    "ROLE:",
    "WHAT I AM:",
    "WHAT I DO:",
    "COMMUNICATION STYLE:",
    "DEFAULT STATE:",
    "AVAILABLE TOOLS:",
    "MEMORY ARCHITECTURE EVOLUTION:",
    "LEARNING PROCEDURES:",
)


def _is_placeholder(line: str) -> bool:
    """Return True if *line* is a default/placeholder block entry."""
    stripped = line.strip()
    if not stripped:
        return True
    return stripped.startswith(_PLACEHOLDER_PREFIXES)


def _extract_block_lines(
    agent: Any,
    labels: list[str],
    *,
    max_per_block: int = 8,
) -> list[str]:
    """Extract non-placeholder lines from one or more context blocks."""
    lines: list[str] = []
    for label in labels:
        content = agent.get_block(label)
        if not content:
            continue
        for line in content.splitlines():
            clean = line.strip()
            if not _is_placeholder(clean):
                lines.append(clean)
                if len(lines) >= max_per_block * len(labels):
                    break
    return lines


def _query_memories_by_scope(
    user_id: str,
    tenant_id: str,
    scopes: tuple[str, ...],
    retentions: tuple[str, ...] | None = None,
    *,
    limit: int = 20,
    order_by: str = "importance DESC, created_at DESC",
) -> list[dict[str, Any]]:
    """Query memories filtered by scope and optionally retention."""
    pool = get_pool(DB_PATH)
    conn = pool.acquire()
    try:
        params: list[Any] = [user_id, tenant_id]
        scope_placeholders = ",".join("?" for _ in scopes)
        params.extend(scopes)

        retention_clause = ""
        if retentions:
            ret_placeholders = ",".join("?" for _ in retentions)
            retention_clause = f"AND retention IN ({ret_placeholders})"
            params.extend(retentions)

        rows = conn.execute(
            f"""SELECT content, category, tags, importance, strength_trend, scope,
                       retention, created_at
                FROM memories
                WHERE user_id = ? AND tenant_id = ? AND is_ghost = 0
                  AND scope IN ({scope_placeholders})
                  {retention_clause}
                ORDER BY {order_by}
                LIMIT ?""",
            (*params, limit),
        ).fetchall()
    finally:
        conn.close()

    results: list[dict[str, Any]] = []
    for r in rows:
        tags_raw = r["tags"]
        if isinstance(tags_raw, str):
            try:
                tags_raw = json.loads(tags_raw)
            except (json.JSONDecodeError, TypeError):
                tags_raw = []
        results.append(
            {
                "content": r["content"],
                "category": r["category"],
                "tags": tags_raw,
                "importance": r["importance"],
                "strength_trend": r["strength_trend"],
                "scope": r["scope"],
                "retention": r["retention"],
                "created_at": r["created_at"],
            }
        )
    return results


def _deduplicate_lines(lines: list[str]) -> list[str]:
    """Remove near-duplicate lines while preserving order."""
    seen: set[str] = set()
    unique: list[str] = []
    for line in lines:
        key = line.strip().lower()[:120]
        if key not in seen:
            seen.add(key)
            unique.append(line)
    return unique


def synthesize_profile(
    user_id: str,
    tenant_id: str = "default",
    config: ProfileConfig | None = None,
) -> dict[str, list[str]]:
    """
    Build a user profile with static (stable facts) and dynamic (recent context) layers.

    Args:
        user_id: User identifier.
        tenant_id: Tenant identifier.
        config: Tuning parameters (max memories, synthesis toggle, etc.).

    Returns:
        ``{"static": [str, ...], "dynamic": [str, ...]}``
    """
    cfg = config or ProfileConfig()
    pool = get_pool(DB_PATH)
    conn = pool.acquire()
    try:
        agent = get_context_block_agent(user_id, tenant_id)

        # ── Static layer ────────────────────────────────────────────────
        static_lines: list[str] = []

        # 1. Context-block preferences
        static_lines.extend(_extract_block_lines(agent, _STATIC_BLOCK_LABELS))

        # 2. Stable (trait/fact) memories with long retention
        static_mems = _query_memories_by_scope(
            user_id,
            tenant_id,
            scopes=("trait", "fact"),
            retentions=("long_term", "permanent"),
            limit=cfg.max_static_memories,
        )
        for m in static_mems:
            tag_str = f" [{', '.join(m['tags'][:3])}]" if m.get("tags") else ""
            static_lines.append(f"{m['content']}{tag_str}")

        # ── Dynamic layer ───────────────────────────────────────────────
        dynamic_lines: list[str] = []

        # 1. Context-block project state, patterns, pending items
        dynamic_lines.extend(_extract_block_lines(agent, _DYNAMIC_BLOCK_LABELS))

        # 2. Recent session/arc memories
        dyn_mems = _query_memories_by_scope(
            user_id,
            tenant_id,
            scopes=("session", "arc"),
            limit=cfg.max_dynamic_memories,
            order_by="created_at DESC",
        )
        for m in dyn_mems:
            dynamic_lines.append(m["content"])

        # ── Optional synthesis on static memories ───────────────────────
        if cfg.include_synthesis and len(static_mems) >= 5:
            try:
                memories_objs: list[MemoryObject] = []
                for m in static_mems[:15]:
                    emo = EmotionalMetadata(intensity=0.5) if m.get("importance", 0.5) > 0.6 else None
                    memories_objs.append(
                        MemoryObject(
                            id=f"prof_{hash(m['content'])}",
                            timestamp=m.get("created_at", ""),
                            scope=m.get("scope", "trait"),
                            retention=m.get("retention", "long_term"),
                            content=m["content"],
                            tags=m.get("tags", []),
                            emotional_context=emo,
                        )
                    )
                synth_result = get_enhanced_synthesizer().synthesize(memories_objs, user_id=user_id)
                # If contradictions found, note them
                if synth_result and synth_result.contradictions:
                    for c in synth_result.contradictions[:3]:
                        logger.info(
                            "Profile contradiction: %s — %s vs %s",
                            c.attribute,
                            c.old_value,
                            c.new_value,
                        )
            except Exception:
                logger.debug("Profile synthesis skipped (non-critical)", exc_info=True)

        # ── Deduplicate and trim ────────────────────────────────────────
        return {
            "static": _deduplicate_lines(static_lines)[: cfg.max_static_lines],
            "dynamic": _deduplicate_lines(dynamic_lines)[: cfg.max_dynamic_lines],
        }
    finally:
        conn.close()


def profile_to_prompt(
    profile: dict[str, list[str]],
    *,
    user_label: str = "User",
) -> str:
    """
    Format a profile dict into an LLM system-prompt snippet.

    Args:
        profile: Output from ``synthesize_profile()``.
        user_label: Label to use for the user in the prompt.

    Returns:
        Formatted prompt block.
    """
    parts: list[str] = []

    if profile.get("static"):
        parts.append(f"ABOUT {user_label.upper()}:\n" + "\n".join(f"- {s}" for s in profile["static"]))

    if profile.get("dynamic"):
        parts.append("CURRENT CONTEXT:\n" + "\n".join(f"- {d}" for d in profile["dynamic"]))

    if not parts:
        return f"# {user_label} Profile\nNo profile data available yet."

    return "\n\n".join(parts)
