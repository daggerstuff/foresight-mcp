"""Tests for the tenant-isolated audit log (PIX-3741 / GAP-6c)."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from foresight_mcp.audit import (
    LLM_CALL_FAILED,
    LLM_CALL_SUCCEEDED,
    NARRATIVE_CACHE_HIT,
    NARRATIVE_FAILED,
    NARRATIVE_GENERATED,
    AuditEvent,
    AuditLog,
)

# --------------------------------------------------------------------
# AuditEvent validation
# --------------------------------------------------------------------


def test_audit_event_validates_inputs() -> None:
    with pytest.raises(ValueError, match="tenant_id"):
        AuditEvent(
            tenant_id="",
            user_id="u1",
            event_type="x",
            resource_id="r",
        )
    with pytest.raises(ValueError, match="user_id"):
        AuditEvent(
            tenant_id="t1",
            user_id="",
            event_type="x",
            resource_id="r",
        )
    with pytest.raises(ValueError, match="event_type"):
        AuditEvent(
            tenant_id="t1",
            user_id="u1",
            event_type="",
            resource_id="r",
        )
    with pytest.raises(ValueError, match="resource_id must be a string"):
        AuditEvent(
            tenant_id="t1",
            user_id="u1",
            event_type="x",
            resource_id=42,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="metadata must be a dict"):
        AuditEvent(
            tenant_id="t1",
            user_id="u1",
            event_type="x",
            resource_id="r",
            metadata="not a dict",  # type: ignore[arg-type]
        )


# --------------------------------------------------------------------
# AuditLog lifecycle + schema
# --------------------------------------------------------------------


def test_audit_log_creates_schema_on_first_use(tmp_path: Any) -> None:
    db_path = tmp_path / "audit.db"
    log = AuditLog(str(db_path))
    log.record(
        AuditEvent(
            tenant_id="t1",
            user_id="u1",
            event_type=NARRATIVE_GENERATED,
            resource_id="r1",
        )
    )

    # Inspect the on-disk schema directly to verify table + indexes + triggers.
    conn = sqlite3.connect(str(db_path))
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "audit_events" in tables

        indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_audit_events_tenant_time" in indexes
        assert "idx_audit_events_type" in indexes
        assert "idx_audit_events_resource" in indexes
        assert "idx_audit_events_tenant_type" in indexes

        triggers = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        assert "audit_events_no_update" in triggers
        assert "audit_events_no_delete" in triggers
    finally:
        conn.close()
    log.close()


def test_audit_log_validates_inputs(tmp_path: Any) -> None:
    log = AuditLog(str(tmp_path / "audit.db"))
    try:
        with pytest.raises(ValueError, match="db_path"):
            AuditLog("")
        with pytest.raises(ValueError, match="tenant_id"):
            log.query("")
        with pytest.raises(ValueError, match="limit"):
            log.query("t1", limit=0)
        with pytest.raises(ValueError, match="limit"):
            log.query("t1", limit=-1)
    finally:
        log.close()


# --------------------------------------------------------------------
# Write + read
# --------------------------------------------------------------------


def test_audit_log_record_and_query(tmp_path: Any) -> None:
    log = AuditLog(str(tmp_path / "audit.db"))
    try:
        log.record(
            AuditEvent(
                tenant_id="t1",
                user_id="u1",
                event_type=NARRATIVE_GENERATED,
                resource_id="r1",
                metadata={"latency_ms": 123.4, "outcome": "success"},
            )
        )
        events = log.query("t1")
        assert len(events) == 1
        ev = events[0]
        assert ev.tenant_id == "t1"
        assert ev.user_id == "u1"
        assert ev.event_type == NARRATIVE_GENERATED
        assert ev.resource_id == "r1"
        assert ev.metadata == {"latency_ms": 123.4, "outcome": "success"}
    finally:
        log.close()


def test_audit_log_tenant_isolation(tmp_path: Any) -> None:
    log = AuditLog(str(tmp_path / "audit.db"))
    try:
        log.record(AuditEvent(tenant_id="t1", user_id="u1", event_type="x", resource_id="r1"))
        log.record(AuditEvent(tenant_id="t2", user_id="u1", event_type="x", resource_id="r2"))
        log.record(AuditEvent(tenant_id="t2", user_id="u2", event_type="x", resource_id="r3"))

        assert [ev.resource_id for ev in log.query("t1")] == ["r1"]
        assert sorted(ev.resource_id for ev in log.query("t2")) == ["r2", "r3"]
        assert log.query("t_unknown") == []
    finally:
        log.close()


def test_audit_log_query_by_event_type(tmp_path: Any) -> None:
    log = AuditLog(str(tmp_path / "audit.db"))
    try:
        log.record(AuditEvent(tenant_id="t1", user_id="u1", event_type=NARRATIVE_GENERATED, resource_id="r1"))
        log.record(AuditEvent(tenant_id="t1", user_id="u1", event_type=NARRATIVE_FAILED, resource_id="r2"))
        log.record(AuditEvent(tenant_id="t1", user_id="u1", event_type=NARRATIVE_CACHE_HIT, resource_id="r3"))

        only_generated = log.query("t1", event_type=NARRATIVE_GENERATED)
        assert [ev.resource_id for ev in only_generated] == ["r1"]

        only_failed = log.query("t1", event_type=NARRATIVE_FAILED)
        assert [ev.resource_id for ev in only_failed] == ["r2"]
    finally:
        log.close()


def test_audit_log_query_by_time_window(tmp_path: Any) -> None:
    log = AuditLog(str(tmp_path / "audit.db"))
    try:
        t0 = 1_000_000.0
        log.record(
            AuditEvent(
                tenant_id="t1",
                user_id="u1",
                event_type="x",
                resource_id="r1",
                created_at=t0,
            )
        )
        log.record(
            AuditEvent(
                tenant_id="t1",
                user_id="u1",
                event_type="x",
                resource_id="r2",
                created_at=t0 + 100.0,
            )
        )
        log.record(
            AuditEvent(
                tenant_id="t1",
                user_id="u1",
                event_type="x",
                resource_id="r3",
                created_at=t0 + 200.0,
            )
        )

        # `since` is inclusive: at exactly t0+100 we still get r2.
        middle = log.query("t1", since=t0 + 50.0, until=t0 + 150.0)
        assert [ev.resource_id for ev in middle] == ["r2"]

        # `until` is inclusive: at exactly t0 we still get r1.
        upto = log.query("t1", until=t0)
        assert [ev.resource_id for ev in upto] == ["r1"]
    finally:
        log.close()


def test_audit_log_metadata_round_trip(tmp_path: Any) -> None:
    class NonJsonable:
        def __init__(self, value: str) -> None:
            self.value = value

        def __repr__(self) -> str:
            return f"NonJsonable({self.value!r})"

    log = AuditLog(str(tmp_path / "audit.db"))
    try:
        metadata = {
            "prompt_hash": "abc123",
            "response_hash": "def456",
            "latency_ms": 12.5,
            "tags": ["a", "b"],
            "blob": NonJsonable("opaque-token"),
        }
        log.record(
            AuditEvent(
                tenant_id="t1",
                user_id="u1",
                event_type=NARRATIVE_GENERATED,
                resource_id="r1",
                metadata=metadata,
            )
        )
        ev = log.query("t1")[0]
        assert ev.metadata["prompt_hash"] == "abc123"
        assert ev.metadata["latency_ms"] == 12.5
        assert ev.metadata["tags"] == ["a", "b"]
        assert isinstance(ev.metadata["blob"], str)
        assert "NonJsonable" in ev.metadata["blob"]
        assert "opaque-token" in ev.metadata["blob"]
    finally:
        log.close()


def test_audit_log_query_orders_by_most_recent(tmp_path: Any) -> None:
    log = AuditLog(str(tmp_path / "audit.db"))
    try:
        base = 1_700_000_000.0
        for i in range(5):
            log.record(
                AuditEvent(
                    tenant_id="t1",
                    user_id="u1",
                    event_type="x",
                    resource_id=f"r{i}",
                    created_at=base + i,
                )
            )
        events = log.query("t1", limit=10)
        # Most recent first.
        assert [ev.resource_id for ev in events] == ["r4", "r3", "r2", "r1", "r0"]
    finally:
        log.close()


def test_audit_log_limit_truncates(tmp_path: Any) -> None:
    log = AuditLog(str(tmp_path / "audit.db"))
    try:
        for i in range(5):
            log.record(
                AuditEvent(
                    tenant_id="t1",
                    user_id="u1",
                    event_type="x",
                    resource_id=f"r{i}",
                )
            )
        events = log.query("t1", limit=2)
        assert len(events) == 2
    finally:
        log.close()


# --------------------------------------------------------------------
# Count + stats
# --------------------------------------------------------------------


def test_audit_log_count(tmp_path: Any) -> None:
    log = AuditLog(str(tmp_path / "audit.db"))
    try:
        for i in range(3):
            log.record(
                AuditEvent(
                    tenant_id="t1",
                    user_id="u1",
                    event_type=NARRATIVE_GENERATED,
                    resource_id=f"r{i}",
                )
            )
        for i in range(2):
            log.record(
                AuditEvent(
                    tenant_id="t1",
                    user_id="u1",
                    event_type=NARRATIVE_FAILED,
                    resource_id=f"f{i}",
                )
            )
        log.record(AuditEvent(tenant_id="t2", user_id="u1", event_type="x", resource_id="x"))

        assert log.count("t1") == 5
        assert log.count("t1", event_type=NARRATIVE_GENERATED) == 3
        assert log.count("t1", event_type=NARRATIVE_FAILED) == 2
        assert log.count("t1", event_type="nonexistent") == 0
        assert log.count("t2") == 1
    finally:
        log.close()


def test_audit_log_stats(tmp_path: Any) -> None:
    log = AuditLog(str(tmp_path / "audit.db"))
    try:
        assert log.stats("t1") == {
            "total": 0,
            "by_type": {},
            "first_at": None,
            "last_at": None,
        }
        t0 = 1_700_000_000.0
        log.record(
            AuditEvent(
                tenant_id="t1",
                user_id="u1",
                event_type=NARRATIVE_GENERATED,
                resource_id="r1",
                created_at=t0,
            )
        )
        log.record(
            AuditEvent(
                tenant_id="t1",
                user_id="u1",
                event_type=NARRATIVE_GENERATED,
                resource_id="r2",
                created_at=t0 + 10,
            )
        )
        log.record(
            AuditEvent(
                tenant_id="t1",
                user_id="u1",
                event_type=NARRATIVE_FAILED,
                resource_id="r3",
                created_at=t0 + 20,
            )
        )
        s = log.stats("t1")
        assert s["total"] == 3
        assert s["by_type"][NARRATIVE_GENERATED] == 2
        assert s["by_type"][NARRATIVE_FAILED] == 1
        assert s["first_at"] == t0
        assert s["last_at"] == t0 + 20
    finally:
        log.close()


# --------------------------------------------------------------------
# Append-only tamper-evidence
# --------------------------------------------------------------------


def test_audit_log_append_only_blocks_update(tmp_path: Any) -> None:
    log = AuditLog(str(tmp_path / "audit.db"))
    try:
        log.record(AuditEvent(tenant_id="t1", user_id="u1", event_type="x", resource_id="r1"))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            log._get_conn().execute("UPDATE audit_events SET event_type = 'tampered' WHERE tenant_id = 't1'")
    finally:
        log.close()


def test_audit_log_append_only_blocks_delete(tmp_path: Any) -> None:
    log = AuditLog(str(tmp_path / "audit.db"))
    try:
        log.record(AuditEvent(tenant_id="t1", user_id="u1", event_type="x", resource_id="r1"))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            log._get_conn().execute("DELETE FROM audit_events WHERE tenant_id = 't1'")
    finally:
        log.close()


# --------------------------------------------------------------------
# Concurrent writes are serialized; reads are safe.
# --------------------------------------------------------------------


def test_audit_log_thread_safe_writes(tmp_path: Any) -> None:
    import threading

    log = AuditLog(str(tmp_path / "audit.db"))
    try:
        n_threads = 4
        n_per_thread = 25

        def worker(thread_idx: int) -> None:
            for i in range(n_per_thread):
                log.record(
                    AuditEvent(
                        tenant_id="t1",
                        user_id=f"u{thread_idx}",
                        event_type=NARRATIVE_GENERATED,
                        resource_id=f"r{thread_idx}-{i}",
                    )
                )

        threads = [threading.Thread(target=worker, args=(idx,)) for idx in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert log.count("t1") == n_threads * n_per_thread
    finally:
        log.close()


# --------------------------------------------------------------------
# LLM_CALL_* event type constants exported
# --------------------------------------------------------------------


def test_event_type_constants_distinct() -> None:
    seen = {
        NARRATIVE_GENERATED,
        NARRATIVE_FAILED,
        NARRATIVE_CACHE_HIT,
        LLM_CALL_SUCCEEDED,
        LLM_CALL_FAILED,
    }
    assert len(seen) == 5
