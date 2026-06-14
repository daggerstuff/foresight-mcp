"""Tenant-isolated audit log for foresight-mcp clinical workflows.

Replaces the Python ``logging``-based stopgap (PIX-3738) with a SQLite
table that supports queryable, tenant-isolated, retention-controlled
audit events. Every event in the system that touches PHI or LLM-derived
output should emit a row through this module.

Append-only tamper-evidence
---------------------------

The ``audit_events`` table is enforced append-only at the SQLite layer
via triggers (see ``_SCHEMA``). The application cannot ``UPDATE`` or
``DELETE`` rows; attempts raise ``sqlite3.IntegrityError``. This is a
deliberate HIPAA-grade control — the audit log must be tamper-evident.

Tenant isolation
----------------

Every :meth:`AuditLog.record` call requires ``tenant_id``; every
:meth:`AuditLog.query` call requires ``tenant_id`` as a positional
argument. Cross-tenant reads are not expressible. ``query()`` returns
an empty list for a tenant with no events rather than raising.

Backward compatibility
----------------------

If no :class:`AuditLog` is configured (e.g. unit tests, ephemeral CLI
runs), callers should fall back to ``logger.info(...)`` rather than
requiring a database. The narrative module wires this fallback
automatically via the optional ``audit_log`` parameter.

Storage location
----------------

Per the GAP-6c design, audit lives in a separate SQLite file alongside
the memory store (``audit_events.db``). This avoids WAL contention
with the hot-path memory tables and lets the audit file have its own
retention/encryption policy. Callers control the path via the
``db_path`` constructor argument.
"""

from __future__ import annotations

import atexit
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("foresight_audit")

# Event-type constants. These are strings (not an enum) so new event
# types do not require code changes to the audit table. The narrative
# module uses these; new modules that emit audit events should follow
# the same lowercase_snake_case convention.
NARRATIVE_GENERATED = "narrative_generated"
NARRATIVE_FAILED = "narrative_failed"
NARRATIVE_CACHE_HIT = "narrative_cache_hit"
LLM_CALL_SUCCEEDED = "llm_call_succeeded"
LLM_CALL_FAILED = "llm_call_failed"


# SQL schema. The ``audit_events`` table is created lazily on first
# connection. The two ``RAISE(ABORT, ...)`` triggers enforce
# append-only semantics at the SQLite layer, independent of application
# code. ``_SCHEMA`` is a multi-statement string; ``executescript`` is
# the documented sqlite3 entry point for that.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_events_tenant_time
    ON audit_events(tenant_id, created_at);

CREATE INDEX IF NOT EXISTS idx_audit_events_type
    ON audit_events(event_type);

CREATE INDEX IF NOT EXISTS idx_audit_events_resource
    ON audit_events(resource_id);

CREATE INDEX IF NOT EXISTS idx_audit_events_tenant_type
    ON audit_events(tenant_id, event_type);

CREATE TRIGGER IF NOT EXISTS audit_events_no_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only: UPDATE is not allowed');
END;

CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only: DELETE is not allowed');
END;
"""


@dataclass(frozen=True)
class AuditEvent:
    """A single audit event to be persisted via :meth:`AuditLog.record`.

    Attributes:
        tenant_id: Required for isolation. Every read filters by this.
        user_id: The user the event pertains to. Required.
        event_type: A short snake_case string identifying the event
            class. See module-level constants for common types.
        resource_id: Identifier of the resource the event pertains to
            (e.g. ``report_id`` for a narrative, model name for an
            LLM call). Use empty string if not applicable.
        metadata: A dict of event-specific metadata. The dict is
            serialized as JSON; ``datetime`` and other non-JSON-native
            types are coerced via ``default=str``. Do NOT include raw
            PHI, raw memory ``content``, or raw LLM prompt/response
            bodies in ``metadata``. Use hashes only.
        created_at: Epoch seconds (UTC). Defaults to ``time.time()``
            at construction; tests may pin it.
    """

    tenant_id: str
    user_id: str
    event_type: str
    resource_id: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.tenant_id or not isinstance(self.tenant_id, str):
            raise ValueError("tenant_id is required and must be a non-empty string")
        if not self.user_id or not isinstance(self.user_id, str):
            raise ValueError("user_id is required and must be a non-empty string")
        if not self.event_type or not isinstance(self.event_type, str):
            raise ValueError("event_type is required and must be a non-empty string")
        if not isinstance(self.resource_id, str):
            raise ValueError("resource_id must be a string (use '' if not applicable)")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata must be a dict")


class AuditLog:
    """Tenant-isolated audit log backed by SQLite.

    The connection is opened lazily on first use, configured with WAL
    mode and ``check_same_thread=False`` for cross-process safety. The
    schema (table + indexes + append-only triggers) is created on
    first use. A single instance is safe to share across threads; the
    internal lock serializes writes.

    The instance registers an ``atexit`` handler that closes the
    underlying connection when the interpreter shuts down. Callers
    that want deterministic lifecycle (e.g. tests) should call
    :meth:`close` explicitly.
    """

    def __init__(self, db_path: str) -> None:
        if not db_path or not isinstance(db_path, str):
            raise ValueError("db_path is required and must be a non-empty string")
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._closed = False
        atexit.register(self.close)

    # ------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
            conn.executescript(_SCHEMA)
            conn.commit()
            self._conn = conn
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error as exc:
                    logger.warning("audit close failed: %s", exc)
                self._conn = None
                self._closed = True

    def __enter__(self) -> AuditLog:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ------------------------------------------------------------
    # Write
    # ------------------------------------------------------------

    def record(self, event: AuditEvent) -> None:
        """Persist an audit event.

        Append-only is enforced by SQLite triggers. Any attempt to
        call this twice with the same event id is harmless — each
        insert is independent.

        Raises:
            sqlite3.IntegrityError: If the database is closed or the
                schema is missing. The narrative module catches this
                and falls back to ``logger.info``.
        """
        if self._closed:
            raise sqlite3.ProgrammingError("AuditLog is closed")
        metadata_json = json.dumps(event.metadata, sort_keys=True, default=str)
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """
                INSERT INTO audit_events
                    (tenant_id, user_id, event_type, resource_id, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.tenant_id,
                    event.user_id,
                    event.event_type,
                    event.resource_id,
                    metadata_json,
                    event.created_at,
                ),
            )
            conn.commit()

    # ------------------------------------------------------------
    # Read
    # ------------------------------------------------------------

    def query(
        self,
        tenant_id: str,
        *,
        since: float | None = None,
        until: float | None = None,
        event_type: str | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        """Query audit events for a single tenant.

        Tenant isolation: ``tenant_id`` is a required positional
        argument. There is intentionally no way to query across
        tenants.

        Args:
            tenant_id: Required. Only events for this tenant are
                returned.
            since: If provided, only events with ``created_at >=
                since`` are returned (epoch seconds).
            until: If provided, only events with ``created_at <=
                until`` are returned (epoch seconds).
            event_type: If provided, only events of this type are
                returned.
            limit: Maximum number of events to return. Defaults to
                100. The query orders by ``created_at DESC, id DESC``
                so the most recent events come first.

        Returns:
            A list of :class:`AuditEvent` instances, possibly empty.
        """
        if not tenant_id or not isinstance(tenant_id, str):
            raise ValueError("tenant_id is required and must be a non-empty string")
        if limit <= 0:
            raise ValueError("limit must be a positive integer")

        clauses = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("created_at <= ?")
            params.append(until)
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)

        params.append(limit)
        sql = (
            "SELECT tenant_id, user_id, event_type, resource_id, metadata_json, created_at "
            "FROM audit_events "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC, id DESC "
            "LIMIT ?"
        )

        conn = self._get_conn()
        rows = conn.execute(sql, params).fetchall()
        return [
            AuditEvent(
                tenant_id=row["tenant_id"],
                user_id=row["user_id"],
                event_type=row["event_type"],
                resource_id=row["resource_id"],
                metadata=json.loads(row["metadata_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def count(self, tenant_id: str, *, event_type: str | None = None) -> int:
        """Count audit events for a tenant (cheap, no row hydration)."""
        if not tenant_id or not isinstance(tenant_id, str):
            raise ValueError("tenant_id is required and must be a non-empty string")
        clauses = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        sql = f"SELECT COUNT(*) AS n FROM audit_events WHERE {' AND '.join(clauses)}"
        conn = self._get_conn()
        return int(conn.execute(sql, params).fetchone()["n"])

    def stats(self, tenant_id: str) -> dict[str, Any]:
        """Aggregate stats for a tenant.

        Returns a dict with:
            * ``total`` — total event count
            * ``by_type`` — ``{event_type: count}``
            * ``first_at`` — earliest ``created_at`` (epoch seconds), or None
            * ``last_at`` — latest ``created_at`` (epoch seconds), or None
        """
        if not tenant_id or not isinstance(tenant_id, str):
            raise ValueError("tenant_id is required and must be a non-empty string")
        conn = self._get_conn()
        total_row = conn.execute(
            "SELECT COUNT(*) AS n, MIN(created_at) AS first_at, MAX(created_at) AS last_at "
            "FROM audit_events WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()
        type_rows = conn.execute(
            "SELECT event_type, COUNT(*) AS n FROM audit_events WHERE tenant_id = ? GROUP BY event_type",
            (tenant_id,),
        ).fetchall()
        return {
            "total": int(total_row["n"]),
            "by_type": {row["event_type"]: int(row["n"]) for row in type_rows},
            "first_at": total_row["first_at"],
            "last_at": total_row["last_at"],
        }


__all__ = [
    "LLM_CALL_FAILED",
    "LLM_CALL_SUCCEEDED",
    "NARRATIVE_CACHE_HIT",
    "NARRATIVE_FAILED",
    "NARRATIVE_GENERATED",
    "AuditEvent",
    "AuditLog",
]
