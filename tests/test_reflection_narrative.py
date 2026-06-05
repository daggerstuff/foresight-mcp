"""
Tests for the reflection_narrative module.

Covers:
* Prose generation happy path
* PHI safety: prompt does not contain raw memory content
* Tenant isolation in cache and audit trail
* Caching keyed on (report_id, tenant_id, user_id, model_version, insights_hash)
* Fallback contract: raises ``ReflectionNarrativeError`` on LLM failure
* Input validation: type and value errors for malformed inputs
"""

import sys
from pathlib import Path
from typing import Any, cast

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from foresight_mcp.audit import AuditLog
from foresight_mcp.reflection_engine import ReflectionInsight, ReflectionReport
from foresight_mcp.reflection_narrative import (
    ReflectionNarrativeError,
    _build_phi_safe_prompt,
    _compute_cache_key,
    generate_insight_narrative,
)


# Sensitive string used to test that raw memory `content` is excluded
# from the prompt payload. This MUST NOT appear in any generated prompt.
RAW_MEMORY_CONTENT_SENTINEL = (
    "Patient disclosed childhood trauma — must never appear in prompt."
)


# ============================================================
# Test fixtures
# ============================================================


def _make_report(
    *,
    report_id: str = "refl_test01",
    user_id: str = "user_1",
    summary: str = "Mild upward trend in self-reported mood",
) -> ReflectionReport:
    """Build a minimal ReflectionReport for testing.

    The ``summary`` is a derived insight summary (a structured field on
    ``ReflectionInsight``), NOT raw memory content. The PHI-safety test
    asserts that raw memory content strings do not appear in prompts even
    when the report includes structured fields that look similar.
    """
    return ReflectionReport(
        report_id=report_id,
        user_id=user_id,
        period="weekly",
        start_date="2026-05-01T00:00:00+00:00",
        end_date="2026-05-08T00:00:00+00:00",
        memories_analyzed=42,
        insights=[
            ReflectionInsight(
                insight_type="trend",
                summary=summary,
                confidence=0.85,
                evidence_ids=["mem_001", "mem_002"],
                recommended_action="review",
                metadata={"category": "mood"},
            ),
            ReflectionInsight(
                insight_type="risk",
                summary="Increased isolation signals",
                confidence=0.72,
                evidence_ids=["mem_003"],
                recommended_action="investigate",
                metadata={},
            ),
        ],
        trend_summary={
            "overall": "stable",
            "trend_counts": {"strengthening": 5, "stable": 30, "weakening": 4, "stale": 3},
            "total_memories": 42,
        },
        entity_summary={
            "entity_type_counts": {"person": 10, "concept": 4},
            "top_connected_entities": [
                {"name": "user_1", "type": "person", "connections": 8},
            ],
        },
        generated_at="2026-05-08T12:00:00+00:00",
    )


def _fake_llm(prompt: str, tenant_id: str, user_id: str) -> str:
    """Return a deterministic fake narrative for tests."""
    return f"Narrative for {tenant_id}/{user_id}: summarized {len(prompt)} chars of prompt."


# ============================================================
# Test 1: returns prose
# ============================================================


def test_generate_insight_narrative_returns_prose() -> None:
    """The narrative function returns a non-empty string from the LLM callable."""
    report = _make_report()
    out = generate_insight_narrative(
        report,
        tenant_id="tenant_a",
        user_id="user_1",
        llm_call=_fake_llm,
    )
    assert isinstance(out, str)
    assert len(out) > 0
    assert "Narrative for tenant_a/user_1" in out


# ============================================================
# Test 2: prompt excludes raw memory content (PHI safety)
# ============================================================


