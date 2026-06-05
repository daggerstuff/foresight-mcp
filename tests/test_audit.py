"""Tests for SQLite-backed audit logging."""

import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from foresight_mcp.audit import AuditEvent, AuditLog


def test_audit_log_record_and_query(tmp_path: Path) -> None:
    """Recorded audit events are queryable by tenant."""
    audit_log = AuditLog(tmp_path / "audit.db")
    event = AuditEvent(
        tenant_id="tenant_a",
        user_id="user_1",
        event_type="reflection_narrative_generated",
        resource_id="refl_001",
        metadata={"outcome": "success"},
        created_at=time.time(),
    )

    audit_log.record(event)

    rows = audit_log.query("tenant_a")
    assert rows == [event]

    conn = sqlite3.connect(tmp_path / "audit.db")
    try:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'audit_events'"
        ).fetchone()
        assert table is not None
    finally:
        conn.close()
        audit_log.close()


def test_audit_log_tenant_isolation(tmp_path: Path) -> None:
    """Querying a tenant returns only that tenant's audit rows."""
    audit_log = AuditLog(tmp_path / "audit.db")
    now = time.time()
    audit_log.record(
        AuditEvent("tenant_a", "user_1", "type_a", "resource_1", {"value": 1}, now)
    )
    audit_log.record(
        AuditEvent("tenant_b", "user_1", "type_a", "resource_2", {"value": 2}, now)
    )

    rows = audit_log.query("tenant_a")

    assert len(rows) == 1
    assert rows[0].tenant_id == "tenant_a"
    assert rows[0].resource_id == "resource_1"
    audit_log.close()


def test_audit_log_query_by_event_type(tmp_path: Path) -> None:
    """Querying by event type narrows rows within a tenant."""
    audit_log = AuditLog(tmp_path / "audit.db")
    now = time.time()
    audit_log.record(AuditEvent("tenant_a", "user_1", "type_a", "resource_1", {}, now))
    audit_log.record(AuditEvent("tenant_a", "user_1", "type_b", "resource_2", {}, now))

    rows = audit_log.query("tenant_a", event_type="type_b")

    assert [row.event_type for row in rows] == ["type_b"]
    assert rows[0].resource_id == "resource_2"
    audit_log.close()


def test_audit_log_query_by_time_window(tmp_path: Path) -> None:
    """The since filter excludes older audit rows."""
    audit_log = AuditLog(tmp_path / "audit.db")
    old = 1_700_000_000.0
    new = old + 60
    audit_log.record(AuditEvent("tenant_a", "user_1", "type_a", "old", {}, old))
    audit_log.record(AuditEvent("tenant_a", "user_1", "type_a", "new", {}, new))

    rows = audit_log.query("tenant_a", since=old + 1)

    assert [row.resource_id for row in rows] == ["new"]
    audit_log.close()


def test_audit_log_metadata_round_trip(tmp_path: Path) -> None:
    """Metadata is JSON-encoded and decoded without losing nested values."""
    audit_log = AuditLog(tmp_path / "audit.db")
    metadata = {
        "outcome": "success",
        "latency_ms": 12.5,
        "hashes": {"prompt": "abc", "response": "def"},
        "tags": ["clinical", "llm"],
    }
    audit_log.record(
        AuditEvent("tenant_a", "user_1", "type_a", "resource_1", metadata, 1_700_000_000.0)
    )

    rows = audit_log.query("tenant_a")

    assert rows[0].metadata == metadata
    audit_log.close()


def test_audit_log_stats(tmp_path: Path) -> None:
    """Stats are tenant-scoped and grouped by event type and day."""
    audit_log = AuditLog(tmp_path / "audit.db")
    audit_log.record(AuditEvent("tenant_a", "user_1", "type_a", "resource_1", {}, 1_700_000_000.0))
    audit_log.record(AuditEvent("tenant_a", "user_1", "type_a", "resource_2", {}, 1_700_000_100.0))
    audit_log.record(AuditEvent("tenant_a", "user_1", "type_b", "resource_3", {}, 1_700_086_400.0))
    audit_log.record(AuditEvent("tenant_b", "user_1", "type_a", "resource_4", {}, 1_700_000_000.0))

    stats = audit_log.stats("tenant_a")

    assert stats["by_event_type"] == {"type_a": 2, "type_b": 1}
    assert stats["by_day"] == {"2023-11-14": 2, "2023-11-15": 1}
    audit_log.close()
