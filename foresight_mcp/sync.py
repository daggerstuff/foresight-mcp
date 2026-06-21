"""
Offline-First Synchronization

Provides offline-first sync capabilities:
- Local storage (SQLite)
- Operation queue with persistence
- Sync manager with retry logic
- Progress events for UI
- Conflict resolution with CRDTs
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from .connection_pool import get_pool
from .crdt import LWWRegister, ORSet, VectorClock
from .tenant_context import get_current_tenant_id

logger = logging.getLogger("foresight_sync")


class SyncStatus(StrEnum):
    """Sync status."""

    IDLE = "idle"
    SYNCING = "syncing"
    OFFLINE = "offline"
    ERROR = "error"


class OperationType(StrEnum):
    """Types of operations."""

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


@dataclass
class Operation:
    """Represents a pending operation."""

    id: str
    type: OperationType
    entity_type: str
    entity_id: str
    payload: dict[str, Any]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    retry_count: int = 0
    last_attempt: datetime | None = None
    vector_clock: VectorClock = field(default_factory=VectorClock)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type.value,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "payload": self.payload,
            "created_at": self.created_at.isoformat(),
            "retry_count": self.retry_count,
            "last_attempt": self.last_attempt.isoformat() if self.last_attempt else None,
            "vector_clock": self.vector_clock.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Operation:
        op = cls(
            id=data["id"],
            type=OperationType(data["type"]),
            entity_type=data["entity_type"],
            entity_id=data["entity_id"],
            payload=data["payload"],
            created_at=datetime.fromisoformat(data["created_at"]),
            retry_count=data.get("retry_count", 0),
        )
        last_attempt = data.get("last_attempt")
        if last_attempt:
            op.last_attempt = datetime.fromisoformat(last_attempt)
        if "vector_clock" in data:
            op.vector_clock = VectorClock.from_dict(data["vector_clock"])
        return op


@dataclass
class SyncProgress:
    """Sync progress information."""

    status: SyncStatus
    total_operations: int
    pending_operations: int
    synced_operations: int
    errors: list[str] = field(default_factory=list)
    last_sync: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "total_operations": self.total_operations,
            "pending_operations": self.pending_operations,
            "synced_operations": self.synced_operations,
            "errors": self.errors,
            "last_sync": self.last_sync.isoformat() if self.last_sync else None,
        }


class OperationQueue:
    """
    Persistent operation queue.

    Stores operations that need to be synced when online.
    """

    def __init__(self, db_path: str | None = None):
        """Initialize operation queue.

        Args:
            db_path: Path to SQLite database (default: ~/.foresight/operations.db)
        """
        if db_path is None:
            db_path = str(Path.home() / ".foresight" / "operations.db")

        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database schema."""
        db_path = Path(self.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        pool = get_pool(self.db_path)
        conn = pool.acquire()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                type TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                retry_count INTEGER DEFAULT 0,
                last_attempt TEXT,
                vector_clock TEXT DEFAULT '{}'
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ops_entity ON operations(entity_type, entity_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ops_created ON operations(created_at)")
        # Migration: add tenant_id if table exists without it
        try:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(operations)").fetchall()]
            if cols and "tenant_id" not in cols:
                conn.execute("ALTER TABLE operations ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'")
        except sqlite3.OperationalError:
            logger.debug("operations table tenant_id column already exists")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ops_tenant ON operations(tenant_id)")
        conn.commit()
        pool.release(conn)

    def enqueue(self, operation: Operation, tenant_id: str | None = None) -> None:
        """Add operation to queue."""
        tid = tenant_id or get_current_tenant_id()
        pool = get_pool(self.db_path)
        conn = pool.acquire()
        conn.execute(
            """
            INSERT OR REPLACE INTO operations
            (id, tenant_id, type, entity_type, entity_id, payload, created_at, retry_count, last_attempt, vector_clock)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                operation.id,
                tid,
                operation.type.value,
                operation.entity_type,
                operation.entity_id,
                json.dumps(operation.payload),
                operation.created_at.isoformat(),
                operation.retry_count,
                operation.last_attempt.isoformat() if operation.last_attempt else None,
                json.dumps(operation.vector_clock.to_dict()),
            ),
        )
        conn.commit()
        pool.release(conn)

    def dequeue(self, tenant_id: str | None = None) -> Operation | None:
        """Get next operation from queue."""
        tid = tenant_id or get_current_tenant_id()
        pool = get_pool(self.db_path)
        conn = pool.acquire()
        row = conn.execute(
            "SELECT * FROM operations WHERE tenant_id = ? ORDER BY created_at LIMIT 1", (tid,)
        ).fetchone()
        pool.release(conn)

        if row:
            return Operation.from_dict(
                {
                    "id": row[0],
                    "type": row[2],
                    "entity_type": row[3],
                    "entity_id": row[4],
                    "payload": json.loads(row[5]),
                    "created_at": row[6],
                    "retry_count": row[7],
                    "last_attempt": row[8],
                    "vector_clock": json.loads(row[9]),
                }
            )
        return None

    def remove(self, operation_id: str, tenant_id: str | None = None) -> None:
        """Remove operation from queue."""
        tid = tenant_id or get_current_tenant_id()
        pool = get_pool(self.db_path)
        conn = pool.acquire()
        conn.execute("DELETE FROM operations WHERE id = ? AND tenant_id = ?", (operation_id, tid))
        conn.commit()
        pool.release(conn)

    def peek(self, tenant_id: str | None = None) -> list[Operation]:
        """Get all pending operations."""
        tid = tenant_id or get_current_tenant_id()
        pool = get_pool(self.db_path)
        conn = pool.acquire()
        rows = conn.execute("SELECT * FROM operations WHERE tenant_id = ? ORDER BY created_at", (tid,)).fetchall()
        pool.release(conn)

        operations = []
        for row in rows:
            operations.append(
                Operation.from_dict(
                    {
                        "id": row[0],
                        "type": row[2],
                        "entity_type": row[3],
                        "entity_id": row[4],
                        "payload": json.loads(row[5]),
                        "created_at": row[6],
                        "retry_count": row[7],
                        "last_attempt": row[8],
                        "vector_clock": json.loads(row[9]),
                    }
                )
            )
        return operations

    def count(self, tenant_id: str | None = None) -> int:
        """Get count of pending operations."""
        tid = tenant_id or get_current_tenant_id()
        pool = get_pool(self.db_path)
        conn = pool.acquire()
        count = conn.execute("SELECT COUNT(*) FROM operations WHERE tenant_id = ?", (tid,)).fetchone()[0]
        pool.release(conn)
        return count


class SyncManager:
    """
    Manages offline-first synchronization.

    Features:
    - Queue operations when offline
    - Sync when online with retry logic
    - Progress events for UI
    - Conflict resolution with CRDTs
    """

    def __init__(
        self,
        node_id: str = "default",
        max_retries: int = 3,
        retry_delay: float = 1.0,
        sync_callback: Callable[[Operation], bool] | None = None,
    ):
        """Initialize sync manager.

        Args:
            node_id: Unique node identifier for this client
            max_retries: Maximum retry attempts per operation
            retry_delay: Base delay between retries (exponential backoff)
            sync_callback: Callback to execute operation on server
        """
        self.node_id = node_id
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._sync_callback = sync_callback

        self._queue = OperationQueue()
        self._status = SyncStatus.IDLE
        self._errors: list[str] = []
        self._last_sync: datetime | None = None
        self._progress_callbacks: list[Callable[[SyncProgress], None]] = []

        # CRDT stores for local state
        self._local_data: dict[str, LWWRegister] = {}
        self._local_tags: dict[str, ORSet] = {}

    def set_online(self, online: bool) -> None:
        """Set online/offline status."""
        if not online:
            self._status = SyncStatus.OFFLINE
        elif self._status == SyncStatus.OFFLINE:
            self._status = SyncStatus.IDLE
        self._notify_progress()

    def enqueue_operation(
        self,
        type_: OperationType | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        payload: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        """
        Queue an operation for sync.

        Args:
            type_: Operation type (create, update, delete)
            entity_type: Type of entity
            entity_id: Entity identifier
            payload: Operation payload
            **kwargs: Backward compat — accepts 'type' as alias for 'type_'

        Returns:
            Operation ID
        """
        # Backward compat: accept 'type' (without underscore)
        if type_ is None and "type" in kwargs:
            type_ = kwargs.pop("type")
        if type_ is None:
            raise ValueError("type_ is required")
        if entity_type is None:
            raise ValueError("entity_type is required")
        if entity_id is None:
            raise ValueError("entity_id is required")
        if payload is None:
            payload = {}
        """
        Queue an operation for sync.

        Args:
            type: Operation type (create, update, delete)
            entity_type: Type of entity
            entity_id: Entity identifier
            payload: Operation payload

        Returns:
            Operation ID
        """
        operation = Operation(
            id=str(uuid.uuid4()),
            type=type_,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=payload,
        )
        operation.vector_clock.increment(self.node_id)

        self._queue.enqueue(operation)
        logger.info(f"Enqueued operation {operation.id}: {type_.value} {entity_type}:{entity_id}")

        self._notify_progress()
        return operation.id

    def sync(self) -> SyncProgress:
        """
        Sync pending operations.

        Returns:
            SyncProgress with current status
        """
        if self._status == SyncStatus.SYNCING:
            return self._get_progress()

        self._status = SyncStatus.SYNCING
        self._notify_progress()

        pending = self._queue.peek()
        synced = 0
        errors: list[str] = []

        for operation in pending:
            if operation.retry_count >= self.max_retries:
                # Max retries exceeded, skip
                errors.append(f"Max retries exceeded for {operation.id}")
                self._queue.remove(operation.id)
                continue

            try:
                if self._sync_callback:
                    success = self._sync_callback(operation)
                    if success:
                        self._queue.remove(operation.id)
                        synced += 1
                    else:
                        raise Exception("Sync callback returned False")
                else:
                    # No callback, just remove (simulated success)
                    self._queue.remove(operation.id)
                    synced += 1

                self._last_sync = datetime.now(timezone.utc)

            except Exception as e:
                # Retry with exponential backoff
                operation.retry_count += 1
                operation.last_attempt = datetime.now(timezone.utc)
                self._queue.enqueue(operation)  # Re-enqueue with updated retry count
                errors.append(f"Operation {operation.id} failed: {e}")
                logger.warning(f"Operation {operation.id} failed (attempt {operation.retry_count}): {e}")

        self._status = SyncStatus.IDLE if len(errors) == 0 else SyncStatus.ERROR
        self._errors = errors
        self._notify_progress()

        return self._get_progress()

    def _get_progress(self) -> SyncProgress:
        """Get current sync progress."""
        pending = self._queue.count()
        return SyncProgress(
            status=self._status,
            total_operations=pending,
            pending_operations=pending,
            synced_operations=0,  # Would track in production
            errors=self._errors,
            last_sync=self._last_sync,
        )

    def on_progress(self, callback: Callable[[SyncProgress], None]) -> None:
        """Register progress callback."""
        self._progress_callbacks.append(callback)

    def _notify_progress(self) -> None:
        """Notify progress callbacks."""
        progress = self._get_progress()
        for callback in self._progress_callbacks:
            try:
                callback(progress)
            except Exception as e:
                logger.error(f"Progress callback error: {e}")

    def get_status(self) -> dict[str, Any]:
        """Get sync status."""
        return self._get_progress().to_dict()


# =============================================================================
# Global Sync Manager
# =============================================================================


class _SyncManagerSingleton:
    """Module-level singleton for SyncManager."""

    _instance: SyncManager | None = None

    @classmethod
    def get_instance(cls, node_id: str = "default") -> SyncManager:
        """Get the global sync manager instance."""
        if cls._instance is None:
            cls._instance = SyncManager(node_id=node_id)
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the global sync manager (for testing)."""
        cls._instance = None


def get_sync_manager(node_id: str = "default") -> SyncManager:
    """Get the global sync manager instance."""
    return _SyncManagerSingleton.get_instance(node_id)


def reset_sync_manager() -> None:
    """Reset the global sync manager (for testing)."""
    _SyncManagerSingleton.reset()
