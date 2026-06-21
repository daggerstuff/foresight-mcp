"""Payload-Budgeted Context Injection Lanes.

Implements PIX-3949: bounded payload output for inject_context and
get_relevant_memories with lane-based character budget allocation.

Lanes (in priority order):
1. static   – user profile / preferences (highest priority, rarely changes)
2. dynamic  – project context / pending items (session-specific)
3. memories – top-ranked retrieval results (most content, first to truncate)
4. blocks   – context block signals (supplementary)
5. safety   – clinical gating / safety notes (always preserved if present)

Budget allocation strategy:
- Each lane receives a percentage of the total character budget.
- Within a lane, items are ordered by score/importance (highest first).
- When a lane exhausts its budget:
  1. Remaining items get truncated to summaries (first sentence only).
  2. If summaries still don't fit, items are omitted (stub = ID + score only).
  3. Truncation happens at sentence boundaries — never mid-word.
- Default budget is None (unbounded = legacy behavior preserved).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger("foresight_budget")


# =============================================================================
# Lane definitions
# =============================================================================


class Lane(str, Enum):
    """Injection lanes in priority order."""

    STATIC = "static"
    DYNAMIC = "dynamic"
    MEMORIES = "memories"
    BLOCKS = "blocks"
    SAFETY = "safety"


# Priority order for lane allocation
LANE_PRIORITY: list[Lane] = [
    Lane.STATIC,
    Lane.DYNAMIC,
    Lane.MEMORIES,
    Lane.BLOCKS,
    Lane.SAFETY,
]


class TruncationLevel(str, Enum):
    """How much of an item's content is preserved."""

    FULL = "full"  # Complete content
    SUMMARY = "summary"  # First sentence only
    STUB = "stub"  # ID + score only, no content


# Default percentage allocation per lane (must sum to 1.0)
DEFAULT_LANE_WEIGHTS: dict[Lane, float] = {
    Lane.STATIC: 0.10,
    Lane.DYNAMIC: 0.15,
    Lane.MEMORIES: 0.50,
    Lane.BLOCKS: 0.15,
    Lane.SAFETY: 0.10,
}

# Minimum characters before a lane gets promoted from the shared pool
MIN_LANE_CHARS: int = 40

# Sentence boundary pattern: period, exclamation, or question mark followed
# by whitespace or end-of-string, respecting common abbreviations.
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")


# =============================================================================
# Budget configuration
# =============================================================================


@dataclass
class InjectionBudget:
    """Character budget configuration for injection payload formatting.

    When max_chars is None, budgeting is disabled (legacy unbounded behavior).
    Note: each lane enforces a MIN_LANE_CHARS (40) floor, so even max_chars=0
    will produce at least ~200 characters of output (5 lanes × 40 + headers).
    For zero context, do not call the budgeted path at all.

    Attributes:
        max_chars: Total character budget for the formatted payload.
            None = no budget (backward compatible default).
            0 = minimum viable output (not zero-length; each lane gets
            at least MIN_LANE_CHARS characters to preserve clinical safety).
        lane_weights: Percentage allocation per lane. Must sum to 1.0.
            Defaults to DEFAULT_LANE_WEIGHTS if not provided.
        summary_max_chars: Maximum characters for a summary (first sentence).
            If the first sentence exceeds this, it is hard-truncated at
            a word boundary within this limit.
        stub_format: Format string for stub items. Available fields:
            {id}, {score}.
    """

    max_chars: int | None = None
    lane_weights: dict[Lane, float] = field(default_factory=lambda: dict(DEFAULT_LANE_WEIGHTS))
    summary_max_chars: int = 200
    stub_format: str = "[{id}] (score: {score})"

    def __post_init__(self) -> None:
        if self.max_chars is not None and self.max_chars < 0:
            raise ValueError(f"max_chars must be >= 0 or None, got {self.max_chars}")
        total_weight = sum(self.lane_weights.values())
        if abs(total_weight - 1.0) > 0.01:
            raise ValueError(f"lane_weights must sum to 1.0, got {total_weight:.4f}")

    @property
    def is_bounded(self) -> bool:
        return self.max_chars is not None

    def lane_budget(self, lane: Lane) -> int:
        """Return the character budget for a specific lane.

        Lanes with weights < 0 get MIN_LANE_CHARS from the shared pool,
        then the remaining budget is distributed proportionally.
        """
        if not self.is_bounded:
            return -1  # sentinel for unbounded

        weight = self.lane_weights.get(lane, 0.0)
        return max(MIN_LANE_CHARS, int(self.max_chars * weight))  # type: ignore[arg-type]


