"""Clinical/safety/privacy sensitivity tagging for memory capture.

PIX-3956: per-tenant, opt-in flag raised on memory insert when content
matches a clinical/PHI detector. The detector never inspects content
already in storage; it only inspects incoming text at the capture
boundary. Maintenance flows downstream read the resulting
``is_sensitive`` column to decide what they are allowed to touch.

Detector layers (in priority order):

1. **PII regex** — SSN, MRN, credit-card, phone, email patterns.
2. **Clinical keyword set** — small starter list, overridable via the
   ``FORESIGHT_SENSITIVITY_KEYWORDS`` env var (comma-separated, lower-case).
3. **Explicit caller flag** — the store action accepts an
   ``opts.is_sensitive`` boolean to force-on or force-off the bit.

The keyword set is intentionally conservative. False positives are
preferred to false negatives: low-risk automatic maintenance skips
sensitive rows either way, so a too-broad detector only widens that
skip set.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

DEFAULT_CLINICAL_KEYWORDS: tuple[str, ...] = (
    "patient",
    "diagnosis",
    "diagnostic",
    "prescription",
    "prescribed",
    "medication",
    "dosage",
    "phi",
    "protected health information",
    "medical record",
    "medical history",
    "ssn",
    "social security",
    "mrn",
    "medical record number",
    "insurance id",
    "credit card",
    "date of birth",
    "dob",
    "psychiatric",
    "therapy session",
    "therapist",
    "counselor notes",
    "substance use",
    "suicide",
    "suicidal ideation",
    "self harm",
    "abuse",
)


@dataclass(frozen=True)
class SensitivityVerdict:
    """Outcome of evaluating ``is_sensitive`` for a single content string."""

    is_sensitive: bool
    reason: str | None
    matched_pattern: str | None


def _compile_default_patterns() -> list[re.Pattern[str]]:
    return [
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # US SSN NNN-NN-NNNN
        re.compile(r"\b\d{9}\b"),  # bare 9-digit SSN
        re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"),  # credit card
        re.compile(r"\b\d{3}-\d{3}-\d{4}\b"),  # US phone
        re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),  # email
        re.compile(r"\bMRN[:\s#-]*\d{4,}\b", re.IGNORECASE),
    ]


def _load_keywords() -> tuple[str, ...]:
    override = os.environ.get("FORESIGHT_SENSITIVITY_KEYWORDS", "").strip()
    if not override:
        return DEFAULT_CLINICAL_KEYWORDS
    extras = tuple(k.strip().lower() for k in override.split(",") if k.strip())
    if not extras:
        return DEFAULT_CLINICAL_KEYWORDS
    return tuple(sorted(set(DEFAULT_CLINICAL_KEYWORDS) | set(extras)))


class SensitivityDetector:
    """Evaluate whether content should be flagged ``is_sensitive`` at capture.

    Thread-safe — pattern list and keyword set are captured at construction
    time and never mutated. Instances are cheap to keep on a module-level
    singleton.
    """

    def __init__(
        self,
        extra_keywords: tuple[str, ...] = (),
        extra_patterns: tuple[re.Pattern[str], ...] = (),
    ) -> None:
        keywords = set(_load_keywords())
        keywords.update(k.lower() for k in extra_keywords)
        self._keywords: tuple[str, ...] = tuple(sorted(keywords))
        self._patterns: tuple[re.Pattern[str], ...] = (
            *_compile_default_patterns(),
            *extra_patterns,
        )
        self._keyword_set = frozenset(self._keywords)

    def evaluate(self, content: str | None) -> SensitivityVerdict:
        if not content:
            return SensitivityVerdict(False, None, None)
        lowered = content.lower()
        for pattern in self._patterns:
            match = pattern.search(content)
            if match is not None:
                return SensitivityVerdict(True, "pii_pattern", match.group(0))
        for word in self._keywords:
            if word and word in lowered:
                return SensitivityVerdict(True, "clinical_keyword", word)
        return SensitivityVerdict(False, None, None)


_DEFAULT_DETECTOR = SensitivityDetector()


def detect_sensitivity(content: str | None) -> SensitivityVerdict:
    """Module-level helper that reuses a single :class:`SensitivityDetector`."""
    return _DEFAULT_DETECTOR.evaluate(content)


def resolve_is_sensitive(
    opts_is_sensitive: bool | None,
    content: str | None,
) -> tuple[bool, str | None]:
    """Combine caller override with detector verdict.

    - ``opts_is_sensitive is True`` wins (forced-on). Reason recorded as
      ``"caller_override"``.
    - ``opts_is_sensitive is False`` wins (forced-off). Returned reason is
      ``None`` so callers can short-circuit inspecting the detector.
    - ``opts_is_sensitive is None`` defers to the detector verdict.
    """
    if opts_is_sensitive is True:
        return True, "caller_override"
    if opts_is_sensitive is False:
        return False, None
    verdict = detect_sensitivity(content)
    return verdict.is_sensitive, verdict.reason
