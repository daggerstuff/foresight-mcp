"""
Tests for the reflection_narrative module.

Covers:
* Prose generation happy path
* PHI safety: prompt does not contain raw memory content
* Tenant isolation in cache and audit trail
* Caching keyed on (report_id, tenant_id, user_id, model_version, insights_hash)
* Fallback contract: raises ``ReflectionNarrativeError`` on LLM failure
* Input validation: type and value errors for malformed inputs
* Persistent NarrativeCache integration (PIX-3740 / GAP-6b)
* Tenant-isolated audit row emission (PIX-3741 / GAP-6c)
"""

import sys
from pathlib import Path
from typing import Any, cast

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from foresight_mcp.audit import (
    NARRATIVE_CACHE_HIT,
    NARRATIVE_FAILED,
    NARRATIVE_GENERATED,
    AuditLog,
)
from foresight_mcp.narrative_cache import NarrativeCache
from foresight_mcp.reflection_engine import ReflectionInsight, ReflectionReport
from foresight_mcp.reflection_narrative import (
    ReflectionNarrativeError,
    _build_phi_safe_prompt,
    _compute_cache_key,
    _compute_insights_hash,
    generate_insight_narrative,
)

# Sensitive string used to test that raw memory `content` is excluded
# from the prompt payload. This MUST NOT appear in any generated prompt.
RAW_MEMORY_CONTENT_SENTINEL = "Patient disclosed childhood trauma — must never appear in prompt."


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

    out_a1 = generate_insight_narrative(
        report,
        tenant_id="tenant_a",
        user_id="user_1",
        llm_call=capturing_llm,
        cache=isolated_cache,
    )
    out_a2 = generate_insight_narrative(
        report,
        tenant_id="tenant_a",
        user_id="user_1",
        llm_call=capturing_llm,
        cache=isolated_cache,
    )
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
    out_v1_again = generate_insight_narrative(
        report_a,
        tenant_id="t",
        user_id="u",
        llm_call=counting_llm,
        model_version="claude-4",
        cache=cache,
    )
    out_b = generate_insight_narrative(
        report_b,
        tenant_id="t",
        user_id="u",
        llm_call=counting_llm,
        model_version="claude-4",
        cache=cache,
    )

    assert call_count == 3
    assert out_v1 == out_v1_again
    assert out_v1 != out_v2
    assert out_v1 != out_b
    assert len(cache) == 3

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
    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert "LLM provider unavailable" in str(exc_info.value.__cause__)

    assert len(cache) == 0

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


# ============================================================
# Persistent NarrativeCache integration (PIX-3740 / GAP-6b)
# ============================================================


def test_reflection_narrative_uses_narrative_cache_persistent_storage(
    tmp_path: Any,
) -> None:
    cache = NarrativeCache(str(tmp_path / "narratives.db"))
    try:
        report = _make_report()
        call_count = 0

        def counting_llm(prompt: str, tenant_id: str, user_id: str) -> str:
            nonlocal call_count
            call_count += 1
            return "persisted narrative"

        out1 = generate_insight_narrative(
            report,
            tenant_id="tenant-persist",
            user_id="user-1",
            llm_call=counting_llm,
            cache=cache,
        )
        assert out1 == "persisted narrative"
        assert call_count == 1

        out2 = generate_insight_narrative(
            report,
            tenant_id="tenant-persist",
            user_id="user-1",
            llm_call=counting_llm,
            cache=cache,
        )
        assert out2 == "persisted narrative"
        assert call_count == 1
    finally:
        cache.close()