# =============================================================================
# Truncation utilities
# =============================================================================


def _first_sentence(text: str, max_chars: int = 200) -> str:
    """Extract the first sentence from text, respecting sentence boundaries.

    If the first sentence exceeds max_chars, it is truncated at a word
    boundary within the limit. Never cuts mid-word.
    """
    if not text:
        return ""

    # Split on sentence boundaries
    parts = _SENTENCE_END_RE.split(text, maxsplit=1)
    first = parts[0].strip()

    if len(first) <= max_chars:
        return first

    # Hard-truncate at word boundary
    truncated = first[:max_chars]
    # Walk back to the last space to avoid cutting mid-word
    last_space = truncated.rfind(" ")
    if last_space > 0:
        truncated = truncated[:last_space]
    return truncated.rstrip(".,;:!?") + "..."


def _truncate_to_chars(text: str, max_chars: int) -> str:
    """Truncate text to at most max_chars, breaking at word boundaries."""
    if not text or len(text) <= max_chars:
        return text

    truncated = text[:max_chars]
    last_space = truncated.rfind(" ")
    if last_space > 0:
        truncated = truncated[:last_space]
    return truncated.rstrip(".,;:!?") + "..."


# =============================================================================
# Lane item types (what flows through each lane)
# =============================================================================


@dataclass
class LaneItem:
    """A single item to be formatted within a lane.

    Attributes:
        id: Unique identifier (memory_id, block label, etc.)
        content: Full text content
        score: Relevance or importance score (0.0–1.0)
        lane: Which lane this item belongs to
        metadata: Optional dict for lane-specific fields
    """

    id: str
    content: str
    score: float = 0.0
    lane: Lane = Lane.MEMORIES
    metadata: dict[str, Any] = field(default_factory=dict)


# =============================================================================
# Budget allocation result
# =============================================================================


@dataclass
class LaneAllocation:
    """Result of budget allocation for a single lane.

    Attributes:
        lane: The lane this allocation is for
        budget_chars: Character budget assigned to this lane
        full_items: Items rendered at TruncationLevel.FULL
        summary_items: Items rendered at TruncationLevel.SUMMARY
        stub_items: Items rendered at TruncationLevel.STUB
        omitted_count: Number of items completely omitted
    """

    lane: Lane
    budget_chars: int
    full_items: list[tuple[LaneItem, str]] = field(default_factory=list)  # (item, rendered)
    summary_items: list[tuple[LaneItem, str]] = field(default_factory=list)
    stub_items: list[tuple[LaneItem, str]] = field(default_factory=list)
    omitted_count: int = 0

    @property
    def total_items(self) -> int:
        return len(self.full_items) + len(self.summary_items) + len(self.stub_items)

    @property
    def chars_used(self) -> int:
        rendered = self.full_items + self.summary_items + self.stub_items
        return sum(len(text) for _, text in rendered)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane.value,
            "budget_chars": self.budget_chars,
            "full_count": len(self.full_items),
            "summary_count": len(self.summary_items),
            "stub_count": len(self.stub_items),
            "omitted_count": self.omitted_count,
            "chars_used": self.chars_used,
        }


# =============================================================================
# Budget-aware formatter
# =============================================================================


@dataclass
class BudgetResult:
    """Complete result of budget-aware formatting.

    Attributes:
        formatted: The formatted string respecting the budget
        allocations: Per-lane allocation details
        total_chars: Total characters in the formatted output
        budget: The budget configuration used
    """

    formatted: str
    allocations: dict[Lane, LaneAllocation] = field(default_factory=dict)
    total_chars: int = 0
    budget: InjectionBudget = field(default_factory=InjectionBudget)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_chars": self.total_chars,
            "budget": {"max_chars": self.budget.max_chars, "is_bounded": self.budget.is_bounded},
            "lanes": {lane.value: alloc.to_dict() for lane, alloc in self.allocations.items()},
        }


