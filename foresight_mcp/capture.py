"""
Lightweight session transcript capture pipeline for PIX-3954.

Provides add-only extraction with deterministic deduplication across five
memory categories: decision, preference, tool_recipe, pattern, pending_item.

The pipeline (CapturePipeline.run) is:
  1. SessionClassifier.should_skip — gate trivial or non-technical sessions
  2. MemoryExtractor.extract — scan user+assistant messages for category patterns
  3. DedupeEngine.check — exact content_hash match (bump) or Jaccard near-duplicate (link)
  4. Persist new UNIQUE memories; link NEAR_DUPLICATE via relationship store
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import BANK_ID
from .connection_pool import get_pool
from .document_layer import content_hash as _content_hash
from .tenant_context import get_current_tenant_id

logger = logging.getLogger("foresight_capture")

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class CapturedMemory:
    """A single extracted memory candidate before dedup."""

    content: str
    category: str  # decision | preference | tool_recipe | pattern | pending_item
    importance: float
    scope: str  # arc | trait | fact | session
    retention: str  # long_term | short_term
    tags: list[str] = field(default_factory=list)
    is_immutable: bool = False


@dataclass
class DedupeResult:
    """Result of checking a candidate against existing memories."""

    status: str  # UNIQUE | DUPLICATE | NEAR_DUPLICATE
    existing_id: str | None = None
    similarity: float | None = None


@dataclass
class CaptureStats:
    """Summary of what the pipeline did."""

    skipped: bool = False
    skip_reason: str = ""
    candidates_found: int = 0
    stored: int = 0
    duplicates: int = 0
    near_duplicates: int = 0
    skipped_candidates: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "candidates_found": self.candidates_found,
            "stored": self.stored,
            "duplicates": self.duplicates,
            "near_duplicates": self.near_duplicates,
            "skipped_candidates": self.skipped_candidates,
        }


# ---------------------------------------------------------------------------
# Category extraction patterns
# ---------------------------------------------------------------------------

# decision: architecture or tool choices that should be preserved as immutable arcs
_DECISION_PATTERNS = [
    re.compile(r"\blet'?s\s+use\b", re.IGNORECASE),
    re.compile(r"\bgoing\s+with\b", re.IGNORECASE),
    re.compile(r"\b(?:i'?ll|we'?ll)\s+go\s+with\b", re.IGNORECASE),
    re.compile(r"\b(?:i|we)\s+decided\s+to\b", re.IGNORECASE),
    re.compile(r"\bchose\b", re.IGNORECASE),
    re.compile(r"\bswitch\s+to\b", re.IGNORECASE),
    re.compile(r"\b(?:we|i)\s+(?:will\s+)?use\b", re.IGNORECASE),
]

# preference: user likes/dislikes that inform future interactions
_PREFERENCE_PATTERNS = [
    re.compile(r"\bi\s+(?:always|usually|generally)\s+", re.IGNORECASE),
    re.compile(r"\bi\s+prefer\b", re.IGNORECASE),
    re.compile(r"\bi\s+(?:like|love|enjoy)\b", re.IGNORECASE),
    re.compile(r"\bi\s+don'?t\s+(?:like|want|need)\b", re.IGNORECASE),
    re.compile(r"\bnever\s+use\b", re.IGNORECASE),
    re.compile(r"\balways\s+use\b", re.IGNORECASE),
    re.compile(r"\bi\s+hate\b", re.IGNORECASE),
]

# tool_recipe: commands, code blocks, problem-solution patterns
_TOOL_RECIPE_PATTERNS = [
    re.compile(r"```"),  # code blocks
    re.compile(r"\bsolved\s+it\b", re.IGNORECASE),
    re.compile(r"\bworked\s+for\s+me\b", re.IGNORECASE),
    re.compile(r"[`]\S+[`]"),  # inline code ticks
    re.compile(r"\w+\.\w+/"),  # file paths with extension
    re.compile(r"\w+:\d+:"),  # file:line references (nano style)
]

# pattern: structural similarities and reusable approaches
_PATTERN_PATTERNS = [
    re.compile(r"\bsame\s+pattern\s+as\b", re.IGNORECASE),
    re.compile(r"\bsimilar\s+approach\b", re.IGNORECASE),
    re.compile(r"\bsame\s+\w+\s+as\b", re.IGNORECASE),
    re.compile(r"\bconsistently\b", re.IGNORECASE),
    re.compile(r"\b(?:this|the\s+same)\s+pattern\b", re.IGNORECASE),
    re.compile(r"\breusable?\b", re.IGNORECASE),
]

# pending_item: follow-up tasks and action items
_PENDING_PATTERNS = [
    re.compile(r"\bTODO\b"),
    re.compile(r"\bNEED\s+TO\b"),
    re.compile(r"\bSHOULD\b"),
    re.compile(r"\bMUST\b"),
    re.compile(r"\bi\s+need\s+to\b", re.IGNORECASE),
    re.compile(r"\bwe\s+need\s+to\b", re.IGNORECASE),
    re.compile(r"\bfollow[\s-]+up\b", re.IGNORECASE),
    re.compile(r"\baction\s+item\b", re.IGNORECASE),
]

# Pattern sets for extraction (category → (patterns, extract_config))
_CATEGORY_PATTERNS: dict[str, tuple[list[re.Pattern], dict[str, Any]]] = {
    "decision": (
        _DECISION_PATTERNS,
        {
            "scope": "arc",
            "importance": 0.7,
            "retention": "long_term",
            "tags": ["auto-captured", "decision"],
            "is_immutable": True,
        },
    ),
    "preference": (
        _PREFERENCE_PATTERNS,
        {"scope": "trait", "importance": 0.6, "retention": "long_term", "tags": ["auto-captured", "preference"]},
    ),
    "tool_recipe": (
        _TOOL_RECIPE_PATTERNS,
        {"scope": "fact", "importance": 0.5, "retention": "short_term", "tags": ["auto-captured", "tool_recipe"]},
    ),
    "pattern": (
        _PATTERN_PATTERNS,
        {"scope": "fact", "importance": 0.5, "retention": "short_term", "tags": ["auto-captured", "pattern"]},
    ),
    "pending_item": (
        _PENDING_PATTERNS,
        {"scope": "session", "importance": 0.7, "retention": "short_term", "tags": ["auto-captured", "pending"]},
    ),
}

# ---------------------------------------------------------------------------
# 1. SessionClassifier
# ---------------------------------------------------------------------------


class SessionClassifier:
    """Determine whether a transcript should be skipped as trivial."""

    MIN_MESSAGES = 3
    MIN_AVG_CHARS = 40
    TECHNICAL_MARKERS = re.compile(
        r"[`{}()\[\]=><]|"
        r"/[\w.-]+/[\w.-]+|"
        r"\.\w{2,4}\b|"
        r"\b(?:install|build|test|deploy|config|api|url|http|git|npm|pip|"
        r"database|server|client|cache|query|async|await|handler|middleware|"
        r"postgres|mysql|mongodb|redis|sqlite|docker|kubernetes|ssl|tls|"
        r"class\b|def\b|function|import|export|const\b|let\b|var\b|"
        r"route|endpoint|refactor|debug|compile|migrate|"
        r"python|typescript|javascript|rust|golang|ruby)\b",
        re.IGNORECASE,
    )

    @classmethod
    def should_skip(cls, messages: list[dict]) -> tuple[bool, str]:
        """Return (skip, reason)."""
        if not messages:
            return True, "no messages"

        if len(messages) < cls.MIN_MESSAGES:
            return True, f"only {len(messages)} messages (min {cls.MIN_MESSAGES})"

        # Check for user messages
        user_msgs = [m for m in messages if m.get("role") == "user"]
        if not user_msgs:
            return True, "no user messages"

        # Average message length check
        all_text = " ".join(m.get("content", "") or "" for m in messages)
        avg_len = len(all_text.strip()) / len(messages)
        if avg_len < cls.MIN_AVG_CHARS:
            return True, f"avg message length {avg_len:.0f} chars (min {cls.MIN_AVG_CHARS})"

        # Technical marker check
        for msg in messages:
            content = msg.get("content", "") or ""
            if cls.TECHNICAL_MARKERS.search(content):
                return False, ""

        return True, "no technical content detected"


# ---------------------------------------------------------------------------
# 2. MemoryExtractor
# ---------------------------------------------------------------------------


class MemoryExtractor:
    """Extract categorized memory candidates from transcript messages."""

    @classmethod
    def extract(cls, messages: list[dict]) -> list[CapturedMemory]:
        """Scan messages and return potential memory candidates."""

        candidates: list[CapturedMemory] = []
        seen: set[str] = set()  # dedup within a single extraction pass

        for msg in messages:
            content = (msg.get("content", "") or "").strip()
            if not content:
                continue

            for category, (patterns, config) in _CATEGORY_PATTERNS.items():
                for pattern in patterns:
                    match = pattern.search(content)
                    if match:
                        # Use the matching line as candidate content (first sentence / line)
                        line = cls._extract_line(content, match.start())

                        # Inline dedup for same-pass
                        dedup_key = f"{category}:{_content_hash(line)}"
                        if dedup_key in seen:
                            continue
                        seen.add(dedup_key)

                        candidates.append(
                            CapturedMemory(
                                content=line,
                                category=category,
                                importance=config["importance"],
                                scope=config["scope"],
                                retention=config["retention"],
                                tags=list(config["tags"]),
                                is_immutable=config.get("is_immutable", False),
                            )
                        )
                        break  # first pattern match per category per message

        return candidates

    @classmethod
    def _extract_line(cls, text: str, pos: int) -> str:
        """Extract the line/sentence around position *pos*."""
        # Try sentence boundaries first
        start = text.rfind(".", 0, pos)
        if start == -1:
            start = text.rfind("\n", 0, pos)
        if start == -1:
            start = max(0, pos - 120)
        else:
            start += 1  # skip delimiter

        end = text.find(".", pos)
        if end == -1:
            end = text.find("\n", pos)
        if end == -1:
            end = min(len(text), pos + 240)

        line = text[start:end].strip()
        # Truncate to 240 chars max
        if len(line) > 240:
            line = line[:237] + "..."
        return line


# ---------------------------------------------------------------------------
# 3. DedupeEngine
# ---------------------------------------------------------------------------


class DedupeEngine:
    """Check candidates against stored memories for exact or near duplicates."""

    JACCARD_THRESHOLD = 0.55
    RECENT_WINDOW_HOURS = 72  # check against last 72h of memories

    @classmethod
    def check(
        cls,
        candidate: CapturedMemory,
        user_id: str,
        tenant_id: str,
    ) -> DedupeResult:
        """Check candidate against existing memories.

        Phase 1: exact content_hash match → DUPLICATE (bump activation)
        Phase 2: Jaccard word overlap > threshold → NEAR_DUPLICATE
        Phase 3: no match → UNIQUE
        """
        pool = get_pool()
        conn = pool.acquire()
        try:
            conn.row_factory = __import__("sqlite3").Row
            stored_content = f"[auto-captured/{candidate.category}] {candidate.content}"
            h = _content_hash(stored_content)

            # Phase 1: exact hash match
            row = conn.execute(
                """SELECT id, activation_count FROM memories
                   WHERE user_id = ? AND tenant_id = ? AND content_hash = ? AND is_ghost = 0
                   ORDER BY created_at DESC LIMIT 1""",
                (user_id, tenant_id, h),
            ).fetchone()

            if row is not None:
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """UPDATE memories SET activation_count = activation_count + 1, updated_at = ?
                       WHERE id = ?""",
                    (now, row["id"]),
                )
                conn.commit()
                return DedupeResult(status="DUPLICATE", existing_id=row["id"], similarity=1.0)

            # Phase 2: Jaccard similarity with recent memories (same user/tenant)
            candidate_words = set(cls._tokenize(candidate.content))
            if not candidate_words:
                return DedupeResult(status="UNIQUE")

            # Fetch recent memories by this user (excluding ghost)
            recent = conn.execute(
                """SELECT id, content FROM memories
                   WHERE user_id = ? AND tenant_id = ? AND is_ghost = 0
                     AND category = ?
                   ORDER BY created_at DESC LIMIT 50""",
                (user_id, tenant_id, candidate.category),
            ).fetchall()

            for row in recent:
                existing_words = set(cls._tokenize(row["content"]))
                union = candidate_words | existing_words
                if not union:
                    continue
                jaccard = len(candidate_words & existing_words) / len(union)
                if jaccard >= cls.JACCARD_THRESHOLD:
                    return DedupeResult(
                        status="NEAR_DUPLICATE",
                        existing_id=row["id"],
                        similarity=round(jaccard, 4),
                    )

            return DedupeResult(status="UNIQUE")

        finally:
            pool.release(conn)
            conn.close()

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """Split text into lowercased word tokens."""
        return re.findall(r"\w+", text.lower())


# ---------------------------------------------------------------------------
# 4. CapturePipeline
# ---------------------------------------------------------------------------


class CapturePipeline:
    """Orchestrate the add-only capture flow for a session transcript."""

    def __init__(self) -> None:
        self.classifier = SessionClassifier()
        self.extractor = MemoryExtractor()
        self.dedupe = DedupeEngine()

    def run(
        self,
        session_id: str,
        messages: list[dict],
        user_id: str,
        tenant_id: str | None = None,
    ) -> CaptureStats:
        """Run the full capture pipeline.

        Returns CaptureStats summarizing what happened.
        """
        stats = CaptureStats()
        tid = tenant_id or get_current_tenant_id()
        now = datetime.now(timezone.utc).isoformat()

        # Phase 1: Classify
        skip, reason = self.classifier.should_skip(messages)
        if skip:
            stats.skipped = True
            stats.skip_reason = reason
            logger.info("Session %s skipped: %s", session_id, reason)
            return stats

        # Phase 2: Extract
        candidates = self.extractor.extract(messages)
        stats.candidates_found = len(candidates)

        if not candidates:
            logger.info("Session %s: no candidates extracted", session_id)
            return stats

        # Phase 3-4: Dedupe and persist
        pool = get_pool()
        candidate_map = {
            "decision": [],
            "preference": [],
            "tool_recipe": [],
            "pattern": [],
            "pending_item": [],
        }

        for candidate in candidates:
            dedupe = self.dedupe.check(candidate, user_id, tid)
            if dedupe.status == "DUPLICATE":
                stats.duplicates += 1
                continue

            if dedupe.status == "NEAR_DUPLICATE":
                stats.near_duplicates += 1
                # Still store as new memory, but link via derives
                candidate_map[candidate.category].append((candidate, dedupe))
                continue

            candidate_map[candidate.category].append((candidate, dedupe))

        # Persist candidates
        conn = pool.acquire()
        try:
            conn.row_factory = __import__("sqlite3").Row
            for category, items in candidate_map.items():
                if not items:
                    continue
                for candidate, dedupe in items:
                    content = f"[auto-captured/{candidate.category}] {candidate.content}"
                    h = _content_hash(content)
                    mid = hashlib.sha256(f"{content}{now}".encode()).hexdigest()[:16]

                    conn.execute(
                        """INSERT OR IGNORE INTO memories
                           (id, content, content_hash, scope, retention, category, user_id, bank_id, tenant_id,
                            created_at, updated_at, tags, emotional_context, metrics,
                            is_ghost, synthesized_from, importance)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', '{}', 0, '[]', ?)""",
                        (
                            mid,
                            content,
                            h,
                            candidate.scope,
                            candidate.retention,
                            candidate.category,
                            user_id,
                            BANK_ID,
                            tid,
                            now,
                            now,
                            json.dumps(candidate.tags),
                            candidate.importance,
                        ),
                    )
                    conn.commit()

                    # Link near duplicates via direct INSERT (same connection avoids FK issues)
                    if dedupe.status == "NEAR_DUPLICATE" and dedupe.existing_id and dedupe.existing_id != mid:
                        rel_id = hashlib.sha256(f"{mid}-derives-{dedupe.existing_id}".encode()).hexdigest()[:16]
                        conn.execute(
                            """INSERT OR IGNORE INTO memory_relationships
                               (id, tenant_id, user_id, source_memory_id, target_memory_id,
                                relationship_type, confidence, metadata, created_at)
                               VALUES (?, ?, ?, ?, ?, 'derives', 1.0, '{}', ?)""",
                            (rel_id, tid, user_id, mid, dedupe.existing_id, now),
                        )
                        conn.commit()

                    if dedupe.status != "DUPLICATE":
                        stats.stored += 1
        finally:
            pool.release(conn)
            conn.close()

        logger.info(
            "Session %s: %d candidates → %d stored, %d duplicates, %d near-dups",
            session_id,
            stats.candidates_found,
            stats.stored,
            stats.duplicates,
            stats.near_duplicates,
        )
        return stats


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

_pipeline_instance: CapturePipeline | None = None


def get_capture_pipeline() -> CapturePipeline:
    """Return the process-singleton CapturePipeline."""
    global _pipeline_instance
    if _pipeline_instance is None:
        _pipeline_instance = CapturePipeline()
    return _pipeline_instance


def reset_capture_pipeline() -> None:
    """Reset singleton (test-only helper)."""
    global _pipeline_instance
    _pipeline_instance = None
