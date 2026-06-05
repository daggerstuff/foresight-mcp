"""
LLM narrative generation for reflection engine reports.

Converts a structured :class:`ReflectionReport` into a human-readable prose
summary by calling a caller-supplied LLM callable. The ``foresight-mcp``
package does not bundle an LLM client; the caller is responsible for
providing a tenant-isolated LLM function. This keeps the memory subsystem
LLM-agnostic and avoids new top-level dependencies.

PHI safety
----------

Prompts are constructed *only* from structured insight metadata:

* ``insight_type`` (trend / contradiction / pattern / breakthrough / warning)
* ``summary`` (the structured insight summary — a derived artifact, not a
  raw memory excerpt)
* ``confidence``
* ``evidence_ids``
* ``recommended_action``
* ``metadata``
* ``trend_summary``, ``entity_summary``

Raw memory ``content`` is **never** included in the prompt. Callers must
ensure their LLM function enforces tenant isolation and audit logging
consistent with HIPAA requirements.

Audit
-----

Every successful and failed call writes a row to the
:class:`foresight_mcp.audit.AuditLog` table when one is supplied via
the ``audit_log`` parameter. The row contains ``tenant_id``,
``user_id``, ``report_id``, ``model_version``, ``prompt_hash``,
``response_hash``, ``latency_ms``, and ``outcome``. No raw prompt or
response text is stored. If no ``audit_log`` is supplied (e.g. unit
tests, ephemeral CLI runs), the module falls back to a structured
``logger.info(...)`` call for compatibility. The audit table is
append-only at the SQLite layer (see :mod:`foresight_mcp.audit`).

Caching
-------

The default cache is an in-process ``dict`` keyed by
``tenant_id:user_id:report_id:model_version:insights_hash``. The caller
may pass an external cache (e.g. a persistent dict) via the ``cache``
parameter for testability and durability. The companion ticket GAP-6b
(PIX-3740) adds a SQLite-backed persistent cache.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Callable

from .audit import (
    NARRATIVE_CACHE_HIT,
    NARRATIVE_FAILED,
    NARRATIVE_GENERATED,
    AuditEvent,
    AuditLog,
)
from .reflection_engine import ReflectionReport

logger = logging.getLogger("foresight_reflection_narrative")


class ReflectionNarrativeError(Exception):
    """Raised when narrative generation fails.

    The caller may catch this and fall back to the structured
    :class:`ReflectionReport`. The original exception is preserved on
    ``__cause__`` for diagnostics.
    """


# Type alias for the caller-supplied LLM callable.
#
# Args:
#     prompt (str): The PHI-safe prompt to send to the LLM.
#     tenant_id (str): Tenant ID for caller-side isolation enforcement.
#     user_id (str): User ID for caller-side isolation enforcement.
# Returns:
#     str: The LLM response text.
LLMCallable = Callable[[str, str, str], str]


# Prompt template — uses ONLY structured insight fields. Raw memory
# ``content`` is intentionally absent. The ``{placeholder}`` fields are
# filled in by :func:`_build_phi_safe_prompt`.
NARRATIVE_PROMPT_TEMPLATE = """You are summarizing a structured reflection report for a clinical AI memory platform.

The report contains {n_insights} insights about a user's memory state over the period {start_date} to {end_date} (period: {period}). Total memories analyzed: {memories_analyzed}.

Each insight is provided below as a structured record. Summarize the most important themes in 2-4 paragraphs of natural prose. Do not invent details. Do not include any raw memory content — only describe the patterns the structured insights reveal.

Insights:
{insights_block}

Trend summary (counts of memories by trend state): {trend_summary}
Entity summary (most-connected entities): {entity_summary}