def _render_item(item: LaneItem, level: TruncationLevel, budget: InjectionBudget) -> str:
    """Render a lane item at the given truncation level."""
    if level == TruncationLevel.FULL:
        return item.content
    elif level == TruncationLevel.SUMMARY:
        return _first_sentence(item.content, budget.summary_max_chars)
    else:  # STUB
        return budget.stub_format.format(id=item.id, score=f"{item.score:.2f}")


def allocate_lane(
    items: list[LaneItem],
    lane: Lane,
    budget: InjectionBudget,
) -> LaneAllocation:
    """Allocate budget across items within a single lane.

    Items are sorted by score (descending) so the highest-scoring items
    get full content first. Lower-scoring items are progressively
    truncated or omitted as the budget is exhausted.
    """
    if not budget.is_bounded:
        # Unbounded: everything at FULL
        rendered = [(item, _render_item(item, TruncationLevel.FULL, budget)) for item in items]
        return LaneAllocation(lane=lane, budget_chars=-1, full_items=rendered)

    lane_budget = budget.lane_budget(lane)
    allocation = LaneAllocation(lane=lane, budget_chars=lane_budget)

    # Sort by score descending — highest score gets full content first
    sorted_items = sorted(items, key=lambda i: i.score, reverse=True)

    remaining = lane_budget
    for item in sorted_items:
        if remaining <= 0:
            allocation.omitted_count += 1
            continue

        # Try FULL first
        full_text = _render_item(item, TruncationLevel.FULL, budget)
        full_len = len(full_text) + 1  # +1 for newline separator

        if full_len <= remaining:
            allocation.full_items.append((item, full_text))
            remaining -= full_len
            continue

        # Try SUMMARY
        summary_text = _render_item(item, TruncationLevel.SUMMARY, budget)
        summary_len = len(summary_text) + 1

        if summary_len <= remaining:
            allocation.summary_items.append((item, summary_text))
            remaining -= summary_len
            continue

        # Try STUB
        stub_text = _render_item(item, TruncationLevel.STUB, budget)
        stub_len = len(stub_text) + 1

        if stub_len <= remaining:
            allocation.stub_items.append((item, stub_text))
            remaining -= stub_len
            continue

        # Can't fit even a stub — omit
        allocation.omitted_count += 1

    return allocation


def format_budgeted_payload(
    lane_items: dict[Lane, list[LaneItem]],
    budget: InjectionBudget,
    header: str = "[Relevant Context]",
) -> BudgetResult:
    """Format all lanes into a single budgeted payload string.

    Lanes are rendered in LANE_PRIORITY order. Within each lane,
    items appear as full → summary → stub. The total output respects
    budget.max_chars when bounded.

    Args:
        lane_items: Map of lane to its list of LaneItems
        budget: Budget configuration
        header: Header line for the formatted output

    Returns:
        BudgetResult with formatted string and allocation details
    """
    allocations: dict[Lane, LaneAllocation] = {}
    header_line = f"{header} - {sum(len(v) for v in lane_items.values())} items"
    sections: list[str] = [header_line]

    for lane in LANE_PRIORITY:
        items = lane_items.get(lane, [])
        if not items:
            continue

        alloc = allocate_lane(items, lane, budget)
        allocations[lane] = alloc

        lane_lines: list[str] = []
        for item, text in alloc.full_items + alloc.summary_items + alloc.stub_items:
            lane_lines.append(f"- [{item.id}] {text}")

        if lane_lines:
            sections.append("")
            sections.append(f"[{lane.value}]")
            sections.extend(lane_lines)

    formatted = "\n".join(sections)
    return BudgetResult(
        formatted=formatted,
        allocations=allocations,
        total_chars=len(formatted),
        budget=budget,
    )