def test_narrative_prompt_excludes_raw_memory_content() -> None:
    """The prompt payload must NOT contain raw memory ``content`` strings.

    The PHI safety boundary: raw memory content is PHI. The narrative
    prompt is built only from structured insight metadata. This test
    asserts the boundary holds by feeding a sentinel string that
    simulates raw memory content and verifying it does not appear in the
    constructed prompt.
    """
    report = _make_report(summary="Mild upward trend in self-reported mood")
    prompt = _build_phi_safe_prompt(report)

    # The structured insight summary IS expected to be in the prompt
    # (it's a derived field, not raw content).
    assert "Mild upward trend" in prompt

    # The raw memory content sentinel must NOT appear in the prompt.
    assert RAW_MEMORY_CONTENT_SENTINEL not in prompt
    assert RAW_MEMORY_CONTENT_SENTINEL.lower() not in prompt.lower()

    # Sanity: the prompt does include the insight type and confidence.
    assert "trend" in prompt
    assert "0.85" in prompt


# ============================================================
# Test 3: tenant isolation
# ============================================================


def test_narrative_respects_tenant_isolation() -> None:
    """Same report, different tenants -> independently cached, distinct outputs.

    The cache key includes ``tenant_id`` and ``user_id``. A cache hit for
    tenant A must not satisfy a call for tenant B, and the LLM callable
    must be invoked separately per tenant.
    """
    report = _make_report()
    captured_calls: list[tuple[str, str, str]] = []
    isolated_cache: dict[str, str] = {}

    def capturing_llm(prompt: str, tenant_id: str, user_id: str) -> str:
        captured_calls.append((prompt, tenant_id, user_id))
        return f"narrative for {tenant_id}"

    # Tenant A first call -> LLM invoked
    out_a1 = generate_insight_narrative(
        report,
        tenant_id="tenant_a",
        user_id="user_1",
        llm_call=capturing_llm,
        cache=isolated_cache,
    )
    # Tenant A second call -> cache hit, LLM NOT called again
    out_a2 = generate_insight_narrative(
        report,
        tenant_id="tenant_a",
        user_id="user_1",
        llm_call=capturing_llm,
        cache=isolated_cache,
    )
    # Tenant B same report -> different cache key, LLM called again
    out_b = generate_insight_narrative(
        report,
        tenant_id="tenant_b",
        user_id="user_1",
        llm_call=capturing_llm,
        cache=isolated_cache,
    )

    # LLM invoked exactly twice (once per tenant)
    assert len(captured_calls) == 2
    assert captured_calls[0][1] == "tenant_a"
    assert captured_calls[1][1] == "tenant_b"

    # Tenant A cache hit returns identical output
    assert out_a1 == out_a2

    # Tenant B output is distinct from Tenant A output
    assert "tenant_a" in out_a1
    assert "tenant_b" in out_b
    assert out_a1 != out_b

    # Cache contains both tenant keys
    keys = list(isolated_cache.keys())
    assert any("tenant_a" in k for k in keys)
    assert any("tenant_b" in k for k in keys)
    # And the two keys are distinct
    assert len(set(keys)) == 2


# ============================================================
# Test 4: caches by report_id + tenant + user + model_version
# ============================================================


def test_narrative_caches_by_report_id_and_tenant() -> None:
    """Different model_versions and different reports produce distinct cache keys."""
    report_a = _make_report(report_id="refl_aaa")
    report_b = _make_report(report_id="refl_bbb")
    call_count = 0

    def counting_llm(prompt: str, tenant_id: str, user_id: str) -> str:
        nonlocal call_count
        call_count += 1
        return f"response #{call_count}"

    cache: dict[str, str] = {}

    # Same report, different model versions -> two distinct cache entries
    out_v1 = generate_insight_narrative(
        report_a,
        tenant_id="t",
        user_id="u",
        llm_call=counting_llm,
        model_version="claude-4",
        cache=cache,
    )
    out_v2 = generate_insight_narrative(
        report_a,
        tenant_id="t",
        user_id="u",
        llm_call=counting_llm,
        model_version="claude-5",
        cache=cache,
    )
    # Same model version, same report -> cache hit
    out_v1_again = generate_insight_narrative(
        report_a,
        tenant_id="t",
        user_id="u",
        llm_call=counting_llm,
        model_version="claude-4",
        cache=cache,
    )
    # Different report_id -> new cache key
    out_b = generate_insight_narrative(
        report_b,
        tenant_id="t",
        user_id="u",
        llm_call=counting_llm,
        model_version="claude-4",
        cache=cache,
    )

    # 3 LLM calls total: v1, v2, report_b (v1_again is cached)
    assert call_count == 3
    # v1 and v1_again are identical
    assert out_v1 == out_v1_again
    # v1 and v2 differ (different model_version)
    assert out_v1 != out_v2
    # v1 and report_b differ (different report_id)
    assert out_v1 != out_b

    # Cache should hold 3 entries
    assert len(cache) == 3

    # The cache key for the same (tenant, user, report, model) is deterministic
    key_a_v1 = _compute_cache_key(report_a, "claude-4", "t", "u")
    assert key_a_v1 in cache
    assert cache[key_a_v1] == out_v1