Write a 2-4 paragraph narrative summary suitable for surfacing in a clinical coaching context. Keep the tone professional, observational, and non-judgmental.
"""


# ============================================================
# Cache key construction
# ============================================================


def _compute_cache_key(
    report: ReflectionReport,
    model_version: str,
    tenant_id: str,
    user_id: str,
) -> str:
    """Deterministic cache key includes tenant + user to enforce isolation.

    The insights hash is built from structured insight fields only. The
    full key shape is::

        tenant_id:user_id:report_id:model_version:insights_hash
    """
    insights_hash = hashlib.sha256(
        "|".join(
            f"{i.insight_type}:{i.summary}:{i.confidence:.3f}:{i.recommended_action}"
            for i in report.insights
        ).encode("utf-8"),
    ).hexdigest()[:16]
    return f"{tenant_id}:{user_id}:{report.report_id}:{model_version}:{insights_hash}"


# ============================================================
# PHI-safe prompt construction
# ============================================================


def _build_phi_safe_prompt(report: ReflectionReport) -> str:
    """Build the LLM prompt using ONLY structured insight fields.

    Raw memory ``content`` is never read or included. The ``summary`` field
    on each :class:`ReflectionInsight` is a derived artifact from the
    reflection analysis, not a raw memory excerpt, and is included
    intentionally.
    """
    insights_block_lines: list[str] = []
    for idx, insight in enumerate(report.insights, 1):
        insights_block_lines.append(
            f"{idx}. [{insight.insight_type}] "
            f"(confidence={insight.confidence:.2f}, "
            f"action={insight.recommended_action}) {insight.summary}"
        )
    insights_block = "\n".join(insights_block_lines) if insights_block_lines else "(no insights)"

    # Serialize summary fields as compact JSON so the prompt is bounded
    # and predictable.
    trend_str = json.dumps(report.trend_summary, sort_keys=True, default=str)
    entity_str = json.dumps(report.entity_summary, sort_keys=True, default=str)

    return NARRATIVE_PROMPT_TEMPLATE.format(
        n_insights=len(report.insights),
        start_date=report.start_date,
        end_date=report.end_date,
        period=report.period,
        memories_analyzed=report.memories_analyzed,
        insights_block=insights_block,
        trend_summary=trend_str,
        entity_summary=entity_str,
    )


# ============================================================
# Audit + hashing helpers
# ============================================================


def _hash_payload(payload: str) -> str:
    """SHA-256 hex digest of a payload, truncated for log readability."""
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _audit(
    *,
    event_type: str,
    tenant_id: str,
    user_id: str,
    report_id: str,
    model_version: str,
    prompt_hash: str,
    response_hash: str | None,
    latency_ms: float,
    outcome: str,
    audit_log: AuditLog | None = None,
) -> None:
    """Emit a structured audit entry.

    If ``audit_log`` is provided, the entry is written to the audit
    table as a queryable, tenant-isolated, append-only row. Otherwise
    the entry is emitted via :func:`logging.Logger.info` with the same
    fields as ``extra`` (a stopgap for environments without an
    audit-log sink). The metadata dictionary never includes raw prompt
    or response bodies — only hashes and timing.
    """
    metadata: dict[str, Any] = {
        "report_id": report_id,
        "model_version": model_version,
        "prompt_hash": prompt_hash,
        "response_hash": response_hash,
        "latency_ms": latency_ms,
        "outcome": outcome,
    }
    if audit_log is not None:
        try:
            audit_log.record(
                AuditEvent(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    event_type=event_type,
                    resource_id=report_id,
                    metadata=metadata,
                )
            )
            return
        except Exception as exc:  # noqa: BLE001 — fall back to logger
            logger.warning(
                "audit_log.record failed; falling back to logger.info: %s",
                exc,
            )

    logger.info(
        event_type,
        extra={
            "tenant_id": tenant_id,
            "user_id": user_id,
            "report_id": report_id,
            "model_version": model_version,
            "prompt_hash": prompt_hash,
            "response_hash": response_hash,
            "latency_ms": latency_ms,
            "outcome": outcome,
        },
    )


# ============================================================
# Public API
# ============================================================


# Module-level in-process cache. Lost on restart. Sufficient as a stopgap;
# callers may supply a persistent cache via the ``cache`` parameter.
_default_cache: dict[str, str] = {}


def generate_insight_narrative(
    reflection_report: ReflectionReport,
    *,
    tenant_id: str,
    user_id: str,
    llm_call: LLMCallable,
    model_version: str = "caller-default",
    cache: dict[str, str] | None = None,
    audit_log: AuditLog | None = None,
) -> str:
    """Generate a natural-language narrative summary of a reflection report.

    Args:
        reflection_report: A :class:`ReflectionReport` with structured
            insights. The function reads only the structured insight
            fields — never raw memory ``content``.
        tenant_id: Tenant ID for isolation. Required.
        user_id: User ID for isolation. Required.
        llm_call: A callable ``(prompt, tenant_id, user_id) -> str`` that
            invokes the LLM. The caller is responsible for enforcing
            tenant isolation in the underlying LLM client. The callable
            is invoked at most once per ``(tenant_id, user_id, report_id,
            model_version, insights_hash)`` tuple (cache hit short-circuits).
        model_version: Model identifier used for cache keying and audit.
            Defaults to ``"caller-default"`` if the caller does not
            specify.
        cache: Optional cache dict. If provided, used as the cache store.
            If ``None``, the module-level in-process dict is used. The
            cache key includes ``tenant_id`` and ``user_id`` to enforce
            isolation.
        audit_log: Optional :class:`foresight_mcp.audit.AuditLog`. If
            provided, success / error / cache-hit events are persisted
            as queryable, tenant-isolated rows. If ``None`` (the
            default), the module falls back to a structured
            ``logger.info(...)`` call for compatibility. Production
            deployments should always pass a configured ``audit_log``.

    Returns:
        A natural-language narrative string.

    Raises:
        TypeError: If ``reflection_report`` is not a :class:`ReflectionReport`
            or ``llm_call`` is not callable.
        ValueError: If ``tenant_id`` or ``user_id`` is empty.
        ReflectionNarrativeError: If the LLM callable raises. The original
            exception is preserved on ``__cause__``. The caller may catch
            this and fall back to the structured report.
    """
    if not isinstance(reflection_report, ReflectionReport):
        raise TypeError(
            f"reflection_report must be a ReflectionReport, "
            f"got {type(reflection_report).__name__}"
        )
    if not tenant_id or not isinstance(tenant_id, str):
        raise ValueError("tenant_id is required and must be a non-empty string")
    if not user_id or not isinstance(user_id, str):
        raise ValueError("user_id is required and must be a non-empty string")
    if not callable(llm_call):
        raise TypeError("llm_call must be callable")

    if cache is None:
        cache = _default_cache

    cache_key = _compute_cache_key(reflection_report, model_version, tenant_id, user_id)
    if cache_key in cache:
        logger.debug("reflection_narrative_cache_hit", extra={"cache_key": cache_key})
        _audit(
            event_type=NARRATIVE_CACHE_HIT,
            tenant_id=tenant_id,
            user_id=user_id,
            report_id=reflection_report.report_id,
            model_version=model_version,
            prompt_hash="",
            response_hash=None,
            latency_ms=0.0,
            outcome="cache_hit",
            audit_log=audit_log,
        )
        return cache[cache_key]

    prompt = _build_phi_safe_prompt(reflection_report)
    prompt_hash = _hash_payload(prompt)

    start = time.perf_counter()
    try:
        response = llm_call(prompt, tenant_id, user_id)
    except Exception as exc:
        latency_ms = (time.perf_counter() - start) * 1000.0
        _audit(
            event_type=NARRATIVE_FAILED,
            tenant_id=tenant_id,
            user_id=user_id,
            report_id=reflection_report.report_id,
            model_version=model_version,
            prompt_hash=prompt_hash,
            response_hash=None,
            latency_ms=latency_ms,
            outcome="error",
            audit_log=audit_log,
        )
        raise ReflectionNarrativeError(
            f"LLM call failed for report {reflection_report.report_id}: {exc}"
        ) from exc

    latency_ms = (time.perf_counter() - start) * 1000.0
    if not isinstance(response, str):
        response = str(response)
    response_hash = _hash_payload(response)

    _audit(
        event_type=NARRATIVE_GENERATED,
        tenant_id=tenant_id,
        user_id=user_id,
        report_id=reflection_report.report_id,
        model_version=model_version,
        prompt_hash=prompt_hash,
        response_hash=response_hash,
        latency_ms=latency_ms,
        outcome="success",
        audit_log=audit_log,
    )

    cache[cache_key] = response
    return response


__all__ = [
    "LLMCallable",
    "NARRATIVE_PROMPT_TEMPLATE",
    "ReflectionNarrativeError",
    "generate_insight_narrative",
]
