"""SQLite-backed audit log for tenant-isolated clinical workflow events."""

from __future__ import annotations

import atexit
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from .config import DB_PATH


@dataclass(frozen=True)
class AuditEvent:
    """Structured audit event persisted to the audit log."""

    NARRATIVE_GENERATED: ClassVar[str] = "reflection_narrative_generated"
    NARRATIVE_FAILED: ClassVar[str] = "reflection_narrative_failed"

    tenant_id: str
    user_id: str
    event_type: str
    resource_id: str
    metadata: dict[str, Any]
    created_at: float = field(default_factory=time.time)


class AuditLog:
    """Tenant-scoped SQLite audit event store."""

    def __init__(
        self,
        db_path: str | os.PathLike[str] = DB_PATH,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self.db_path = os.fspath(db_path)
        self._conn = connection
        self._owns_connection = connection is None
        self._schema_ready = False
        self._closed = False
        self._lock = threading.Lock()
        atexit.register(self.close)

    def record(self, event: AuditEvent) -> None:
        """Persist a single audit event row."""
        self._validate_event(event)
        metadata_json = json.dumps(event.metadata, sort_keys=True, separators=(",", ":"), default=str)

        with self._lock:
            conn = self._connection()
            self._ensure_schema(conn)
            conn.execute(
                """INSERT INTO audit_events
                (tenant_id, user_id, event_type, resource_id, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
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

    def query(
        self,
        tenant_id: str,
        *,
        since: float | None = None,
        event_type: str | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        """Return audit events for one tenant, optionally filtered by type/time."""
        if not tenant_id:
            raise ValueError("tenant_id is required")
        if limit < 1:
            raise ValueError("limit must be positive")

        clauses = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since)
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        params.append(limit)

        with self._lock:
            conn = self._connection()
            self._ensure_schema(conn)
            rows = conn.execute(
                f"""SELECT tenant_id, user_id, event_type, resource_id, metadata_json, created_at
                FROM audit_events
                WHERE {" AND ".join(clauses)}
                ORDER BY created_at ASC, id ASC
                LIMIT ?""",
                params,
            ).fetchall()

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

    def stats(self, tenant_id: str) -> dict[str, dict[str, int]]:
        """Return tenant-scoped audit counts grouped by event type and day."""
        if not tenant_id:
            raise ValueError("tenant_id is required")

        with self._lock:
            conn = self._connection()
            self._ensure_schema(conn)
            type_rows = conn.execute(
                """SELECT event_type, COUNT(*) AS count
                FROM audit_events
                WHERE tenant_id = ?
                GROUP BY event_type
                ORDER BY event_type ASC""",
                (tenant_id,),
            ).fetchall()
            day_rows = conn.execute(
                """SELECT date(created_at, 'unixepoch') AS day, COUNT(*) AS count
                FROM audit_events
                WHERE tenant_id = ?
                GROUP BY day
                ORDER BY day ASC""",
                (tenant_id,),
            ).fetchall()

        return {
            "by_event_type": {row["event_type"]: row["count"] for row in type_rows},
            "by_day": {row["day"]: row["count"] for row in day_rows},
        }

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            if self._closed:
                return
            if self._conn is not None and self._owns_connection:
                self._conn.close()
            self._conn = None
            self._closed = True

    def _connection(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("AuditLog is closed")
        if self._conn is None:
            Path(self.db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        return self._conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        if self._schema_ready:
            return
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS audit_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              tenant_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              event_type TEXT NOT NULL,
              resource_id TEXT NOT NULL,
              metadata_json TEXT NOT NULL,
              created_at REAL NOT NULL
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_events_tenant_time "
            "ON audit_events(tenant_id, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_events_type "
            "ON audit_events(event_type)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_events_resource "
            "ON audit_events(resource_id)"
        )
        conn.commit()
        self._schema_ready = True

    @staticmethod
    def _validate_event(event: AuditEvent) -> None:
        if not event.tenant_id:
            raise ValueError("event.tenant_id is required")
        if not event.user_id:
            raise ValueError("event.user_id is required")
        if not event.event_type:
            raise ValueError("event.event_type is required")
        if not event.resource_id:
            raise ValueError("event.resource_id is required")


__all__ = ["AuditEvent", "AuditLog"]