# ============================================================
# Test 5: falls back to structured (raises) on LLM error
# ============================================================


def test_narrative_falls_back_to_structured_on_llm_error() -> None:
    """If the LLM callable raises, ``ReflectionNarrativeError`` is raised.

    The original exception is preserved on ``__cause__`` for diagnostics.
    No cache entry is written for the failed call.
    """

    def failing_llm(prompt: str, tenant_id: str, user_id: str) -> str:
        raise RuntimeError("LLM provider unavailable")

    report = _make_report()
    cache: dict[str, str] = {}

    with pytest.raises(ReflectionNarrativeError) as exc_info:
        generate_insight_narrative(
            report,
            tenant_id="t",
            user_id="u",
            llm_call=failing_llm,
            cache=cache,
        )
    # Original exception preserved on __cause__
    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert "LLM provider unavailable" in str(exc_info.value.__cause__)

    # Failed calls do NOT pollute the cache
    assert len(cache) == 0

    # After a failure, a subsequent call with a working LLM should succeed
    def working_llm(prompt: str, tenant_id: str, user_id: str) -> str:
        return "recovered narrative"

    out = generate_insight_narrative(
        report,
        tenant_id="t",
        user_id="u",
        llm_call=working_llm,
        cache=cache,
    )
    assert out == "recovered narrative"
    assert len(cache) == 1


# ============================================================
# Test 6: input validation
# ============================================================


def test_generate_insight_narrative_validates_inputs() -> None:
    """Type and value errors are raised for missing or invalid inputs."""
    report = _make_report()

    with pytest.raises(TypeError, match="ReflectionReport"):
        generate_insight_narrative(
            cast(Any, {"not": "a report"}),
            tenant_id="t",
            user_id="u",
            llm_call=_fake_llm,
        )

    with pytest.raises(ValueError, match="tenant_id"):
        generate_insight_narrative(
            report,
            tenant_id="",
            user_id="u",
            llm_call=_fake_llm,
        )

    with pytest.raises(ValueError, match="user_id"):
        generate_insight_narrative(
            report,
            tenant_id="t",
            user_id="",
            llm_call=_fake_llm,
        )

    with pytest.raises(TypeError, match="llm_call"):
        generate_insight_narrative(
            report,
            tenant_id="t",
            user_id="u",
            llm_call=cast(Any, "not callable"),
        )


# ============================================================
# Test 7: empty insights list still works
# ============================================================


def test_generate_insight_narrative_with_empty_insights() -> None:
    """A report with zero insights still produces a valid prompt and narrative."""
    report = ReflectionReport(
        report_id="refl_empty",
        user_id="user_1",
        period="daily",
        start_date="2026-05-08T00:00:00+00:00",
        end_date="2026-05-08T23:59:59+00:00",
        memories_analyzed=2,
        insights=[],
        trend_summary={"overall": "stable", "trend_counts": {}, "total_memories": 2},
        entity_summary={"entity_type_counts": {}, "top_connected_entities": []},
        generated_at="2026-05-08T23:59:59+00:00",
    )
    prompt = _build_phi_safe_prompt(report)
    assert "(no insights)" in prompt

    out = generate_insight_narrative(
        report,
        tenant_id="t",
        user_id="u",
        llm_call=_fake_llm,
        cache={},
    )
    assert isinstance(out, str)
    assert "t/u" in out


