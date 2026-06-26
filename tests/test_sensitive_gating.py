"""Tests for clinical/safety/PHI gating (PIX-3956).

Five scenarios covering the locked-in acceptance criteria:

1. Detector — sensitive text → is_sensitive=True at capture; non-sensitive
   content leaves the bit at 0.
2. Consolidate — sensitive neighbors are flagged for review, never
   auto-merged, regardless of overlap score.
3. Archive stale — sensitive rows are never archived by default; only when
   the caller explicitly opts into sensitive_only=True.
4. Contradict — sensitive memories are still scanned and flagged for
   review; the run counts them via sensitive_excluded (the audit log
   signal).
5. Tenant isolation — sensitive flag does not leak across (tenant_id,
   user_id) boundaries.
"""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timezone

from foresight_mcp.memory_maintenance import (
    MaintenanceConfig,
    MemoryMaintenanceJob,
)


def _make_test_db() -> str:
    """Create a temp DB with the schema MemoryMaintenanceJob expects."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL DEFAULT 'default',
            content TEXT,
            scope TEXT DEFAULT 'session',
            retention TEXT DEFAULT 'short_term',
            category TEXT DEFAULT 'general',
            importance REAL DEFAULT 0.5,
            current_strength REAL DEFAULT 1.0,
            strength_trend TEXT DEFAULT 'stable',
            activation_count INTEGER DEFAULT 0,
            is_ghost INTEGER DEFAULT 0,
            is_sensitive INTEGER NOT NULL DEFAULT 0,
            sensitivity_reason TEXT,
            gist TEXT,
            synthesized_from TEXT,
            emotional_context TEXT,
            metrics TEXT,
            tags TEXT,
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            event_type TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            actor TEXT,
            entity_id TEXT,
            payload TEXT
        )"""
    )
    conn.commit()
    conn.close()
    return db_path


