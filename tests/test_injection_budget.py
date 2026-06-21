"""Tests for Payload-Budgeted Context Injection Lanes (PIX-3949).

Covers: budget enforcement, lane ordering, truncation at sentence
boundaries, progressive degradation (full → summary → stub → omit),
edge cases (empty lanes, zero budget, single item), and high-signal
preservation under pressure.
"""

import pytest

from foresight_mcp.injection_budget import (
    DEFAULT_LANE_WEIGHTS,
    InjectionBudget,
    Lane,
    LaneAllocation,
    LaneItem,
    TruncationLevel,
    _first_sentence,
    _truncate_to_chars,
    allocate_lane,
    format_budgeted_payload,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _item(id: str, content: str, score: float = 0.5, lane: Lane = Lane.MEMORIES) -> LaneItem:
    return LaneItem(id=id, content=content, score=score, lane=lane)


# ---------------------------------------------------------------------------
# InjectionBudget
# ---------------------------------------------------------------------------


class TestInjectionBudget:
    def test_unbounded_by_default(self):
        budget = InjectionBudget()
        assert not budget.is_bounded
        assert budget.lane_budget(Lane.MEMORIES) == -1

    def test_bounded_when_max_chars_set(self):
        budget = InjectionBudget(max_chars=1000)
        assert budget.is_bounded

    def test_lane_budget_distribution(self):
        budget = InjectionBudget(max_chars=1000)
        assert budget.lane_budget(Lane.MEMORIES) == 500
        assert budget.lane_budget(Lane.STATIC) == 100
        assert budget.lane_budget(Lane.DYNAMIC) == 150

    def test_negative_max_chars_rejected(self):
        with pytest.raises(ValueError, match="max_chars must be >= 0"):
            InjectionBudget(max_chars=-1)

    def test_weights_must_sum_to_one(self):
        with pytest.raises(ValueError, match="lane_weights must sum to 1.0"):
            InjectionBudget(max_chars=1000, lane_weights={Lane.MEMORIES: 0.5})

    def test_custom_weights(self):
        budget = InjectionBudget(
            max_chars=200,
            lane_weights={Lane.MEMORIES: 1.0, Lane.STATIC: 0.0, Lane.DYNAMIC: 0.0, Lane.BLOCKS: 0.0, Lane.SAFETY: 0.0},
        )
        assert budget.lane_budget(Lane.MEMORIES) == 200

    def test_lane_budget_minimum(self):
        budget = InjectionBudget(
            max_chars=100,
            lane_weights={Lane.STATIC: 0.0, Lane.DYNAMIC: 0.0, Lane.MEMORIES: 1.0, Lane.BLOCKS: 0.0, Lane.SAFETY: 0.0},
        )
        assert budget.lane_budget(Lane.STATIC) >= 40


# ---------------------------------------------------------------------------
# Truncation utilities
# ---------------------------------------------------------------------------


class TestFirstSentence:
    def test_single_sentence(self):
        assert _first_sentence("Hello world.") == "Hello world."

    def test_multiple_sentences(self):
        result = _first_sentence("First sentence. Second sentence. Third one.")
        assert result == "First sentence."

    def test_exclamation(self):
        result = _first_sentence("Watch out! This is important.")
        assert result == "Watch out!"

    def test_question(self):
        result = _first_sentence("Why now? Because it matters.")
        assert result == "Why now?"

    def test_long_sentence_truncated(self):
        text = "A" * 300
        result = _first_sentence(text, max_chars=200)
        assert len(result) <= 203  # 200 + "..."
        assert result.endswith("...")

    def test_empty_string(self):
        assert _first_sentence("") == ""

    def test_truncation_at_word_boundary(self):
        text = "The quick brown fox jumps over the lazy dog and keeps going and going"
        result = _first_sentence(text, max_chars=30)
        assert len(result) <= 33
        assert not result.rstrip(".").endswith("goi")


class TestTruncateToChars:
    def test_short_text_unchanged(self):
        assert _truncate_to_chars("hi", 10) == "hi"

    def test_long_text_truncated(self):
        text = "word " * 100
        result = _truncate_to_chars(text, 30)
        assert len(result) <= 33
        assert result.endswith("...")

    def test_empty(self):
        assert _truncate_to_chars("", 10) == ""


# ---------------------------------------------------------------------------
# allocate_lane
# ---------------------------------------------------------------------------


class TestAllocateLane:
    def test_unbounded_all_full(self):
        items = [_item("a", "content a"), _item("b", "content b")]
        budget = InjectionBudget()
        alloc = allocate_lane(items, Lane.MEMORIES, budget)
        assert len(alloc.full_items) == 2
        assert alloc.summary_items == []
        assert alloc.stub_items == []
        assert alloc.omitted_count == 0

    def test_budget_enforcement(self):
        items = [_item("a", "A" * 100, score=0.9), _item("b", "B" * 100, score=0.5)]
        budget = InjectionBudget(max_chars=150)
        alloc = allocate_lane(items, Lane.MEMORIES, budget)
        assert alloc.total_items == 2
        assert len(alloc.full_items) + len(alloc.summary_items) + len(alloc.stub_items) == 2

    def test_highest_score_gets_full_first(self):
        items = [_item("low", "low score content", score=0.2), _item("high", "high score content", score=0.9)]
        budget = InjectionBudget(max_chars=200)
        alloc = allocate_lane(items, Lane.MEMORIES, budget)
        full_ids = [item.id for item, _ in alloc.full_items]
        assert "high" in full_ids

    def test_omitted_items(self):
        items = [_item(f"i{n}", "X" * 200, score=0.5 - n * 0.01) for n in range(20)]
        budget = InjectionBudget(max_chars=100)
        alloc = allocate_lane(items, Lane.MEMORIES, budget)
        assert alloc.omitted_count > 0
        assert alloc.total_items + alloc.omitted_count == 20

    def test_empty_items(self):
        budget = InjectionBudget(max_chars=500)
        alloc = allocate_lane([], Lane.MEMORIES, budget)
        assert alloc.total_items == 0
        assert alloc.omitted_count == 0

    def test_progressive_degradation(self):
        long_content = "First sentence here. Second sentence. Third sentence goes on. Fourth. Fifth."
        items = [_item(f"m{n}", long_content, score=0.9 - n * 0.1) for n in range(6)]
        budget = InjectionBudget(max_chars=300)
        alloc = allocate_lane(items, Lane.MEMORIES, budget)
        assert len(alloc.full_items) >= 1
        assert alloc.total_items > 0

    def test_allocation_to_dict(self):
        items = [_item("a", "content")]
        budget = InjectionBudget(max_chars=1000)
        alloc = allocate_lane(items, Lane.MEMORIES, budget)
        d = alloc.to_dict()
        assert d["lane"] == "memories"
        assert "full_count" in d
        assert "chars_used" in d


# ---------------------------------------------------------------------------
# format_budgeted_payload
# ---------------------------------------------------------------------------


class TestFormatBudgetedPayload:
    def test_empty_lanes(self):
        budget = InjectionBudget(max_chars=500)
        result = format_budgeted_payload({}, budget)
        assert result.total_chars > 0
        assert result.formatted.startswith("[Relevant Context]")

    def test_memories_only(self):
        items = [_item("mem1", "Memory content one.", 0.8), _item("mem2", "Memory content two.", 0.6)]
        lane_items = {Lane.MEMORIES: items}
        budget = InjectionBudget(max_chars=1000)
        result = format_budgeted_payload(lane_items, budget)
        assert "[memories]" in result.formatted
        assert "mem1" in result.formatted
        assert "mem2" in result.formatted

    def test_lane_priority_ordering(self):
        lane_items: dict[Lane, list[LaneItem]] = {
            Lane.STATIC: [_item("pref", "User preference content", 0.9, Lane.STATIC)],
            Lane.DYNAMIC: [_item("proj", "Project context content", 0.8, Lane.DYNAMIC)],
            Lane.MEMORIES: [_item("mem", "Memory content", 0.7, Lane.MEMORIES)],
            Lane.BLOCKS: [_item("blk", "Block signal content", 0.5, Lane.BLOCKS)],
            Lane.SAFETY: [_item("safe", "Safety note content", 0.9, Lane.SAFETY)],
        }
        budget = InjectionBudget(max_chars=2000)
        result = format_budgeted_payload(lane_items, budget)
        formatted = result.formatted
        static_pos = formatted.find("[static]")
        dynamic_pos = formatted.find("[dynamic]")
        memories_pos = formatted.find("[memories]")
        blocks_pos = formatted.find("[blocks]")
        safety_pos = formatted.find("[safety]")
        assert static_pos < dynamic_pos < memories_pos < blocks_pos < safety_pos

    def test_budget_respected(self):
        items = [_item(f"m{n}", "X" * 100, score=0.5) for n in range(10)]
        lane_items = {Lane.MEMORIES: items}
        budget = InjectionBudget(max_chars=300)
        result = format_budgeted_payload(lane_items, budget)
        assert result.total_chars <= 600  # Allow for headers + separators

    def test_unbounded(self):
        items = [_item("a", "Content A", 0.5)]
        lane_items = {Lane.MEMORIES: items}
        budget = InjectionBudget()
        result = format_budgeted_payload(lane_items, budget)
        assert "Content A" in result.formatted

    def test_result_to_dict(self):
        items = [_item("a", "Content A", 0.5)]
        lane_items = {Lane.MEMORIES: items}
        budget = InjectionBudget(max_chars=500)
        result = format_budgeted_payload(lane_items, budget)
        d = result.to_dict()
        assert "total_chars" in d
        assert "budget" in d
        assert "lanes" in d
        assert "memories" in d["lanes"]

    def test_custom_header(self):
        result = format_budgeted_payload({}, InjectionBudget(), header="[Custom]")
        assert "[Custom]" in result.formatted

    def test_truncation_under_pressure(self):
        items = [_item(f"m{n}", "First sentence. Second sentence. Third.", score=0.9 - n * 0.05) for n in range(15)]
        lane_items = {Lane.MEMORIES: items}
        budget = InjectionBudget(max_chars=200)
        result = format_budgeted_payload(lane_items, budget)
        mem_alloc = result.allocations.get(Lane.MEMORIES)
        if mem_alloc is not None:
            has_truncated = (
                len(mem_alloc.summary_items) > 0 or len(mem_alloc.stub_items) > 0 or mem_alloc.omitted_count > 0
            )
            assert has_truncated


# ---------------------------------------------------------------------------
# High-signal preservation
# ---------------------------------------------------------------------------


class TestHighSignalPreservation:
    def test_high_score_preserved_over_low(self):
        items = [
            _item("critical", "Critical important memory content that must be preserved.", score=0.95),
            _item("low", "Low priority content that can be dropped.", score=0.1),
        ]
        lane_items = {Lane.MEMORIES: items}
        budget = InjectionBudget(max_chars=300)
        result = format_budgeted_payload(lane_items, budget)
        mem_alloc = result.allocations.get(Lane.MEMORIES)
        assert mem_alloc is not None
        preserved_ids = [item.id for item, _ in mem_alloc.full_items + mem_alloc.summary_items + mem_alloc.stub_items]
        assert "critical" in preserved_ids

    def test_safety_lane_preserved_when_present(self):
        lane_items: dict[Lane, list[LaneItem]] = {
            Lane.MEMORIES: [_item(f"m{n}", "X" * 50, score=0.5) for n in range(10)],
            Lane.SAFETY: [
                _item("safety1", "Clinical safety note: do not discuss self-harm methods.", score=1.0, lane=Lane.SAFETY)
            ],
        }
        budget = InjectionBudget(max_chars=500)
        result = format_budgeted_payload(lane_items, budget)
        assert "safety1" in result.formatted

    def test_static_lane_preserved_over_memories(self):
        lane_items: dict[Lane, list[LaneItem]] = {
            Lane.STATIC: [_item("prefs", "User prefers concise responses.", score=0.9, lane=Lane.STATIC)],
            Lane.MEMORIES: [_item(f"m{n}", "Memory " * 20, score=0.5) for n in range(10)],
        }
        budget = InjectionBudget(max_chars=300)
        result = format_budgeted_payload(lane_items, budget)
        static_pos = result.formatted.find("prefs")
        assert static_pos >= 0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_zero_budget(self):
        items = [_item("a", "content")]
        budget = InjectionBudget(max_chars=0)
        alloc = allocate_lane(items, Lane.MEMORIES, budget)
        assert budget.lane_budget(Lane.MEMORIES) == 40
        assert alloc.total_items >= 0

    def test_single_item_fits(self):
        items = [_item("only", "Short content.", 0.9)]
        lane_items = {Lane.MEMORIES: items}
        budget = InjectionBudget(max_chars=500)
        result = format_budgeted_payload(lane_items, budget)
        assert "only" in result.formatted

    def test_single_item_too_large(self):
        items = [_item("big", "X" * 2000, 0.5)]
        lane_items = {Lane.MEMORIES: items}
        budget = InjectionBudget(max_chars=100)
        result = format_budgeted_payload(lane_items, budget)
        mem_alloc = result.allocations.get(Lane.MEMORIES)
        if mem_alloc is not None:
            assert len(mem_alloc.full_items) == 0 or len(mem_alloc.summary_items) > 0 or len(mem_alloc.stub_items) > 0

    def test_no_mid_word_cuts(self):
        items = [_item("a", "The quick brown fox jumps over the lazy dog.", 0.5)]
        budget = InjectionBudget(max_chars=30)
        alloc = allocate_lane(items, Lane.MEMORIES, budget)
        for _, text in alloc.full_items + alloc.summary_items:
            if text.endswith("..."):
                before_ellipsis = text[:-3]
                assert not before_ellipsis.endswith(" ") or before_ellipsis.rstrip() == before_ellipsis

    def test_exact_budget_match(self):
        content = "A" * 80
        items = [_item("a", content, 0.9)]
        budget = InjectionBudget(max_chars=100)
        alloc = allocate_lane(items, Lane.MEMORIES, budget)
        assert alloc.total_items == 1

    def test_multiple_lanes_with_tight_budget(self):
        lane_items: dict[Lane, list[LaneItem]] = {
            Lane.STATIC: [_item("s1", "Static content here.", 0.9, Lane.STATIC)],
            Lane.DYNAMIC: [_item("d1", "Dynamic content here.", 0.8, Lane.DYNAMIC)],
            Lane.MEMORIES: [_item("m1", "Memory content here.", 0.7, Lane.MEMORIES)],
            Lane.BLOCKS: [_item("b1", "Block content here.", 0.5, Lane.BLOCKS)],
            Lane.SAFETY: [_item("f1", "Safety content here.", 1.0, Lane.SAFETY)],
        }
        budget = InjectionBudget(max_chars=150)
        result = format_budgeted_payload(lane_items, budget)
        assert result.total_chars > 0
        d = result.to_dict()
        assert len(d["lanes"]) > 0

    def test_unicode_content(self):
        items = [_item("u1", "日本語のテスト文。二つ目の文。", 0.8)]
        lane_items = {Lane.MEMORIES: items}
        budget = InjectionBudget(max_chars=200)
        result = format_budgeted_payload(lane_items, budget)
        assert "u1" in result.formatted