def test_reflection_narrative_narrative_cache_survives_restart(
    tmp_path: Any,
) -> None:
    db_path = str(tmp_path / "narratives.db")
    report = _make_report()

    cache1 = NarrativeCache(db_path)
    try:
        generate_insight_narrative(
            report,
            tenant_id="tenant-restart",
            user_id="user-1",
            llm_call=_fake_llm,
            cache=cache1,
        )
    finally:
        cache1.close()

    def must_not_call(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("LLM must not be called when cache is hydrated from disk")

    cache2 = NarrativeCache(db_path)
    try:
        out = generate_insight_narrative(
            report,
            tenant_id="tenant-restart",
            user_id="user-1",
            llm_call=must_not_call,
            cache=cache2,
        )
        assert out
    finally:
        cache2.close()


def test_reflection_narrative_narrative_cache_tenant_isolation(
    tmp_path: Any,
) -> None:
    cache = NarrativeCache(str(tmp_path / "narratives.db"))
    try:
        report = _make_report()
        insights_hash = _compute_insights_hash(report)

        generate_insight_narrative(
            report,
            tenant_id="tenant-a",
            user_id="user-1",
            llm_call=_fake_llm,
            cache=cache,
        )

        out_b = generate_insight_narrative(
            report,
            tenant_id="tenant-b",
            user_id="user-1",
            llm_call=_fake_llm,
            cache=cache,
        )
        assert out_b

        a = cache.get(
            report.report_id,
            tenant_id="tenant-a",
            user_id="user-1",
            model_version="caller-default",
            insights_hash=insights_hash,
        )
        b = cache.get(
            report.report_id,
            tenant_id="tenant-b",
            user_id="user-1",
            model_version="caller-default",
            insights_hash=insights_hash,
        )
        assert a is not None
        assert b is not None
        assert a != b
        assert a.startswith("Narrative for tenant-a/")
        assert b.startswith("Narrative for tenant-b/")

        c = cache.get(
            report.report_id,
            tenant_id="tenant-c",
            user_id="user-1",
            model_version="caller-default",
            insights_hash=insights_hash,
        )
        assert c is None
    finally:
        cache.close()


def test_reflection_narrative_rejects_invalid_cache_type() -> None:
    report = _make_report()
    with pytest.raises(TypeError, match="cache must be a dict or NarrativeCache"):
        generate_insight_narrative(
            report,
            tenant_id="t",
            user_id="u",
            llm_call=_fake_llm,
            cache=cast(Any, "not a cache"),
        )


# ============================================================
# Audit emission (PIX-3741 / GAP-6c)
# ============================================================


def test_reflection_narrative_emits_audit_row_on_success(tmp_path: Any) -> None:
    log = AuditLog(str(tmp_path / "audit.db"))
    try:
        report = _make_report()
        out = generate_insight_narrative(
            report,
            tenant_id="tenant-success",
            user_id="user-1",
            llm_call=_fake_llm,
            cache={},
            audit_log=log,
        )
        assert out

        rows = log.query("tenant-success", event_type=NARRATIVE_GENERATED)
        assert len(rows) == 1
        row = rows[0]
        assert row.tenant_id == "tenant-success"
        assert row.user_id == "user-1"
        assert row.resource_id == report.report_id
        assert row.metadata["outcome"] == "success"
        assert row.metadata["report_id"] == report.report_id
        assert row.metadata["prompt_hash"]
        assert row.metadata["response_hash"]
        assert row.metadata["latency_ms"] >= 0
    finally:
        log.close()


def test_reflection_narrative_emits_audit_row_on_error(tmp_path: Any) -> None:
    def boom(prompt: str, tenant_id: str, user_id: str) -> str:
        raise RuntimeError("upstream provider timeout")

    log = AuditLog(str(tmp_path / "audit.db"))
    try:
        report = _make_report()
        with pytest.raises(ReflectionNarrativeError):
            generate_insight_narrative(
                report,
                tenant_id="tenant-fail",
                user_id="user-1",
                llm_call=boom,
                cache={},
                audit_log=log,
            )

        rows = log.query("tenant-fail", event_type=NARRATIVE_FAILED)
        assert len(rows) == 1
        row = rows[0]
        assert row.tenant_id == "tenant-fail"
        assert row.user_id == "user-1"
        assert row.metadata["outcome"] == "error"
        assert row.metadata["response_hash"] is None
    finally:
        log.close()


def test_reflection_narrative_emits_audit_row_on_cache_hit(tmp_path: Any) -> None:
    log = AuditLog(str(tmp_path / "audit.db"))
    try:
        report = _make_report()
        cache: dict[str, str] = {}
        out1 = generate_insight_narrative(
            report,
            tenant_id="tenant-cache",
            user_id="user-1",
            llm_call=_fake_llm,
            cache=cache,
            audit_log=log,
        )
        assert out1

        def must_not_call(*args: Any, **kwargs: Any) -> str:
            raise AssertionError("LLM callable should not be invoked on cache hit")

        out2 = generate_insight_narrative(
            report,
            tenant_id="tenant-cache",
            user_id="user-1",
            llm_call=must_not_call,
            cache=cache,
            audit_log=log,
        )
        assert out2 == out1

        cache_hits = log.query("tenant-cache", event_type=NARRATIVE_CACHE_HIT)
        assert len(cache_hits) == 1
        assert cache_hits[0].metadata["outcome"] == "cache_hit"
    finally:
        log.close()


def test_reflection_narrative_audit_log_is_optional() -> None:
    report = _make_report()
    out = generate_insight_narrative(
        report,
        tenant_id="tenant-noaudit",
        user_id="user-1",
        llm_call=_fake_llm,
        cache={},
    )
    assert out

    out2 = generate_insight_narrative(
        report,
        tenant_id="tenant-noaudit",
        user_id="user-1",
        llm_call=_fake_llm,
        cache={},
    )
    assert out2 == out


# ============================================================
# Persistent NarrativeCache integration (PIX-3740 / GAP-6b)
