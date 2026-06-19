"""
Phrase trigger hook for automatic memory capture.

Detects configurable trigger phrases in text (e.g., "remember this:", "note:")
and extracts the associated content as structured memory items. Intended for
use by agents that want to capture memories inline during conversation without
making a separate explicit tool call.

Inspired by Factory's phrase-trigger hook pattern where phrases in user prompts
trigger automatic memory capture, ported to Foresight's typed memory system.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Default trigger configuration
# ---------------------------------------------------------------------------

# Each trigger phrase maps to memory metadata applied when storing.
# Keys are the trigger strings (matched case-insensitively in text).
DEFAULT_TRIGGERS: dict[str, dict[str, Any]] = {
    "remember this:": {
        "category": "decision",
        "scope": "arc",
        "importance": 0.7,
        "retention": "long_term",
        "tags": ["auto-captured", "decision"],
    },
    "remember:": {
        "category": "decision",
        "scope": "arc",
        "importance": 0.7,
        "retention": "long_term",
        "tags": ["auto-captured", "decision"],
    },
    "note to self:": {
        "category": "preference",
        "scope": "trait",
        "importance": 0.6,
        "retention": "long_term",
        "tags": ["auto-captured", "preference"],
    },
    "note:": {
        "category": "fact",
        "scope": "fact",
        "importance": 0.5,
        "retention": "short_term",
        "tags": ["auto-captured", "note"],
    },
    "save this:": {
        "category": "fact",
        "scope": "fact",
        "importance": 0.5,
        "retention": "short_term",
        "tags": ["auto-captured"],
    },
    "important:": {
        "category": "preference",
        "scope": "trait",
        "importance": 0.8,
        "retention": "long_term",
        "tags": ["auto-captured", "important"],
    },
    "preference:": {
        "category": "preference",
        "scope": "trait",
        "importance": 0.6,
        "retention": "long_term",
        "tags": ["auto-captured", "preference"],
    },
    "decision:": {
        "category": "decision",
        "scope": "arc",
        "importance": 0.7,
        "retention": "long_term",
        "tags": ["auto-captured", "decision"],
    },
    "lesson:": {
        "category": "learning",
        "scope": "arc",
        "importance": 0.7,
        "retention": "long_term",
        "tags": ["auto-captured", "learning"],
    },
    "fact:": {
        "category": "fact",
        "scope": "fact",
        "importance": 0.5,
        "retention": "short_term",
        "tags": ["auto-captured", "fact"],
    },
    "context:": {
        "category": "context",
        "scope": "arc",
        "importance": 0.6,
        "retention": "short_term",
        "tags": ["auto-captured", "context"],
    },
}

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class PhraseTriggerMatch:
    """A single detected phrase trigger in input text.

    Attributes:
        trigger: The trigger phrase that matched (e.g. ``"remember this:"``).
        content: Extracted content following the trigger (whitespace-stripped).
        metadata: Memory metadata copied from the trigger configuration.
        position: Character offset in the original text where the trigger began.
    """

    trigger: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    position: int = 0


# ---------------------------------------------------------------------------
# Detection logic
# ---------------------------------------------------------------------------


def extract_triggered_memories(
    text: str,
    triggers: dict[str, dict[str, Any]] | None = None,
) -> list[PhraseTriggerMatch]:
    """Scan *text* for trigger phrases and extract matched content.

    Each trigger captures the content that follows it, up to either the next
    trigger phrase or the end of the text.  Longer trigger phrases are matched
    before shorter ones when they overlap (e.g. ``"remember this:"`` wins over
    ""remember:"``).

    Parameters
    ----------
    text:
        The text to scan — typically a user prompt or conversation turn.
    triggers:
        Custom trigger configuration mapping phrase → memory metadata.
        Falls back to :data:`DEFAULT_TRIGGERS` when ``None``.

    Returns
    -------
    list[PhraseTriggerMatch]
        Detected matches in order of appearance, each with extracted content
        and the associated memory metadata.
    """
    if triggers is None:
        triggers = DEFAULT_TRIGGERS

    if not text or not triggers:
        return []

    # Filter out empty trigger keys to avoid empty regex patterns
    triggers = {k: v for k, v in triggers.items() if k}
    if not triggers:
        return []

    # Build a fast lookup from lowercased trigger → (original_key, metadata).
    # Triggers are sorted longest-first so that multi-word triggers like
    # "note to self:" are tried before shorter ones like "note:".
    sorted_pairs = sorted(triggers.items(), key=lambda x: len(x[0]), reverse=True)
    lookup = {k.lower(): (k, dict(v)) for k, v in sorted_pairs}

    escaped = [re.escape(t) for t, _ in sorted_pairs]
    pattern = re.compile("|".join(escaped), re.IGNORECASE)

    matches: list[PhraseTriggerMatch] = []
    cursor = 0

    while cursor < len(text):
        m = pattern.search(text, cursor)
        if not m:
            break

        trigger_raw = m.group(0)
        raw_lower = trigger_raw.lower()

        entry = lookup.get(raw_lower)
        if entry is None:
            cursor = m.end()
            continue

        matched_key, metadata = entry
        content_start = m.end()

        # Content runs until the next trigger or end-of-text.
        next_m = pattern.search(text, content_start)
        if next_m:
            content = text[content_start : next_m.start()].strip()
            cursor = next_m.start()
        else:
            content = text[content_start:].strip()
            cursor = len(text)

        if content:
            matches.append(
                PhraseTriggerMatch(
                    trigger=matched_key,
                    content=content,
                    metadata=metadata,
                    position=m.start(),
                )
            )

    return matches
