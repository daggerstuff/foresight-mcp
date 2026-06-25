"""Evaluation harness for Memory Maintenance (PIX-3952).

Measures payload reduction and correctness of the MemoryMaintenanceJob
across all four modes: consolidate, contradict, archive_stale, synthesize.

Creates an isolated database with crafted fixture memories engineered to
trigger each maintenance mode, runs the job per-mode and combined,
then reports pre/post metrics.

Usage:
    from foresight_mcp.maintenance_eval import MaintenanceEvalHarness

    harness = MaintenanceEvalHarness()
    harness.seed_fixtures()
    report = harness.run_all()
    print(report.format_text())

Or via CLI:
    python -m foresight_mcp.maintenance_eval
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .memory_maintenance import (
    MAX_BATCH_SIZE,
    MAX_RUNTIME_SECONDS,
    STALE_IMPORTANCE_THRESHOLD,
    STALE_STRENGTH_THRESHOLD,
    MaintenanceConfig,
    MaintenanceStats,
    MemoryMaintenanceJob,
)

logger = logging.getLogger("maintenance_eval")

# =============================================================================
# Fixture definitions — memories crafted to trigger each maintenance mode
# =============================================================================

FIXTURE_DUPLICATES: list[dict[str, Any]] = [
    # High-overlap pair (> 0.70) → triggers auto_consolidate
    {
        "id": "dup_high_a",
        "content": (
            "User mentioned that they prefer the calm and quiet environment "
            "of the library for studying because it helps them focus better "
            "and retain more information without distractions."
        ),
        "scope": "session",
        "retention": "short_term",
        "category": "fact",
        "importance": 0.6,
        "strength_trend": "stable",
    },
    {
        "id": "dup_high_b",
        "content": (
            "User mentioned that they prefer the calm and quiet environment "
            "of the library for studying because it helps them focus better "
            "and retain more information efficiently without any disturbances."
        ),
        "scope": "session",
        "retention": "short_term",
        "category": "fact",
        "importance": 0.5,
        "strength_trend": "stable",
    },
    # Marginal-overlap pair (> 0.30) → triggers flag_review
    {
        "id": "dup_marg_a",
        "content": (
            "The user noted that meditation in the morning sets a productive "
            "tone for the rest of the day, reducing stress levels significantly."
        ),
        "scope": "session",
        "retention": "short_term",
        "category": "fact",
        "importance": 0.5,
        "strength_trend": "stable",
    },
    {
        "id": "dup_marg_b",
        "content": (
            "The user finds that morning meditation helps them stay calm "
            "and productive throughout the day, with noticeable reductions "
            "in overall stress."
        ),
        "scope": "session",
        "retention": "short_term",
        "category": "fact",
        "importance": 0.4,
        "strength_trend": "stable",
    },
]

FIXTURE_CONTRADICTIONS: list[dict[str, Any]] = [
    {
        "id": "contra_love",
        "content": (
            "I really love working with React because the component model makes it easy to build reusable UI elements."
        ),
        "scope": "session",
        "retention": "short_term",
        "category": "fact",
        "importance": 0.7,
        "strength_trend": "stable",
    },
    {
        "id": "contra_hate",
        "content": (
            "I really hate working with React because the tooling and build "
            "configuration is overly complex and fragile."
        ),
        "scope": "session",
        "retention": "short_term",
        "category": "fact",
        "importance": 0.6,
        "strength_trend": "stable",
    },
]

FIXTURE_STALE: list[dict[str, Any]] = [
    {
        "id": "stale_low_imp_a",
        "content": (
            "The office wifi password was set to guest2020 last year and "
            "the IT team said they would rotate it quarterly."
        ),
        "scope": "fact",
        "retention": "long_term",
        "category": "fact",
        "importance": 0.08,  # < stale_importance_threshold (0.1)
        "strength_trend": "weakening",
    },
    {
        "id": "stale_low_imp_b",
        "content": ("Mentioned that the break room coffee machine was making a strange noise and needed maintenance."),
        "scope": "session",
        "retention": "short_term",
        "category": "fact",
        "importance": 0.05,  # < stale_importance_threshold (0.1)
        "strength_trend": "weakening",
    },
]

FIXTURE_SYNTHESIZE: list[dict[str, Any]] = [
    {
        "id": "synth_qa_a",
        "content": (
            "The user wants to improve code quality by adding more unit tests "
            "and adopting test-driven development practices in their projects."
        ),
        "scope": "fact",
        "retention": "long_term",
        "category": "fact",
        "importance": 0.7,
        "strength_trend": "stable",
    },
    {
        "id": "synth_qa_b",
        "content": (
            "User mentioned that they plan to set up continuous integration "
            "with automated test runs to ensure code quality is maintained "
            "across all team members."
        ),
        "scope": "session",
        "retention": "short_term",
        "category": "fact",
        "importance": 0.6,
        "strength_trend": "stable",
    },
    {
        "id": "synth_qa_c",
        "content": (
            "During the last retrospective the user agreed to take ownership "
            "of improving the test coverage and adding automated quality checks."
        ),
        "scope": "session",
        "retention": "short_term",
        "category": "fact",
        "importance": 0.5,
        "strength_trend": "stable",
    },
]

# All fixtures combined for seeding
ALL_FIXTURES: list[dict[str, Any]] = FIXTURE_DUPLICATES + FIXTURE_CONTRADICTIONS + FIXTURE_STALE + FIXTURE_SYNTHESIZE

FIXTURE_USER_ID = "_eval_maint_user_"
FIXTURE_TENANT_ID = "_eval_maint_tenant_"


# =============================================================================
# Scenario definitions
# =============================================================================


@dataclass
class MaintenanceScenario:
    """Definition of a single maintenance evaluation scenario."""

    id: str
    description: str
    modes: list[str]
    expected_metric: str  # Key from MaintenanceStats.to_dict()
    expected_min: int  # Minimum value for metric to pass
    pass_condition: str  # Human-readable condition


SCENARIOS: list[MaintenanceScenario] = [
    MaintenanceScenario(
        id="S1_consolidate",
        description="Consolidate mode finds and processes near-duplicate memories",
        modes=["consolidate"],
        expected_metric="duplicates_found",
        expected_min=4,
        pass_condition="duplicates_found >= 4 (two high-overlap + two marginal pairs)",
    ),
    MaintenanceScenario(
        id="S2_contradict",
        description="Contradict mode detects sentiment-conflict pairs",
        modes=["contradict"],
        expected_metric="contradictions_flagged_review",
        expected_min=1,
        pass_condition="contradictions_flagged_review >= 1 (love/hate React)",
    ),
    MaintenanceScenario(
        id="S3_archive_stale",
        description="Archive-stale mode archives low-importance memories",
        modes=["archive_stale"],
        expected_metric="stale_archived",
        expected_min=2,
        pass_condition="stale_archived >= 2 (two low-importance memories)",
    ),
    MaintenanceScenario(
        id="S4_synthesize",
        description="Synthesize mode generates insights from related memories",
        modes=["synthesize"],
        expected_metric="insights_generated",
        expected_min=1,
        pass_condition="insights_generated >= 1 (quality/test topic across 3 memories)",
    ),
    MaintenanceScenario(
        id="S5_all_modes",
        description="All modes combined produce non-zero stats across all metrics",
        modes=["consolidate", "contradict", "archive_stale", "synthesize"],
        expected_metric="modes_run",
        expected_min=4,
        pass_condition="All 4 modes run without errors and produce expected outputs",
    ),
]


# =============================================================================
# Result types
# =============================================================================


@dataclass
class MaintenanceScenarioResult:
    """Result of running a single maintenance scenario."""

    scenario_id: str
    modes: list[str]
    passed: bool
    stats: dict[str, Any]  # MaintenanceStats.to_dict() output
    pre_memory_count: int
    post_memory_count: int
    memory_reduction: int
    latency_ms: float
    error: str | None = None


@dataclass
class MaintenanceEvalReport:
    """Complete maintenance evaluation report."""

    timestamp: str
    scenarios: list[MaintenanceScenarioResult]
    summary: dict[str, Any]
    harness_version: str = "1.0.0"

    def format_text(self) -> str:
        """Format as human-readable text report."""
        lines: list[str] = [
            "=" * 72,
            "  MAINTENANCE EVALUATION HARNESS",
            f"  Version: {self.harness_version}",
            f"  Time:    {self.timestamp}",
            "=" * 72,
            "",
            f"Summary: {self.summary['passed']}/{self.summary['total']} scenarios passed",
            f"  Pass rate: {self.summary['pass_rate_pct']:.1f}%",
            "",
        ]

        if self.summary.get("total_memory_reduction") is not None:
            lines.append(
                f"Total memory reduction: {self.summary['total_memory_reduction']} "
                f"(pre={self.summary['total_pre_count']}, "
                f"post={self.summary['total_post_count']})"
            )
        if self.summary.get("total_events_logged") is not None:
            lines.append(f"Total maintenance events logged: {self.summary['total_events_logged']}")
        if self.summary.get("avg_latency_ms") is not None:
            lines.append(f"Avg maintenance latency: {self.summary['avg_latency_ms']:.2f} ms")
        lines.append("")

        for sr in self.scenarios:
            status = "✓" if sr.passed else "✗"
            lines.append(f"  [{status}] {sr.scenario_id}: {', '.join(sr.modes)}")
            lines.append(
                f"       Memories: {sr.pre_memory_count} → {sr.post_memory_count} "
                f"({sr.memory_reduction:+d}) | "
                f"Latency: {sr.latency_ms:.1f}ms"
            )
            # Show key stats
            stat_items = []
            for key in (
                "duplicates_found",
                "duplicates_auto_consolidated",
                "duplicates_flagged_review",
                "contradictions_found",
                "contradictions_flagged_review",
                "stale_found",
                "stale_archived",
                "insights_generated",
                "errors",
            ):
                val = sr.stats.get(key)
                if val is not None and val != 0:
                    stat_items.append(f"{key}: {val}")
            if stat_items:
                lines.append(f"       Stats: {', '.join(stat_items)}")
            if sr.error:
                lines.append(f"       Error: {sr.error}")
            lines.append("")

        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "harness_version": self.harness_version,
            "timestamp": self.timestamp,
            "summary": self.summary,
            "scenarios": [
                {
                    "scenario_id": s.scenario_id,
                    "modes": s.modes,
                    "passed": s.passed,
                    "stats": s.stats,
                    "pre_memory_count": s.pre_memory_count,
                    "post_memory_count": s.post_memory_count,
                    "memory_reduction": s.memory_reduction,
                    "latency_ms": s.latency_ms,
                    "error": s.error,
                }
                for s in self.scenarios
            ],
        }

    def total_pre_count(self) -> int:
        return sum(s.pre_memory_count for s in self.scenarios) // max(len(self.scenarios), 1)

    def total_post_count(self) -> int:
        return sum(s.post_memory_count for s in self.scenarios) // max(len(self.scenarios), 1)


# =============================================================================
# Harness
# =============================================================================


class MaintenanceEvalHarness:
    """Evaluation harness for MemoryMaintenanceJob (PIX-3952).

    Creates an isolated database, seeds fixture memories, patches the global
    DB_PATH so MemoryMaintenanceJob sees the fixture data, runs each scenario
    against the corresponding maintenance mode(s), and collects metrics.

    Usage:
        harness = MaintenanceEvalHarness()
        harness.seed_fixtures()
        report = harness.run_all()
        logger.info(report.format_text())
    """

    def __init__(
        self,
        db_path: str | None = None,
    ) -> None:
        _user_provided = db_path is not None and db_path != ":memory:"
        if db_path is None or db_path == ":memory:":
            fd, resolved = tempfile.mkstemp(suffix=".db", prefix="foresight_maint_eval_")
            os.close(fd)
            db_path = resolved
        self._db_path: str = db_path
        self._user_provided_db: bool = _user_provided

        self._conn: sqlite3.Connection | None = None
        self._monkeypatches: list[tuple[Any, str, Any]] = []

    @property
    def db_path(self) -> str:
        return self._db_path

    # ------------------------------------------------------------------
    # Database setup
    # ------------------------------------------------------------------

    def _get_connection(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        self._conn = conn
        return conn

    def _apply_patches(self) -> None:
        """Patch DB_PATH so MemoryMaintenanceJob and init_db see our DB.

        Mirrors the pattern in eval_harness.py and tests/test_server.py.
        """
        import foresight_mcp.config as config_module
        import foresight_mcp.connection_pool as conn_pool_module
        from foresight_mcp.connection_pool import reset_pool
        from foresight_mcp.server import init_db

        reset_pool()

        for mod in (config_module, conn_pool_module):
            orig = mod.DB_PATH  # type: ignore[attr-defined]
            mod.DB_PATH = self.db_path  # type: ignore[attr-defined]
            self._monkeypatches.append((mod, "DB_PATH", orig))

        # Ensure schema exists
        init_db()

    def _restore_patches(self) -> None:
        from foresight_mcp.connection_pool import reset_pool

        reset_pool()
        for module, attr, orig in self._monkeypatches:
            setattr(module, attr, orig)
        self._monkeypatches.clear()

    def _count_memories(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM memories WHERE user_id = ? AND tenant_id = ?",
            (FIXTURE_USER_ID, FIXTURE_TENANT_ID),
        ).fetchone()
        return row["cnt"] if row else 0

    def _count_events(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM events WHERE event_type LIKE 'maintenance%'").fetchone()
        return row["cnt"] if row else 0

    def seed_fixtures(self) -> int:
        """Create core schema and seed fixture memories. Returns count inserted."""
        self._apply_patches()
        conn = self._get_connection()
        now_iso = datetime.now(timezone.utc).isoformat()

        count = 0
        for mem in ALL_FIXTURES:
            mem_id = mem["id"]
            content = mem["content"]
            content_hash = hashlib.sha256(content.encode()).hexdigest()
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO memories
                    (id, content, content_hash, tenant_id, user_id, scope, retention,
                     category, bank_id, created_at, updated_at, tags, emotional_context,
                     metrics, is_ghost, synthesized_from, version,
                     importance, activation_count, strength_trend)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        mem_id,
                        content,
                        content_hash,
                        FIXTURE_TENANT_ID,
                        FIXTURE_USER_ID,
                        mem.get("scope", "session"),
                        mem.get("retention", "short_term"),
                        mem.get("category", "fact"),
                        "default",
                        now_iso,
                        now_iso,
                        "[]",
                        "{}",
                        "{}",
                        0,
                        "[]",
                        1,
                        mem.get("importance", 0.5),
                        1,
                        mem.get("strength_trend", "stable"),
                    ),
                )
                if conn.total_changes > 0:
                    count += 1
            except sqlite3.IntegrityError:
                pass

        conn.commit()
        logger.info("Seeded %d fixture memories (%s)", count, self.db_path)
        return count

    # ------------------------------------------------------------------
    # Scenario execution
    # ------------------------------------------------------------------

    def run_scenario(self, scenario: MaintenanceScenario) -> MaintenanceScenarioResult:
        try:
            return self._do_run_scenario(scenario)
        except Exception as exc:
            logger.exception("Maintenance scenario %s failed", scenario.id)
            return MaintenanceScenarioResult(
                scenario_id=scenario.id,
                modes=scenario.modes,
                passed=False,
                stats={},
                pre_memory_count=0,
                post_memory_count=0,
                memory_reduction=0,
                latency_ms=0.0,
                error=str(exc),
            )

    def _do_run_scenario(self, scenario: MaintenanceScenario) -> MaintenanceScenarioResult:
        conn = self._get_connection()
        pre_count = self._count_memories(conn)

        # Build config for this scenario
        config = MaintenanceConfig(
            tenant_id=FIXTURE_TENANT_ID,
            user_id=FIXTURE_USER_ID,
            modes=scenario.modes,
            # Use most aggressive thresholds to guarantee triggers
            duplicate_threshold=0.2,
            consolidation_overlap_high=0.30,
            consolidation_overlap_marginal=0.15,
            stale_strength_threshold=STALE_STRENGTH_THRESHOLD,
            stale_importance_threshold=STALE_IMPORTANCE_THRESHOLD,
            batch_size=MAX_BATCH_SIZE,
            max_runtime_seconds=MAX_RUNTIME_SECONDS,
        )

        job = MemoryMaintenanceJob(db_path=self.db_path)

        t0 = time.perf_counter()
        stats: MaintenanceStats = job.run(config)
        latency_ms = (time.perf_counter() - t0) * 1000

        post_count = self._count_memories(conn)
        memory_reduction = pre_count - post_count
        stats_dict = stats.to_dict()

        # Determine pass/fail
        if scenario.id == "S5_all_modes":
            # All 4 modes ran and produced results
            passed = (
                len(stats.modes_run) >= 4
                and stats.duplicates_found > 0
                and stats.contradictions_found > 0
                and (stats.stale_archived > 0 or stats.stale_found > 0)
                and stats.insights_generated > 0
                and len(stats.errors) == 0
            )
        else:
            metric_value = stats_dict.get(scenario.expected_metric, 0)
            passed = metric_value >= scenario.expected_min

        return MaintenanceScenarioResult(
            scenario_id=scenario.id,
            modes=scenario.modes,
            passed=passed,
            stats=stats_dict,
            pre_memory_count=pre_count,
            post_memory_count=post_count,
            memory_reduction=memory_reduction,
            latency_ms=latency_ms,
        )

    def run_all(self) -> MaintenanceEvalReport:
        results: list[MaintenanceScenarioResult] = []
        total_latency = 0.0
        total_reduction = 0
        scenario_count = 0

        for scenario in SCENARIOS:
            result = self.run_scenario(scenario)
            results.append(result)
            total_latency += result.latency_ms
            total_reduction += result.memory_reduction
            scenario_count += 1

        passed_count = sum(1 for r in results if r.passed)
        total_count = len(results)

        # Count total events logged across all scenarios
        try:
            conn = self._get_connection()
            total_events = self._count_events(conn)
        except Exception:
            total_events = 0

        summary: dict[str, Any] = {
            "total": total_count,
            "passed": passed_count,
            "pass_rate_pct": (passed_count / total_count * 100) if total_count > 0 else 0.0,
            "total_memory_reduction": total_reduction,
            "total_pre_count": results[0].pre_memory_count if results else 0,
            "total_post_count": results[-1].post_memory_count if results else 0,
            "total_events_logged": total_events,
            "avg_latency_ms": total_latency / scenario_count if scenario_count > 0 else 0,
        }

        return MaintenanceEvalReport(
            timestamp=datetime.now(timezone.utc).isoformat(),
            scenarios=results,
            summary=summary,
        )

    def close(self) -> None:
        """Close DB, restore patches, clean up temp file."""
        self._restore_patches()
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if not self._user_provided_db:
            try:
                os.unlink(self._db_path)
            except OSError:
                pass


# =============================================================================
# CLI entry point
# =============================================================================


def run_maintenance_eval(
    db_path: str | None = None,
    report_path: str | None = None,
    json_output: bool = False,
) -> MaintenanceEvalReport:
    """Run the full maintenance evaluation harness.

    Args:
        db_path: Path to temp database for fixtures. None = auto tempfile.
        report_path: If set, path to write the JSON report.
        json_output: If True, print JSON report to stdout instead of text.

    Returns:
        MaintenanceEvalReport instance.
    """
    harness = MaintenanceEvalHarness(db_path=db_path)
    try:
        count = harness.seed_fixtures()
        logger.info("Seeded %d fixture memories", count)
        report = harness.run_all()

        if report_path:
            with open(report_path, "w") as f:
                json.dump(report.to_dict(), f, indent=2)
            logger.info("Report written to %s", report_path)

        if json_output:
            logger.info(json.dumps(report.to_dict(), indent=2))
        else:
            logger.info(report.format_text())

        return report
    finally:
        harness.close()


def main() -> None:
    """CLI entry point. Run with: python -m foresight_mcp.maintenance_eval"""
    import argparse

    parser = argparse.ArgumentParser(description="Foresight Maintenance Evaluation (PIX-3952)")
    parser.add_argument(
        "--db-path",
        default=None,
        help="Path to temporary database (default: auto tempfile)",
    )
    parser.add_argument(
        "--report",
        default=None,
        help="Path to write JSON report (default: no file output)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output report as JSON",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)
    run_maintenance_eval(
        db_path=args.db_path,
        report_path=args.report,
        json_output=args.json,
    )


if __name__ == "__main__":
    main()