def _insert_memory(
    conn,
    mid: str,
    content: str,
    *,
    importance: float = 0.5,
    strength_trend: str = "stable",
    is_ghost: int = 0,
    user_id: str = "u1",
    tenant_id: str = "t1",
    is_sensitive: int = 0,
) -> None:
    conn.execute(
        "INSERT INTO memories (id, user_id, tenant_id, content, importance, strength_trend, is_ghost, is_sensitive, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            mid,
            user_id,
            tenant_id,
            content,
            importance,
            strength_trend,
            is_ghost,
            is_sensitive,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def _row(conn, mid: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM memories WHERE id = ?", (mid,)).fetchone()


class TestSensitiveDetector:
    def test_phi_text_marked_sensitive(self) -> None:
        from foresight_mcp.sensitivity import detect_sensitivity, resolve_is_sensitive

        is_sensitive, reason = resolve_is_sensitive(None, "Patient SSN 123-45-6789 seen at clinic")
        assert is_sensitive is True
        assert reason == "pii_pattern"

        verdict = detect_sensitivity("User prefers short updates")
        assert verdict.is_sensitive is False

    def test_clinical_keyword_triggers_detector(self) -> None:
        from foresight_mcp.sensitivity import detect_sensitivity

        verdict = detect_sensitivity("Patient is on prescription medication, dosage adjustment needed")
        assert verdict.is_sensitive is True
        assert verdict.reason == "clinical_keyword"

    def test_caller_override_wins(self) -> None:
        from foresight_mcp.sensitivity import resolve_is_sensitive

        is_sensitive, reason = resolve_is_sensitive(True, "user prefers dark mode")
        assert is_sensitive is True
        assert reason == "caller_override"

        is_sensitive, reason = resolve_is_sensitive(False, "patient SSN 999-00-1111")
        assert is_sensitive is False
        assert reason is None


class TestConsolidateSensitiveSkipped:
    def test_sensitive_cluster_only_flagged_for_review(self) -> None:
        db_path = _make_test_db()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        identical = "prescription dosage change to 5mg daily for patient records"
        _insert_memory(conn, "mem_s1", identical, is_sensitive=1)
        _insert_memory(conn, "mem_s2", identical, is_sensitive=1)
        _insert_memory(conn, "mem_s3", identical, is_sensitive=1)

        job = MemoryMaintenanceJob(db_path=db_path)
        stats = job.run(
            MaintenanceConfig(
                user_id="u1",
                tenant_id="t1",
                modes=["consolidate"],
                duplicate_threshold=0.25,
                consolidation_overlap_high=0.5,
                consolidation_overlap_marginal=0.3,
                batch_size=50,
            )
        )
        assert stats.duplicates_auto_consolidated == 0
        assert stats.sensitive_excluded >= 1

        for mid in ("mem_s1", "mem_s2", "mem_s3"):
            row = _row(conn, mid)
            assert row is not None
            assert int(row["is_ghost"]) == 0, "sensitive rows must not be soft-archived by consolidate"

        conn.close()


class TestArchiveStaleSensitiveSkipped:
    def test_sensitive_rows_never_archived_by_default(self) -> None:
        db_path = _make_test_db()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        _insert_memory(
            conn,
            "mem_stale_s",
            "diagnostic notes low importance",
            importance=0.05,
            strength_trend="stale",
            is_sensitive=1,
        )
        _insert_memory(
            conn,
            "mem_stale_n",
            "low importance non-sensitive",
            importance=0.05,
            strength_trend="stale",
            is_sensitive=0,
        )

        job = MemoryMaintenanceJob(db_path=db_path)
        stats = job.run(
            MaintenanceConfig(
                user_id="u1",
                tenant_id="t1",
                modes=["archive_stale"],
                stale_importance_threshold=0.1,
                batch_size=50,
            )
        )

        assert stats.stale_archived == 1
        assert stats.stale_found == 1
        s_row = _row(conn, "mem_stale_s")
        n_row = _row(conn, "mem_stale_n")
        assert s_row is not None, "sensitive row must still exist"
        assert int(s_row["is_ghost"]) == 0, "sensitive must never auto-archive"
        assert n_row is not None, "non-sensitive row must still exist"
        assert int(n_row["is_ghost"]) == 1, "non-sensitive low-importance must archive"

        conn.close()

    def test_sensitive_only_explicitly_targets_sensitive_for_review(self) -> None:
        db_path = _make_test_db()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        _insert_memory(
            conn,
            "mem_s_target",
            "medication history summary",
            importance=0.05,
            strength_trend="stale",
            is_sensitive=1,
        )

        job = MemoryMaintenanceJob(db_path=db_path)
        stats = job.run(
            MaintenanceConfig(
                user_id="u1",
                tenant_id="t1",
                modes=["archive_stale"],
                stale_importance_threshold=0.1,
                sensitive_only=True,
                tool_access="observe",
                batch_size=50,
                tool_access="observe"
            )
        )

        assert stats.stale_archived == 1
        assert stats.stale_found == 1
        row = _row(conn, "mem_s_target")
        assert row is not None, "sensitive-only target must still exist"
        assert int(row["is_ghost"]) == 1

        conn.close()


class TestContradictSensitiveSurfaced:
    def test_sensitive_pair_still_flagged_for_review(self) -> None:
        db_path = _make_test_db()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        _insert_memory(
            conn,
            "mem_c1",
            "treatment feels helpful and safe for patient",
            is_sensitive=1,
        )
        _insert_memory(
            conn,
            "mem_c2",
            "treatment feels harmful and afraid for patient",
            is_sensitive=1,
        )

        job = MemoryMaintenanceJob(db_path=db_path)
        stats = job.run(
            MaintenanceConfig(
                user_id="u1",
                tenant_id="t1",
                modes=["contradict"],
                batch_size=50,
            )
        )

        assert stats.contradictions_found >= 1
        assert stats.contradictions_flagged_review >= 1
        assert stats.to_dict()["sensitive_excluded"] >= 1

        row = _row(conn, "mem_c1")
        assert row is not None, "contradicted row must still exist"
        assert int(row["is_ghost"]) == 0

        conn.close()


class TestTenantIsolation:
    def test_sensitive_flag_does_not_leak_across_tenants(self) -> None:
        db_path = _make_test_db()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        _insert_memory(
            conn,
            "mem_a1",
            "patient prescription notes duplicate",
            tenant_id="t_clinic",
            user_id="u_doc",
            is_sensitive=1,
        )
        _insert_memory(
            conn,
            "mem_a2",
            "patient prescription notes duplicate",
            tenant_id="t_clinic",
            user_id="u_doc",
            is_sensitive=1,
        )
        _insert_memory(
            conn,
            "mem_b1",
            "patient prescription notes duplicate",
            tenant_id="t_other",
            user_id="u_doc",
            is_sensitive=0,
        )

        job = MemoryMaintenanceJob(db_path=db_path)
        stats_clinic = job.run(
            MaintenanceConfig(
                user_id="u_doc",
                tenant_id="t_clinic",
                modes=["consolidate"],
                consolidation_overlap_high=0.5,
                consolidation_overlap_marginal=0.3,
                batch_size=50,
            )
        )

        assert stats_clinic.duplicates_auto_consolidated == 0
        assert stats_clinic.sensitive_excluded >= 1

        conn.close()
