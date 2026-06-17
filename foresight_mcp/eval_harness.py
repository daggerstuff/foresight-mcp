"""Evaluation harness for memory retrieval and injection payload quality (PIX-3953).

Provides a reproducible fixture suite, scenario-based evaluation of
inject_context and get_relevant_memories, and detailed metrics on
payload size, latency, retrieval quality, and safety (PII leakage).

Usage:
    from foresight_mcp.eval_harness import EvalHarness, EvalReport

    harness = EvalHarness(db_path="/tmp/eval_fixtures.db")
    harness.seed_fixtures()
    report = harness.run_all()
    print(report.format_text())

    # Also available as JSON for CI/baseline comparison:
    import json; print(json.dumps(report.to_dict(), indent=2))

Or via CLI:
    python -m foresight_mcp.eval_harness
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("foresight_eval")

# =============================================================================
# PII / safety scanner
# =============================================================================

# Patterns that should NEVER appear in injection output
_PII_PATTERNS: list[tuple[str, str]] = [
    ("email", r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
    ("phone", r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b"),
    ("ssn", r"\b\d{3}-\d{2}-\d{4}\b"),
    ("api_key", r"(?i)(?:api[_-]?key|apikey|secret|token)\s*[=:]\s*['\"]?[a-zA-Z0-9_\-]{16,}"),
    ("ip_address", r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
    ("credit_card", r"\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b"),
]


def scan_for_pii(text: str) -> list[dict[str, Any]]:
    """Scan text for PII/secret patterns. Returns list of findings."""
    findings: list[dict[str, Any]] = []
    for label, pattern in _PII_PATTERNS:
        for match in re.finditer(pattern, text):
            findings.append(
                {
                    "type": label,
                    "match": match.group()[:20],  # Truncate for safety
                    "position": match.start(),
                }
            )
    return findings


# =============================================================================
# Fixture definitions
# =============================================================================

FIXTURE_MEMORIES: list[dict[str, Any]] = [
    # --- S1: Preferences ---
    {
        "id": "pref_concise_typescript",
        "content": "User prefers concise TypeScript type definitions with explicit generics rather than inferred types. Always use interface over type alias for object shapes.",
        "scope": "trait",
        "retention": "long_term",
        "category": "preference",
        "importance": 0.85,
        "strength_trend": "stable",
    },
    {
        "id": "pref_2space_indent",
        "content": "Project uses 2-space indentation for all TypeScript/JavaScript files. Configure eslint and prettier accordingly.",
        "scope": "trait",
        "retention": "long_term",
        "category": "preference",
        "importance": 0.75,
        "strength_trend": "stable",
    },
    # --- S2: Pending items ---
    {
        "id": "pending_dashboard",
        "content": "TODO: Build the dashboard overview component showing memory health metrics. Requires creating stat cards for total memories, by-scope breakdown, and recent activity.",
        "scope": "arc",
        "retention": "short_term",
        "category": "plan",
        "importance": 0.9,
        "strength_trend": "strengthening",
    },
    {
        "id": "pending_auth",
        "content": "TODO: Implement OAuth2 authentication flow for the Foresight API. Research refresh token rotation strategy first. Blocked on security review.",
        "scope": "arc",
        "retention": "short_term",
        "category": "plan",
        "importance": 0.8,
        "strength_trend": "strengthening",
    },
    {
        "id": "pending_refactor",
        "content": "TODO: Refactor the hybrid retriever to use composable signal scorers instead of monolithic search method.",
        "scope": "arc",
        "retention": "short_term",
        "category": "plan",
        "importance": 0.6,
        "strength_trend": "stable",
    },
    # --- S3: Stale vs current facts ---
    {
        "id": "current_auth_approach",
        "content": "Current authentication uses JWT with RS256 signatures. Tokens expire after 15 minutes and refresh tokens are stored in HTTP-only cookies. Session management is handled by the auth service.",
        "scope": "fact",
        "retention": "long_term",
        "category": "fact",
        "importance": 0.8,
        "strength_trend": "strengthening",
        "created_at_offset_hours": -2,
    },
    {
        "id": "stale_auth_approach",
        "content": "Old authentication used API keys passed in headers. Every request required a valid HMAC signature computed from the shared secret. Deprecated in favor of JWT-based auth.",
        "scope": "fact",
        "retention": "long_term",
        "category": "fact",
        "importance": 0.3,
        "strength_trend": "weakening",
        "created_at_offset_hours": -720,  # ~30 days ago
    },
    # --- S4: Entity/file references ---
    {
        "id": "entity_db_config",
        "content": "Database configuration is in `config/database.yaml`. Connection string uses environment variable DATABASE_URL with postgres:// scheme. Pool size defaults to 10 connections.",
        "scope": "fact",
        "retention": "long_term",
        "category": "fact",
        "importance": 0.7,
        "strength_trend": "stable",
    },
    {
        "id": "entity_deploy_script",
        "content": "Deployment script is at `scripts/deploy.sh`. Uses rsync for static assets and systemd service restart for the API server. Staging and production environments are configured via environment-specific YAML files.",
        "scope": "fact",
        "retention": "long_term",
        "category": "fact",
        "importance": 0.65,
        "strength_trend": "stable",
    },
    # --- S5: Session / discussion memories ---
    {
        "id": "session_deploy",
        "content": "Discussed deployment pipeline improvements during standup. Agreed to migrate from rsync to Docker-based deployments. Need to set up GitHub Actions workflow for CI/CD.",
        "scope": "session",
        "retention": "short_term",
        "category": "fact",
        "importance": 0.7,
        "strength_trend": "stable",
        "created_at_offset_hours": -24,
    },
    {
        "id": "session_code_review",
        "content": "Reviewed PR #1234: hybrid retriever refactor. Main feedback was about test coverage — need more edge case tests for empty queries and tenant isolation edge cases.",
        "scope": "session",
        "retention": "short_term",
        "category": "fact",
        "importance": 0.6,
        "strength_trend": "stable",
        "created_at_offset_hours": -48,
    },
    # --- Distractor / low-relevance memories ---
    {
        "id": "distractor_weather",
        "content": "Mentioned that the weather has been unusually warm for June. Not relevant to any project work.",
        "scope": "session",
        "retention": "short_term",
        "category": "fact",
        "importance": 0.1,
        "strength_trend": "weakening",
        "created_at_offset_hours": -72,
    },
    {
        "id": "distractor_lunch",
        "content": "Had lunch at the new ramen place downtown. The broth was excellent but the wait was too long.",
        "scope": "session",
        "retention": "short_term",
        "category": "fact",
        "importance": 0.05,
        "strength_trend": "weakening",
        "created_at_offset_hours": -96,
    },
]


# =============================================================================
# Scenario definitions
# =============================================================================


@dataclass
class EvalScenario:
    """Definition of a single evaluation scenario."""

    id: str
    description: str
    query: str
    expected_memory_ids: set[str]
    pass_condition: str  # Human-readable condition
    min_expected: int = 1  # Minimum number of expected memories that should be found


SCENARIOS: list[EvalScenario] = [
    EvalScenario(
        id="S1_preference",
        description="Retrieval finds coding-style preference memory",
        query="What coding style does the user prefer for TypeScript?",
        expected_memory_ids={"pref_concise_typescript", "pref_2space_indent"},
        pass_condition="Top-3 results include at least one preference memory",
        min_expected=1,
    ),
    EvalScenario(
        id="S2_pending",
        description="Retrieval finds pending project items",
        query="What's the current project status and what needs to be done?",
        expected_memory_ids={"pending_dashboard", "pending_auth", "pending_refactor"},
        pass_condition="At least 2 of 3 pending items appear in results",
        min_expected=2,
    ),
    EvalScenario(
        id="S3_stale_vs_current",
        description="Current facts rank higher than stale facts for same topic",
        query="How does authentication work in this project?",
        expected_memory_ids={"current_auth_approach", "stale_auth_approach"},
        pass_condition="current_auth_approach ranks higher than stale_auth_approach",
        min_expected=2,
    ),
    EvalScenario(
        id="S4_entity_ref",
        description="Entity/file reference retrieval by description",
        query="Where is the database configuration file located?",
        expected_memory_ids={"entity_db_config"},
        pass_condition="entity_db_config appears in results",
        min_expected=1,
    ),
    EvalScenario(
        id="S5_session",
        description="Session memory retrieval from recent discussions",
        query="What did we discuss about deployment in the last standup?",
        expected_memory_ids={"session_deploy"},
        pass_condition="session_deploy appears in results",
        min_expected=1,
    ),
]


# =============================================================================
# Result types
# =============================================================================


@dataclass
class ScenarioResult:
    """Result of running a single scenario."""

    scenario_id: str
    query: str
    passed: bool
    metrics: dict[str, Any]
    found_memory_ids: list[str]
    missing_expected: list[str]
    injection_payload_size: int
    latency_ms: float
    signal_counts: dict[str, int]
    fast_path: str | None
    pii_findings: list[dict[str, Any]]
    error: str | None = None


@dataclass
class EvalReport:
    """Complete evaluation report."""

    timestamp: str
    scenarios: list[ScenarioResult]
    summary: dict[str, Any]
    harness_version: str = "1.0.0"

    def format_text(self) -> str:
        """Format as human-readable text report."""
        lines: list[str] = [
            "=" * 72,
            "  FORESIGHT EVALUATION HARNESS",
            f"  Version: {self.harness_version}",
            f"  Time:    {self.timestamp}",
            "=" * 72,
            "",
            f"Summary: {self.summary['passed']}/{self.summary['total']} scenarios passed",
            f"  Pass rate: {self.summary['pass_rate_pct']:.1f}%",
            "",
        ]

        if self.summary.get("avg_payload_size") is not None:
            lines.append(f"Avg injection payload: {self.summary['avg_payload_size']:.0f} chars")
        if self.summary.get("avg_latency_ms") is not None:
            lines.append(f"Avg retrieval latency: {self.summary['avg_latency_ms']:.2f} ms")
        if self.summary.get("pii_findings_total", 0) > 0:
            lines.append(f"⚠ PII/secret findings: {self.summary['pii_findings_total']}")
        else:
            lines.append("PII/secret findings: 0 ✓")
        lines.append("")

        for sr in self.scenarios:
            status = "✓" if sr.passed else "✗"
            lines.append(f"  [{status}] {sr.scenario_id}: {sr.query}")
            lines.append(
                f"       Payload: {sr.injection_payload_size} chars | "
                f"Latency: {sr.latency_ms:.1f}ms | "
                f"Found: {len(sr.found_memory_ids)} memories"
            )
            if sr.missing_expected:
                lines.append(f"       Missing: {', '.join(sr.missing_expected)}")
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
                    "query": s.query,
                    "passed": s.passed,
                    "metrics": s.metrics,
                    "found_memory_ids": s.found_memory_ids,
                    "missing_expected": s.missing_expected,
                    "injection_payload_size": s.injection_payload_size,
                    "latency_ms": s.latency_ms,
                    "signal_counts": s.signal_counts,
                    "fast_path": s.fast_path,
                    "pii_findings": s.pii_findings,
                    "error": s.error,
                }
                for s in self.scenarios
            ],
        }


# =============================================================================
# Harness
# =============================================================================


class EvalHarness:
    """Evaluation harness for memory retrieval and injection quality (PIX-3953).

    Creates an isolated database, seeds fixture memories, patches the global
    DB_PATH so inject_context() and get_relevant_memories() use the fixture
    data, runs scenarios, and collects metrics.

    Usage:
        harness = EvalHarness()
        harness.seed_fixtures()
        report = harness.run_all()
        print(report.format_text())

    Args:
        db_path: Path to the temporary database file.
            Uses tempfile if None.
        user_id: User ID for fixture data and queries.
        tenant_id: Tenant ID for fixture data and queries.
    """

    def __init__(
        self,
        db_path: str | None = None,
        user_id: str = "_eval_user_",
        tenant_id: str = "_eval_tenant_",
    ) -> None:
        # Connection pool requires a real file path — resolve sentinels to tempfile
        _user_provided = db_path is not None and db_path != ":memory:"
        if db_path is None or db_path == ":memory:":
            fd, resolved = tempfile.mkstemp(suffix=".db", prefix="foresight_eval_")
            os.close(fd)
            db_path = resolved
        self._db_path: str = db_path
        self._user_provided_db: bool = _user_provided
        self.user_id = user_id
        self.tenant_id = tenant_id

        self._conn: sqlite3.Connection | None = None
        self._monkeypatches: list[tuple[Any, str, Any]] = []  # (module, attr, original_value)

    @property
    def db_path(self) -> str:
        return self._db_path

    # ------------------------------------------------------------------
    # Database setup
    # ------------------------------------------------------------------

    def _get_connection(self) -> sqlite3.Connection:
        """Get or create the SQLite connection."""
        if self._conn is not None:
            return self._conn
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        self._conn = conn
        return conn

    def _apply_patches(self) -> None:
        """Patch DB_PATH and tenant context so server.py functions see our DB.

        Mirrors the pattern in tests/test_server.py::setup_test_db.
        Note: hybrid_retriever has its own `from .config import DB_PATH` local
        binding, so we must patch that module's attribute directly too.
        """
        import foresight_mcp.config as config_module
        import foresight_mcp.connection_pool as conn_pool_module
        import foresight_mcp.hybrid_retriever as hr_module
        from foresight_mcp.connection_pool import reset_pool
        from foresight_mcp.server import init_db
        from foresight_mcp.hybrid_retriever import reset_hybrid_retriever
        from foresight_mcp.tenant_context import set_current_user_id, set_current_account_id

        # Reset singletons
        reset_pool()
        reset_hybrid_retriever()

        # Patch config DB_PATH in each module that holds a local binding
        for mod in (config_module, conn_pool_module, hr_module):
            orig = mod.DB_PATH
            mod.DB_PATH = self.db_path
            self._monkeypatches.append((mod, "DB_PATH", orig))

        # Set tenant context
        set_current_user_id(self.user_id)
        set_current_account_id(self.tenant_id)

        # Ensure schema exists
        init_db()

    def _restore_patches(self) -> None:
        """Restore all monkeypatched values."""
        import foresight_mcp.hybrid_retriever as hr_module

        hr_module.reset_hybrid_retriever()
        from foresight_mcp.connection_pool import reset_pool

        reset_pool()
        for module, attr, orig in self._monkeypatches:
            setattr(module, attr, orig)
        self._monkeypatches.clear()
        from foresight_mcp.tenant_context import reset_tenant_context

        reset_tenant_context()

    def seed_fixtures(self) -> int:
        """Seed fixture memories into the database. Returns count of memories inserted."""
        self._apply_patches()  # init_db() creates core schema via the patched connection pool
        conn = self._get_connection()
        now = datetime.now(timezone.utc)

        count = 0
        for mem in FIXTURE_MEMORIES:
            mem_id = mem["id"]
            content = mem["content"]
            content_hash = hashlib.sha256(content.encode()).hexdigest()

            created_at = now
            offset = mem.get("created_at_offset_hours")
            if offset is not None:
                created_at = datetime.now(timezone.utc).__class__.fromtimestamp(
                    now.timestamp() + (offset or 0) * 3600,
                    tz=timezone.utc,
                )
            created_at_iso = created_at.isoformat()
            updated_at_iso = created_at.isoformat()

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
                        self.tenant_id,
                        self.user_id,
                        mem.get("scope", "session"),
                        mem.get("retention", "short_term"),
                        mem.get("category", "fact"),
                        "default",
                        created_at_iso,
                        updated_at_iso,
                        mem.get("tags", "[]"),
                        mem.get("emotional_context", "{}"),
                        mem.get("metrics", "{}"),
                        mem.get("is_ghost", 0),
                        mem.get("synthesized_from", "[]"),
                        mem.get("version", 1),
                        mem.get("importance", 0.5),
                        mem.get("activation_count", 1),
                        mem.get("strength_trend", "stable"),
                    ),
                )
                if conn.total_changes > 0 or count == 0:  # Best-effort count
                    count += 1
            except sqlite3.IntegrityError:
                pass

        conn.commit()

        # Build the entity_entity_links and memory_entity_links for graph search
        self._seed_entities(conn)

        logger.info("Seeded %d fixture memories (%s)", count, self.db_path)
        return count

    def _seed_entities(self, conn: sqlite3.Connection) -> None:
        """Create entity records so graph search can link memory IDs to entity names."""
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memory_entities (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                name TEXT,
                entity_type TEXT,
                description TEXT,
                properties TEXT DEFAULT '{}',
                confidence REAL DEFAULT 1.0,
                user_id TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS entity_relationships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                source_entity_id TEXT,
                target_entity_id TEXT,
                relationship_type TEXT,
                confidence REAL DEFAULT 1.0,
                decay_factor REAL DEFAULT 1.0,
                last_accessed TEXT DEFAULT CURRENT_TIMESTAMP,
                metadata TEXT DEFAULT '{}',
                user_id TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS memory_entity_links (
                memory_id TEXT,
                entity_id TEXT,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                user_id TEXT,
                relevance_score REAL DEFAULT 1.0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (memory_id, entity_id)
            );
        """)

        # Insert entities for key fixture memories
        entities = [
            ("ent_ts", "TypeScript", "language", 0.95),
            ("ent_auth", "authentication", "concept", 0.9),
            ("ent_db", "database", "concept", 0.9),
            ("ent_deploy", "deployment", "concept", 0.85),
            ("ent_config", "configuration", "concept", 0.8),
        ]
        for eid, name, etype, conf in entities:
            conn.execute(
                "INSERT OR IGNORE INTO memory_entities (id, tenant_id, name, entity_type, confidence, user_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (eid, self.tenant_id, name, etype, conf, self.user_id),
            )

        # Link memories to entities
        links = [
            ("pref_concise_typescript", "ent_ts"),
            ("pref_2space_indent", "ent_ts"),
            ("current_auth_approach", "ent_auth"),
            ("stale_auth_approach", "ent_auth"),
            ("entity_db_config", "ent_db"),
            ("entity_db_config", "ent_config"),
            ("entity_deploy_script", "ent_deploy"),
            ("entity_deploy_script", "ent_config"),
            ("session_deploy", "ent_deploy"),
        ]
        for mid, eid in links:
            conn.execute(
                "INSERT OR IGNORE INTO memory_entity_links (memory_id, entity_id, tenant_id, user_id) "
                "VALUES (?, ?, ?, ?)",
                (mid, eid, self.tenant_id, self.user_id),
            )

        # Create entity relationships
        conn.execute(
            "INSERT OR IGNORE INTO entity_relationships (source_entity_id, target_entity_id, user_id, tenant_id, confidence, relationship_type) "
            "VALUES ('ent_db', 'ent_config', ?, ?, 0.9, 'depends_on')",
            (self.user_id, self.tenant_id),
        )
        conn.execute(
            "INSERT OR IGNORE INTO entity_relationships (source_entity_id, target_entity_id, user_id, tenant_id, confidence, relationship_type) "
            "VALUES ('ent_deploy', 'ent_config', ?, ?, 0.85, 'depends_on')",
            (self.user_id, self.tenant_id),
        )

        conn.commit()

    # ------------------------------------------------------------------
    # Scenario execution
    # ------------------------------------------------------------------

    def run_scenario(
        self,
        scenario: EvalScenario,
        budget_chars: int | None = 2000,
    ) -> ScenarioResult:
        """Execute a single evaluation scenario against inject_context.

        Measures payload size, latency, memory retrieval quality, and PII safety.
        """
        try:
            return self._do_run_scenario(scenario, budget_chars)
        except Exception as exc:
            logger.exception("Scenario %s failed with exception", scenario.id)
            return ScenarioResult(
                scenario_id=scenario.id,
                query=scenario.query,
                passed=False,
                metrics={},
                found_memory_ids=[],
                missing_expected=list(scenario.expected_memory_ids),
                injection_payload_size=0,
                latency_ms=0.0,
                signal_counts={},
                fast_path=None,
                pii_findings=[],
                error=str(exc),
            )

    def _do_run_scenario(
        self,
        scenario: EvalScenario,
        budget_chars: int | None,
    ) -> ScenarioResult:
        """Internal: run scenario without exception handling."""
        from foresight_mcp.server import inject_context as ic_fn
        from foresight_mcp.tenant_context import set_current_user_id, set_current_account_id

        # Set tenant context for server functions
        set_current_user_id(self.user_id)
        set_current_account_id(self.tenant_id)

        # Also patch the module-level USER_ID for inject_context
        import foresight_mcp.server as server_module

        original_user_id = server_module.USER_ID
        server_module.USER_ID = self.user_id

        try:
            # Run inject_context (budgeted) with low relevance threshold
            # because fixture memories are short and produce low RRF scores.
            t0 = time.perf_counter()
            result_str = ic_fn(
                conversation_text=scenario.query,
                user_id=self.user_id,
                max_memories=10,
                min_relevance=0.01,
                include_details=True,
                max_chars=budget_chars,
            )
            latency_ms = (time.perf_counter() - t0) * 1000

            # Parse result
            result = json.loads(result_str)
            memories = result.get("memories", [])
            found_ids = [m.get("memory_id", "") for m in memories]
            formatted = result.get("formatted", "")

            # Also run get_relevant_memories for structured metrics
            from foresight_mcp.server import get_relevant_memories as grm_fn

            t1 = time.perf_counter()
            grm_str = grm_fn(
                query=scenario.query,
                user_id=self.user_id,
                limit=10,
                min_relevance=0.01,
            )
            grm_latency = (time.perf_counter() - t1) * 1000
            grm_result = json.loads(grm_str)

            # Determine missing expected memories
            found_set = set(found_ids)
            missing = sorted(scenario.expected_memory_ids - found_set)

            # Pass condition
            # S3 has special condition: current must rank higher than stale
            if scenario.id == "S3_stale_vs_current":
                idx_current = next(
                    (i for i, mid in enumerate(found_ids) if mid == "current_auth_approach"),
                    None,
                )
                idx_stale = next(
                    (i for i, mid in enumerate(found_ids) if mid == "stale_auth_approach"),
                    None,
                )
                passed = idx_current is not None and idx_stale is not None and idx_current < idx_stale
            else:
                # General: at least min_expected expected memories in the results
                passed = len(found_set & scenario.expected_memory_ids) >= scenario.min_expected

            # Signal counts
            signal_counts = grm_result.get("signal_counts", {})
            total_candidates = grm_result.get("total_candidates", 0)

            # PII scan
            pii_findings = scan_for_pii(formatted)

            metrics = {
                "total_candidates": total_candidates,
                "grm_latency_ms": round(grm_latency, 2),
                "grm_result_count": len(grm_result.get("memories", [])),
                "budget_chars": budget_chars,
            }

            return ScenarioResult(
                scenario_id=scenario.id,
                query=scenario.query,
                passed=passed,
                metrics=metrics,
                found_memory_ids=found_ids,
                missing_expected=missing,
                injection_payload_size=len(formatted),
                latency_ms=latency_ms,
                signal_counts=signal_counts,
                fast_path=signal_counts.get("fast_path"),
                pii_findings=pii_findings,
            )
        finally:
            server_module.USER_ID = original_user_id

    def run_all(
        self,
        budget_chars: int | None = 2000,
        unbounded_budget: bool = False,
    ) -> EvalReport:
        """Run all evaluation scenarios and produce a report.

        Args:
            budget_chars: Character budget for budgeted injection.
                If None, uses unbounded injection.
            unbounded_budget: If True, also run unbounded version for comparison.

        Returns:
            EvalReport with results from all scenarios.
        """
        results: list[ScenarioResult] = []
        total_payload = 0
        total_latency = 0.0
        total_pii = 0
        scenario_count = 0

        for scenario in SCENARIOS:
            # Reset hybrid retriever between scenarios to avoid cross-contamination
            from foresight_mcp.hybrid_retriever import reset_hybrid_retriever

            reset_hybrid_retriever()
            result = self.run_scenario(scenario, budget_chars)
            results.append(result)
            total_payload += result.injection_payload_size
            total_latency += result.latency_ms
            total_pii += len(result.pii_findings)
            scenario_count += 1

        passed_count = sum(1 for r in results if r.passed)
        total_count = len(results)

        summary: dict[str, Any] = {
            "total": total_count,
            "passed": passed_count,
            "pass_rate_pct": (passed_count / total_count * 100) if total_count > 0 else 0.0,
            "avg_payload_size": total_payload / scenario_count if scenario_count > 0 else 0,
            "avg_latency_ms": total_latency / scenario_count if scenario_count > 0 else 0,
            "pii_findings_total": total_pii,
            "pii_scenarios_with_findings": sum(1 for r in results if r.pii_findings),
        }

        return EvalReport(
            timestamp=datetime.now(timezone.utc).isoformat(),
            scenarios=results,
            summary=summary,
        )

    def compare_baseline(
        self,
        baseline_report: EvalReport,
        new_report: EvalReport,
    ) -> dict[str, Any]:
        """Compare two reports and return a diff dict for before/after analysis."""
        diff: dict[str, Any] = {
            "baseline_timestamp": baseline_report.timestamp,
            "new_timestamp": new_report.timestamp,
            "payload_change_pct": self._pct_diff(
                baseline_report.summary.get("avg_payload_size"),
                new_report.summary.get("avg_payload_size"),
            ),
            "latency_change_pct": self._pct_diff(
                baseline_report.summary.get("avg_latency_ms"),
                new_report.summary.get("avg_latency_ms"),
            ),
            "pass_rate_change": (
                new_report.summary.get("pass_rate_pct", 0) - baseline_report.summary.get("pass_rate_pct", 0)
            ),
            "pii_change": (
                new_report.summary.get("pii_findings_total", 0) - baseline_report.summary.get("pii_findings_total", 0)
            ),
            "scenario_diffs": [],
        }

        baseline_map = {s.scenario_id: s for s in baseline_report.scenarios}
        for new_scenario in new_report.scenarios:
            base = baseline_map.get(new_scenario.scenario_id)
            if base is not None:
                scenario_diff = {
                    "scenario_id": new_scenario.scenario_id,
                    "payload_change": new_scenario.injection_payload_size - base.injection_payload_size,
                    "latency_change": round(new_scenario.latency_ms - base.latency_ms, 2),
                    "status_change": "new_pass"
                    if (not base.passed and new_scenario.passed)
                    else "new_fail"
                    if (base.passed and not new_scenario.passed)
                    else "unchanged",
                }
                diff["scenario_diffs"].append(scenario_diff)

        return diff

    @staticmethod
    def _pct_diff(old: float | None, new: float | None) -> float | None:
        if old is None or new is None or old == 0:
            return None
        return (new - old) / old * 100

    def close(self) -> None:
        """Close the database connection, restore patches, and clean up."""
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


