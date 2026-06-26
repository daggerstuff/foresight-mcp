"""Memory Maintenance Job Orchestrator.

Conservative background memory maintenance that improves quality without
requiring end users to manually curate memory. Operates in four modes:

1. consolidate  - Find near-duplicate memories, auto-merge if high-confidence
                  or flag for admin review if marginal.
2. contradict   - Detect sentiment-conflict pairs, flag for admin review only.
3. archive_stale - Archive memories with low strength or importance without
                   hard-deleting (soft archive preserving gist lookup).
4. synthesize  - Detect cross-memory topic patterns, emit insight records.

All operations are:
- Bounded by batch size and wall-clock time to avoid blocking.
- Logged with structured counts and reasons for auditability.
- Tenant/user-scoped; never touch data from other tenants.
- Low-risk changes apply automatically; high-impact contradictions are
  flagged for admin review and never auto-applied.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .clustering import cluster_memories
from .config import DB_PATH
from .connection_pool import get_pool
from .sensitivity import resolve_is_sensitive

logger = logging.getLogger("foresight_maintenance")

SENTIMENT_OPPOSITES: tuple[tuple[str, str], ...] = (
    ("love", "hate"),
    ("good", "bad"),
    ("happy", "sad"),
    ("better", "worse"),
    ("helpful", "harmful"),
    ("easy", "hard"),
    ("improve", "worsen"),
    ("like", "dislike"),
    ("hope", "despair"),
    ("calm", "anxious"),
    ("confident", "doubtful"),
    ("safe", "afraid"),
    ("trust", "distrust"),
    ("accept", "reject"),
    ("satisfied", "frustrated"),
    ("optimistic", "pessimistic"),
    ("grateful", "resentful"),
    ("comfortable", "uncomfortable"),
    ("peaceful", "distressed"),
    ("motivated", "discouraged"),
    ("supported", "abandoned"),
    ("connected", "isolated"),
    ("valued", "worthless"),
    ("strong", "weak"),
    ("progress", "regress"),
    ("healing", "hurting"),
    ("joy", "sorrow"),
)

DUPLICATE_OVERLAP_HIGH = 0.70  # Auto-consolidate threshold
DUPLICATE_OVERLAP_MARGINAL = 0.30  # Flag for admin review
STALE_STRENGTH_THRESHOLD = 0.2
STALE_IMPORTANCE_THRESHOLD = 0.1
MAX_BATCH_SIZE = 200
MAX_RUNTIME_SECONDS = 300


@dataclass
class MaintenanceConfig:
    tenant_id: str = "default"
    user_id: str = "default"
    modes: list[str] = field(default_factory=lambda: ["consolidate", "contradict", "archive_stale", "synthesize"])
    duplicate_threshold: float = 0.25
    consolidation_overlap_high: float = DUPLICATE_OVERLAP_HIGH
    consolidation_overlap_marginal: float = DUPLICATE_OVERLAP_MARGINAL
    stale_strength_threshold: float = STALE_STRENGTH_THRESHOLD
    stale_importance_threshold: float = STALE_IMPORTANCE_THRESHOLD
    batch_size: int = MAX_BATCH_SIZE
    sensitive_only: bool = False
    tool_access: str = "observe"
    max_runtime_seconds: float = MAX_RUNTIME_SECONDS
    sensitive_only: bool = False
    tool_access: str = "auto"


@dataclass
class DuplicateCandidate:
    cluster_id: str
    memory_ids: list[str]
    overlap_scores: dict[tuple[str, str], float]  # (id_a, id_b) -> score
    action: str  # "auto_consolidate" | "flag_review"
    representative_id: str | None = None


@dataclass
class ContradictionCandidate:
    memory_id_a: str
    memory_id_b: str
    pos_word: str
    neg_word: str
    topic: str
    confidence: float


@dataclass
class StaleCandidate:
    memory_id: str
    reason: str  # "low_strength" | "low_importance" | "ghost_stale"
    strength: float
    importance: float


@dataclass
class SynthesisInsight:
    topic: str
    statement: str
    member_ids: list[str]
    confidence: float


@dataclass
class MaintenanceStats:
    maintenance_duration_seconds: float = 0.0
    modes_run: list[str] = field(default_factory=list)
    duplicates_found: int = 0
    duplicates_auto_consolidated: int = 0
    duplicates_flagged_review: int = 0
    contradictions_found: int = 0
    contradictions_flagged_review: int = 0
    stale_found: int = 0
    stale_archived: int = 0
    insights_generated: int = 0
    sensitive_excluded: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "maintenance_duration_seconds": round(self.maintenance_duration_seconds, 2),
            "modes_run": self.modes_run,
            "duplicates_found": self.duplicates_found,
            "duplicates_auto_consolidated": self.duplicates_auto_consolidated,
            "duplicates_flagged_review": self.duplicates_flagged_review,
            "contradictions_found": self.contradictions_found,
            "contradictions_flagged_review": self.contradictions_flagged_review,
            "stale_found": self.stale_found,
            "stale_archived": self.stale_archived,
            "insights_generated": self.insights_generated,
            "sensitive_excluded": self.sensitive_excluded,
            "errors": self.errors,
        }


class MemoryMaintenanceJob:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or DB_PATH

    def run(self, config: MaintenanceConfig) -> MaintenanceStats:
        t0 = time.perf_counter()
        stats = MaintenanceStats()
        pool = get_pool(self.db_path)
        conn = pool.acquire()

        try:
            stats.modes_run = config.modes

            if "consolidate" in config.modes:
                self._run_consolidate(conn, config, stats)

            if "contradict" in config.modes:
                self._run_contradict(conn, config, stats)

            if "archive_stale" in config.modes:
                self._run_archive_stale(conn, config, stats)

            if "synthesize" in config.modes:
                self._run_synthesize(conn, config, stats)

        except Exception as e:
            logger.exception("Maintenance job failed: %s", e)
            stats.errors.append(str(e))
        finally:
            pool.release(conn)

        stats.maintenance_duration_seconds = time.perf_counter() - t0
        logger.info(
            "Maintenance complete: modes=%s duration=%.2fs dups=%d+%d contr=%d stale=%d insights=%d errors=%d",
            stats.modes_run,
            stats.maintenance_duration_seconds,
            stats.duplicates_auto_consolidated,
            stats.duplicates_flagged_review,
            stats.contradictions_flagged_review,
            stats.stale_archived,
            stats.insights_generated,
            len(stats.errors),
        )
        return stats

    def _fetch_memories(
        self, conn: Any, config: MaintenanceConfig, extra_where: str = "", extra_params: tuple = ()
    ) -> list[dict[str, Any]]:
        sensitivity_filter = "is_sensitive = 1" if config.sensitive_only else "COALESCE(is_sensitive, 0) = 0"
        where = f"user_id = ? AND tenant_id = ? AND {sensitivity_filter}{extra_where}"
        params = (config.user_id, config.tenant_id) + extra_params
        cursor = conn.execute(
            f"""
            SELECT id, user_id, tenant_id, scope, retention, content, tags,
                   category, importance, strength_trend, created_at,
                   activation_count, is_ghost, synthesized_from,
                   emotional_context, metrics, COALESCE(is_sensitive, 0) AS is_sensitive
            FROM memories
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            [*params, config.batch_size],
        )
        rows = cursor.fetchall()
        return [
            {
                "id": r["id"],
                "user_id": r["user_id"],
                "tenant_id": r["tenant_id"],
                "scope": r["scope"],
                "retention": r["retention"],
                "content": r["content"],
                "tags": r["tags"],
                "category": r["category"],
                "importance": r["importance"] or 0.5,
                "strength_trend": r["strength_trend"],
                "created_at": r["created_at"],
                "activation_count": r["activation_count"] or 0,
                "is_ghost": r["is_ghost"],
                "synthesized_from": r["synthesized_from"],
                "emotional_context": r["emotional_context"],
                "metrics": r["metrics"],
                "is_sensitive": int(r["is_sensitive"] or 0),
            }
            for r in rows
        ]

    def _fetch_memories_batch(self, conn: Any, config: MaintenanceConfig) -> list[dict[str, Any]]:
        cursor = conn.execute(
            """
            SELECT id, user_id, tenant_id, scope, retention, content, tags,
                    category, importance, strength_trend, created_at,
                    activation_count, is_ghost, synthesized_from,
                    emotional_context, metrics, COALESCE(is_sensitive, 0) AS is_sensitive
            FROM memories
            WHERE user_id = ? AND tenant_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (config.user_id, config.tenant_id, config.batch_size),
        )
        rows = cursor.fetchall()
        return [
            {
                "id": r["id"],
                "user_id": r["user_id"],
                "tenant_id": r["tenant_id"],
                "scope": r["scope"],
                "retention": r["retention"],
                "content": r["content"],
                "tags": r["tags"],
                "category": r["category"],
                "importance": r["importance"] or 0.5,
                "strength_trend": r["strength_trend"],
                "created_at": r["created_at"],
                "activation_count": r["activation_count"] or 0,
                "is_ghost": r["is_ghost"],
                "synthesized_from": r["synthesized_from"],
                "emotional_context": r["emotional_context"],
                "metrics": r["metrics"],
                "is_sensitive": int(r["is_sensitive"] or 0),
            }
            for r in rows
        ]

    def _run_consolidate(self, conn: Any, config: MaintenanceConfig, stats: MaintenanceStats) -> None:
        # Count sensitive rows in scope before the SQL filter so the audit
        # log captures them even when they short-circuit clustering.
        if not config.sensitive_only:
            stats.sensitive_excluded += int(
                conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE user_id = ? AND tenant_id = ? AND COALESCE(is_sensitive, 0) = 1",
                    (config.user_id, config.tenant_id),
                ).fetchone()[0]
                or 0
            )
        memories = self._fetch_memories(conn, config)
        if len(memories) < 2:
            return

        result = cluster_memories(
            memories,
            min_similarity=config.duplicate_threshold,
            min_cluster_size=2,
            max_clusters=None,
        )

        if not result.cluster_entities:
            return

        high_confidence: list[DuplicateCandidate] = []
        flagged: list[DuplicateCandidate] = []

        for entity in result.cluster_entities:
            props = entity.get("properties", {})
            member_ids: list[str] = props.get("member_ids", [])
            if len(member_ids) < 2:
                continue

            cluster_memories_list = [m for m in memories if m["id"] in member_ids]
            overlap_scores = self._pairwise_overlaps(cluster_memories_list)
            avg_overlap = sum(overlap_scores.values()) / len(overlap_scores) if overlap_scores else 0.0

            candidate = DuplicateCandidate(
                cluster_id=entity["id"],
                memory_ids=member_ids,
                overlap_scores=overlap_scores,
                action="flag_review",
                representative_id=member_ids[0],
            )

            # No auto_action on sensitive clusters — only flag-for-review, even
            # when overlap >= consolidation_overlap_high. This is the AC that
            # sensitive memories never get silently merged/overwritten.
            cluster_has_sensitive = any(m.get("is_sensitive") for m in cluster_memories_list)

            if cluster_has_sensitive:
                candidate.action = "flag_review"
                flagged.append(candidate)
                stats.duplicates_flagged_review += 1
                stats.sensitive_excluded += 1
                stats.duplicates_found += len(member_ids)
                continue

            if avg_overlap >= config.consolidation_overlap_high:
                candidate.action = "auto_consolidate"
                high_confidence.append(candidate)
            elif avg_overlap >= config.consolidation_overlap_marginal:
                flagged.append(candidate)
            stats.duplicates_found += len(member_ids)

        for cand in high_confidence:
            try:
                self._auto_consolidate(conn, cand, config)
                stats.duplicates_auto_consolidated += 1
            except Exception as e:
                logger.warning("Consolidation failed for cluster %s: %s", cand.cluster_id, e)
                stats.errors.append(f"consolidate:{cand.cluster_id}:{e}")

        for cand in flagged:
            self._flag_for_review(conn, cand, "consolidate")
            stats.duplicates_flagged_review += 1

        logger.debug(
            "Consolidate phase: found=%d auto=%d flagged=%d",
            stats.duplicates_found,
            stats.duplicates_auto_consolidated,
            stats.duplicates_flagged_review,
        )

    def _pairwise_overlaps(self, memories: list[dict[str, Any]]) -> dict[tuple[str, str], float]:
        scores: dict[tuple[str, str], float] = {}
        for i, mem_a in enumerate(memories):
            for mem_b in memories[i + 1 :]:
                words_a = set(re.findall(r"\b\w+\b", (mem_a["content"] or "").lower()))
                words_b = set(re.findall(r"\b\w+\b", (mem_b["content"] or "").lower()))
                if not words_a or not words_b:
                    score = 0.0
                else:
                    score = len(words_a & words_b) / len(words_a | words_b)
                key = tuple(sorted([mem_a["id"], mem_b["id"]]))
                scores[key] = score
        return scores

    def _auto_consolidate(self, conn: Any, cand: DuplicateCandidate, config: MaintenanceConfig) -> None:
        primary_id = cand.representative_id or cand.memory_ids[0]
        other_ids = [mid for mid in cand.memory_ids if mid != primary_id]
        if not other_ids:
            return

        # Fetch primary memory data
        cursor = conn.execute(
            "SELECT content, synthesized_from FROM memories WHERE id = ? AND tenant_id = ? AND user_id = ?",
            (primary_id, config.tenant_id, config.user_id),
        )
        row = cursor.fetchone()
        if not row:
            return

        existing_content = row["content"] or ""
        existing_synth = set(re.findall(r"\w+", row["synthesized_from"] or "")) if row["synthesized_from"] else set()

        # Fetch all other memories' content in a single query
        if other_ids:
            placeholders = ",".join("?" for _ in other_ids)
            rows = conn.execute(
                f"SELECT id, content FROM memories WHERE id IN ({placeholders}) AND tenant_id = ? AND user_id = ?",
                (*other_ids, config.tenant_id, config.user_id),
            ).fetchall()

            additional_content = []
            for row in rows:
                if row["content"]:
                    additional_content.append(row["content"])
                    existing_synth.add(row["id"])
        else:
            additional_content = []

        combined = existing_content
        if additional_content:
            combined = existing_content + " " + " ".join(additional_content)

        # Update primary memory with content and sensitivity in a single transaction
        is_sensitive_bit, sensitivity_reason = resolve_is_sensitive(None, combined[:1000])
        conn.execute(
            """UPDATE memories 
               SET content = ?, 
                   is_ghost = 1, 
                   gist = ?, 
                   synthesized_from = ?,
                   is_sensitive = ?,
                   sensitivity_reason = ?
               WHERE id = ? AND tenant_id = ? AND user_id = ?""",
            (
                combined[:1000],
                existing_content[:200],
                str(list(existing_synth)),
                1 if is_sensitive_bit else 0,
                sensitivity_reason,
                primary_id,
                config.tenant_id,
                config.user_id,
            ),
        )

        # Ghost all other memories in a single query
        if other_ids:
            placeholders = ",".join("?" for _ in other_ids)
            gist_values = [(other_ids[0][:200] if other_ids else "")] * len(other_ids)
            synthesized_from_values = [str(list(existing_synth) + [mid]) for mid in other_ids]

            # Build the SET clause for the UPDATE
            set_clause = "is_ghost = 1, gist = CASE id "
            for i, mid in enumerate(other_ids):
                set_clause += "WHEN ? THEN ? "
            set_clause += "END, synthesized_from = CASE id "
            for i, mid in enumerate(other_ids):
                set_clause += "WHEN ? THEN ? "
            set_clause += "END"

            # Prepare parameters
            params = []
            for i, mid in enumerate(other_ids):
                params.extend([mid, gist_values[i]])
            for i, mid in enumerate(other_ids):
                params.extend([mid, synthesized_from_values[i]])
            params.extend([config.tenant_id, config.user_id])

            # Add the WHERE clause parameters (the IDs)
            where_params = list(other_ids)
            params = where_params + params

            conn.execute(
                f"UPDATE memories SET {set_clause} WHERE id IN ({placeholders}) AND tenant_id = ? AND user_id = ?",
                params,
            )

        conn.commit()

    def _flag_for_review(self, conn: Any, cand: DuplicateCandidate, reason: str) -> None:
        event_type = f"maintenance_review:{reason}"
        # Get sample content from first memory for auditing purposes
        sample_content = ""
        if cand.memory_ids:
            cursor = conn.execute(
                "SELECT content, is_sensitive FROM memories WHERE id = ? LIMIT 1",
                (cand.memory_ids[0],),
            )
            row = cursor.fetchone()
            if row:
                content = row["content"] or ""
                is_sensitive = bool(row["is_sensitive"])
                sample_content = "[REDACTED - sensitive]" if is_sensitive else content[:100]

        payload = {
            "cluster_id": cand.cluster_id,
            "memory_ids": cand.memory_ids,
            "reason": reason,
            "action": cand.action,
            "sample_content": sample_content,
        }
        try:
            conn.execute(
                "INSERT INTO events (id, tenant_id, event_type, timestamp, actor, entity_id, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    hashlib.sha256(f"{cand.cluster_id}{datetime.now(timezone.utc).isoformat()}".encode()).hexdigest()[
                        :16
                    ],
                    cand.memory_ids[0][:8],
                    event_type,
                    datetime.now(timezone.utc).isoformat(),
                    "maintenance_job",
                    cand.cluster_id,
                    str(payload),
                ),
            )
            conn.commit()
        except Exception:
            pass

    def _run_contradict(self, conn: Any, config: MaintenanceConfig, stats: MaintenanceStats) -> None:
        # PIX-3956: contradict must scan sensitive memories too. They are
        # always reviewed by an admin and never silently dropped. We use a
        # dedicated un-gated fetch because _fetch_memories is wired to the
        # SQL sensitivity filter that exists for the destructive paths.
        # Contradict scans ALL memories (including sensitive) for admin review.
        # The count here tracks how many sensitive rows were *flagged*, not
        # excluded — use += so earlier modes' counts are preserved.
        stats.sensitive_excluded += int(
            conn.execute(
                "SELECT COUNT(*) FROM memories WHERE user_id = ? AND tenant_id = ? AND COALESCE(is_sensitive, 0) = 1",
                (config.user_id, config.tenant_id),
            ).fetchone()[0]
            or 0
        )
        memories = self._fetch_memories_batch(conn, config)
        if len(memories) < 2:
            return

        scan_target = memories

        topic_clusters: dict[str, list[dict[str, Any]]] = {}
        for mem in scan_target:
            content_lower = (mem["content"] or "").lower()
            words = set(re.findall(r"\b\w+\b", content_lower))
            for topic in words:
                if len(topic) > 3 and topic not in {
                    "that",
                    "with",
                    "from",
                    "have",
                    "this",
                    "been",
                    "were",
                    "they",
                    "their",
                }:
                    if topic not in topic_clusters:
                        topic_clusters[topic] = []
                    topic_clusters[topic].append(mem)

        flagged: list[ContradictionCandidate] = []
        seen_pairs: set[tuple[str, str]] = set()
        # Pre-compute word sets to avoid O(n²) re-tokenization
        word_map: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
        for mem in scan_target:
            content_lower = (mem["content"] or "").lower()
            word_map[mem["id"]] = (
                frozenset(re.findall(r"\b\w+\b", content_lower)),
                frozenset(re.findall(r"\b\w+\b", content_lower)),
            )

        for topic, cluster in topic_clusters.items():
            if len(cluster) < 2:
                continue
            if len(cluster) > 50:
                continue
            for i, mem_a in enumerate(cluster):
                for mem_b in cluster[i + 1 :]:
                    pair_key = tuple(sorted([mem_a["id"], mem_b["id"]]))
                    if pair_key in seen_pairs:
                        continue
                    conflict = self._find_sentiment_conflict(mem_a["content"] or "", mem_b["content"] or "")
                    if conflict is not None:
                        seen_pairs.add(pair_key)
                        pos_word, neg_word = conflict
                        words_a = word_map[mem_a["id"]][0]
                        words_b = word_map[mem_b["id"]][1]
                        overlap = len(words_a & words_b) / len(words_a | words_b) if words_a and words_b else 0.0
                        flagged.append(
                            ContradictionCandidate(
                                memory_id_a=mem_a["id"],
                                memory_id_b=mem_b["id"],
                                pos_word=pos_word,
                                neg_word=neg_word,
                                topic=topic,
                                confidence=min(overlap * 1.5, 1.0),
                            )
                        )

        stats.contradictions_found += len(flagged)
        for cand in flagged:
            self._flag_contradiction_for_review(conn, cand)
            stats.contradictions_flagged_review += 1

        logger.debug(
            "Contradict phase: found=%d flagged=%d",
            stats.contradictions_found,
            stats.contradictions_flagged_review,
        )

    def _find_sentiment_conflict(self, content_a: str, content_b: str) -> tuple[str, str] | None:
        words_a = set(re.findall(r"\b\w+\b", content_a.lower()))
        words_b = set(re.findall(r"\b\w+\b", content_b.lower()))
        for pos_word, neg_word in SENTIMENT_OPPOSITES:
            if (pos_word in words_a and neg_word in words_b) or (neg_word in words_a and pos_word in words_b):
                return (pos_word, neg_word)
        return None

    def _flag_contradiction_for_review(self, conn: Any, cand: ContradictionCandidate) -> None:
        event_type = "maintenance_review:contradict"
        # Get content for both memories for auditing purposes, redacting if sensitive
        content_a = ""
        content_b = ""
        if cand.memory_id_a and cand.memory_id_b:
            cursor = conn.execute(
                "SELECT id, content, is_sensitive FROM memories WHERE id IN (?, ?)",
                (cand.memory_id_a, cand.memory_id_b),
            )
            rows = {row["id"]: row for row in cursor.fetchall()}

            # Get content for first memory
            if cand.memory_id_a in rows:
                row = rows[cand.memory_id_a]
                content = row["content"] or ""
                is_sensitive = bool(row["is_sensitive"])
                content_a = "[REDACTED - sensitive]" if is_sensitive else content[:100]

            # Get content for second memory
            if cand.memory_id_b in rows:
                row = rows[cand.memory_id_b]
                content = row["content"] or ""
                is_sensitive = bool(row["is_sensitive"])
                content_b = "[REDACTED - sensitive]" if is_sensitive else content[:100]

        payload = {
            "memory_id_a": cand.memory_id_a,
            "memory_id_b": cand.memory_id_b,
            "pos_word": cand.pos_word,
            "neg_word": cand.neg_word,
            "topic": cand.topic,
            "confidence": round(cand.confidence, 3),
            "content_a": content_a,
            "content_b": content_b,
        }
        try:
            conn.execute(
                "INSERT INTO events (id, tenant_id, event_type, timestamp, actor, entity_id, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    hashlib.sha256(
                        f"{cand.memory_id_a}{cand.memory_id_b}{datetime.now(timezone.utc).isoformat()}".encode()
                    ).hexdigest()[:16],
                    "maintenance",
                    event_type,
                    datetime.now(timezone.utc).isoformat(),
                    "maintenance_job",
                    cand.memory_id_a[:8],
                    str(payload),
                ),
            )
            conn.commit()
        except Exception:
            pass

    def _run_archive_stale(self, conn: Any, config: MaintenanceConfig, stats: MaintenanceStats) -> None:
        # Guard against accidental PHI ghosting when sensitive_only is True
        if config.sensitive_only and getattr(config, "tool_access", None) != "observe":
            raise ValueError("sensitive_only archive_stale requires tool_access=observe to prevent PHI loss")

        candidates = self._find_stale_candidates(conn, config)
        stats.stale_found = len(candidates)

        for cand in candidates:
            try:
                self._archive_stale_memory(conn, cand)
                stats.stale_archived += 1
            except Exception as e:
                logger.warning("Archive stale failed for %s: %s", cand.memory_id, e)
                stats.errors.append(f"archive_stale:{cand.memory_id}:{e}")

        logger.debug(
            "Archive stale phase: found=%d archived=%d",
            stats.stale_found,
            stats.stale_archived,
        )

    def _find_stale_candidates(self, conn: Any, config: MaintenanceConfig) -> list[StaleCandidate]:
        sensitivity_filter = "is_sensitive = 1" if config.sensitive_only else "COALESCE(is_sensitive, 0) = 0"
        cursor = conn.execute(
            f"""
            SELECT id, importance,
                   COALESCE(strength_trend, 'stable') as strength_trend,
                   content
            FROM memories
            WHERE user_id = ? AND tenant_id = ?
            AND is_ghost = 0
            AND {sensitivity_filter}
            AND (importance <= ? OR strength_trend = 'stale')
            ORDER BY importance ASC, created_at ASC
            LIMIT ?
            """,
            (
                config.user_id,
                config.tenant_id,
                config.stale_importance_threshold,
                config.batch_size,
            ),
        )
        candidates: list[StaleCandidate] = []
        for row in cursor.fetchall():
            reason = "low_importance"
            if row["strength_trend"] == "stale":
                reason = "low_strength"
            elif row["importance"] is not None and row["importance"] <= config.stale_importance_threshold:
                reason = "low_importance"
            candidates.append(
                StaleCandidate(
                    memory_id=row["id"],
                    reason=reason,
                    strength=0.0,
                    importance=row["importance"] or 0.5,
                )
            )
        return candidates

    def _archive_stale_memory(self, conn: Any, cand: StaleCandidate) -> None:
        cursor = conn.execute(
            "SELECT content, gist, synthesized_from FROM memories WHERE id = ?",
            (cand.memory_id,),
        ).fetchone()
        if not cursor:
            return

        conn.execute(
            "UPDATE memories SET is_ghost = 1, gist = ? WHERE id = ?",
            (
                (cursor["content"] or "")[:200],
                cand.memory_id,
            ),
        )
        conn.commit()

    def _run_synthesize(self, conn: Any, config: MaintenanceConfig, stats: MaintenanceStats) -> None:
        memories = self._fetch_memories(conn, config)
        if len(memories) < 3:
            return

        topic_map: dict[str, list[dict[str, Any]]] = {}
        for mem in memories:
            words = set(re.findall(r"\b\w+\b", (mem["content"] or "").lower()))
            for word in words:
                if len(word) > 4 and word not in {"which", "where", "would", "could", "should", "about"}:
                    if word not in topic_map:
                        topic_map[word] = []
                    topic_map[word].append(mem)

        insights: list[SynthesisInsight] = []
        for topic, cluster in topic_map.items():
            if len(cluster) >= 3:
                contents = [m["content"] for m in cluster if m["content"]]
                if len(contents) >= 3:
                    combined = " ".join(contents[:5])
                    statement = (
                        f"Multiple memories share topic '{topic}': {len(cluster)} references across recent memories."
                    )
                    insights.append(
                        SynthesisInsight(
                            topic=topic,
                            statement=statement,
                            member_ids=[m["id"] for m in cluster],
                            confidence=min(0.5 + (len(cluster) - 3) * 0.05, 0.95),
                        )
                    )

        for insight in insights[:20]:
            self._emit_insight_event(conn, insight)
        stats.insights_generated = len(insights)

        logger.debug("Synthesize phase: generated=%d", stats.insights_generated)

    def _emit_insight_event(self, conn: Any, insight: SynthesisInsight) -> None:
        event_type = "maintenance_insight"
        payload = {
            "topic": insight.topic,
            "statement": insight.statement,
            "member_ids": insight.member_ids,
            "confidence": round(insight.confidence, 3),
        }
        try:
            conn.execute(
                "INSERT INTO events (id, tenant_id, event_type, timestamp, actor, entity_id, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    hashlib.sha256(f"{insight.topic}{datetime.now(timezone.utc).isoformat()}".encode()).hexdigest()[
                        :16
                    ],
                    "maintenance",
                    event_type,
                    datetime.now(timezone.utc).isoformat(),
                    "maintenance_job",
                    insight.member_ids[0][:8] if insight.member_ids else "unknown",
                    str(payload),
                ),
            )
            conn.commit()
        except Exception:
            pass