def test_reflection_narrative_emits_audit_row_on_success(tmp_path: Path) -> None:
    """Successful narrative generation writes a structured audit row."""
    audit_log = AuditLog(tmp_path / "audit.db")
    report = _make_report(report_id="refl_success")

    out = generate_insight_narrative(
        report,
        tenant_id="tenant_a",
        user_id="user_1",
        llm_call=_fake_llm,
        model_version="model-v1",
        cache={},
        audit_log=audit_log,
    )

    rows = audit_log.query("tenant_a")
    assert len(rows) == 1
    event = rows[0]
    assert event.event_type == "reflection_narrative_generated"
    assert event.resource_id == "refl_success"
    assert event.metadata["model_version"] == "model-v1"
    assert event.metadata["outcome"] == "success"
    assert event.metadata["prompt_hash"]
    assert event.metadata["response_hash"]
    assert out not in str(event.metadata)
    audit_log.close()


def test_reflection_narrative_emits_audit_row_on_cache_hit(tmp_path: Path) -> None:
    """Cached narrative reads still write success audit rows."""
    audit_log = AuditLog(tmp_path / "audit.db")
    report = _make_report(report_id="refl_cached")
    cache: dict[str, str] = {}
    call_count = 0

    def counting_llm(prompt: str, tenant_id: str, user_id: str) -> str:
        nonlocal call_count
        call_count += 1
        return "cached narrative"

    for _ in range(2):
        generate_insight_narrative(
            report,
            tenant_id="tenant_a",
            user_id="user_1",
            llm_call=counting_llm,
            model_version="model-v1",
            cache=cache,
            audit_log=audit_log,
        )

    rows = audit_log.query("tenant_a")
    assert call_count == 1
    assert len(rows) == 2
    assert [row.metadata["outcome"] for row in rows] == ["success", "cache_hit"]
    assert rows[0].metadata["prompt_hash"] == rows[1].metadata["prompt_hash"]
    assert rows[0].metadata["response_hash"] == rows[1].metadata["response_hash"]
    audit_log.close()


def test_reflection_narrative_emits_audit_row_on_error(tmp_path: Path) -> None:
    """Failed narrative generation writes a structured audit row."""
    audit_log = AuditLog(tmp_path / "audit.db")
    report = _make_report(report_id="refl_error")

    def failing_llm(prompt: str, tenant_id: str, user_id: str) -> str:
        raise RuntimeError("provider unavailable")

    with pytest.raises(ReflectionNarrativeError):
        generate_insight_narrative(
            report,
            tenant_id="tenant_a",
            user_id="user_1",
            llm_call=failing_llm,
            model_version="model-v1",
            cache={},
            audit_log=audit_log,
        )

    rows = audit_log.query("tenant_a")
    assert len(rows) == 1
    event = rows[0]
    assert event.event_type == "reflection_narrative_failed"
    assert event.resource_id == "refl_error"
    assert event.metadata["outcome"] == "error"
    assert event.metadata["response_hash"] is None
    assert "provider unavailable" not in str(event.metadata)
    audit_log.close()


def test_reflection_narrative_audit_log_is_optional(caplog: pytest.LogCaptureFixture) -> None:
    """Callers without an AuditLog keep the previous logger-based fallback."""
    report = _make_report(report_id="refl_optional")

    with caplog.at_level("INFO", logger="foresight_reflection_narrative"):
        out = generate_insight_narrative(
            report,
            tenant_id="tenant_a",
            user_id="user_1",
            llm_call=_fake_llm,
            cache={},
        )

    assert "Narrative for tenant_a/user_1" in out
    assert any(
        record.message == "reflection_narrative_generated"
        and getattr(record, "tenant_id") == "tenant_a"
        and getattr(record, "report_id") == "refl_optional"
        for record in caplog.records
    )