def run_eval(
    db_path: str | None = None,
    report_path: str | None = None,
    budget_chars: int | None = 2000,
    json_output: bool = False,
) -> EvalReport:
    """Run the full evaluation harness and optionally persist the report.

    Args:
        db_path: Path to temp database for fixtures. None = in-memory.
        report_path: If set, path to write the JSON report.
        budget_chars: Character budget for budgeted injection.
        json_output: If True, print JSON report to stdout instead of text.

    Returns:
        EvalReport instance.
    """
    harness = EvalHarness(db_path=db_path)
    try:
        count = harness.seed_fixtures()
        logger.info("Seeded %d fixture memories", count)
        report = harness.run_all(budget_chars=budget_chars)

        if report_path:
            with open(report_path, "w") as f:
                json.dump(report.to_dict(), f, indent=2)
            logger.info("Report written to %s", report_path)

        if json_output:
            print(json.dumps(report.to_dict(), indent=2))
        else:
            print(report.format_text())

        return report
    finally:
        harness.close()


def main() -> None:
    """CLI entry point. Run with: python -m foresight_mcp.eval_harness"""
    import argparse

    parser = argparse.ArgumentParser(description="Foresight Evaluation Harness (PIX-3953)")
    parser.add_argument(
        "--db-path",
        default=None,
        help="Path to temporary database (default: in-memory)",
    )
    parser.add_argument(
        "--report",
        default=None,
        help="Path to write JSON report (default: no file output)",
    )
    parser.add_argument(
        "--budget",
        type=int,
        default=2000,
        help="Character budget for injection (default: 2000)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output report as JSON",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)
    run_eval(
        db_path=args.db_path,
        report_path=args.report,
        budget_chars=args.budget,
        json_output=args.json,
    )


if __name__ == "__main__":
    main()
