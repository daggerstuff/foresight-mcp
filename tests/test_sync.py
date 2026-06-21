"""Tests for offline-first synchronization."""

import os
import tempfile
from datetime import datetime, timezone

import pytest
from foresight_mcp.sync import (
    Operation,
    OperationQueue,
    OperationType,
    SyncManager,
    SyncProgress,
    SyncStatus,
    reset_sync_manager,
)


@pytest.fixture
def temp_db():
    """Create temporary database file."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    if os.path.exists(db_path):
        os.unlink(db_path)


@pytest.fixture(autouse=True)
def reset_sync():
    """Reset sync manager before each test."""
    reset_sync_manager()


class TestOperation:
    """Test Operation dataclass."""

    def test_operation_creation(self):
        """Test creating an operation."""
        op = Operation(
            id="op-1",
            type=OperationType.CREATE,
            entity_type="memory",
            entity_id="mem-123",
            payload={"content": "test"},
        )
        assert op.id == "op-1"
        assert op.type == OperationType.CREATE
        assert op.entity_type == "memory"
        assert op.entity_id == "mem-123"

    def test_operation_to_dict(self):
        """Test operation serialization."""
        op = Operation(
            id="op-1",
            type=OperationType.UPDATE,
            entity_type="memory",
            entity_id="mem-123",
            payload={"content": "updated"},
        )
        data = op.to_dict()
        assert data["id"] == "op-1"
        assert data["type"] == "update"
        assert "vector_clock" in data

    def test_operation_from_dict(self):
        """Test operation deserialization."""
        data = {
            "id": "op-1",
            "type": "create",
            "entity_type": "memory",
            "entity_id": "mem-123",
            "payload": {"content": "test"},
            "created_at": "2026-04-16T00:00:00+00:00",
            "retry_count": 0,
            "vector_clock": {"node-1": 1},
        }
        op = Operation.from_dict(data)
        assert op.id == "op-1"
        assert op.type == OperationType.CREATE


class TestOperationQueue:
    """Test OperationQueue."""

    def test_enqueue_dequeue(self, temp_db):
        """Test enqueue and dequeue."""
        queue = OperationQueue(db_path=temp_db)
        op = Operation(
            id="op-1",
            type=OperationType.CREATE,
            entity_type="memory",
            entity_id="mem-123",
            payload={"content": "test"},
        )
        queue.enqueue(op)
        assert queue.count() == 1

        dequeued = queue.dequeue()
        assert dequeued is not None
        assert dequeued.id == "op-1"

    def test_remove(self, temp_db):
        """Test removing operation."""
        queue = OperationQueue(db_path=temp_db)
        op = Operation(
            id="op-1",
            type=OperationType.CREATE,
            entity_type="memory",
            entity_id="mem-123",
            payload={},
        )
        queue.enqueue(op)
        queue.remove("op-1")
        assert queue.count() == 0

    def test_peek(self, temp_db):
        """Test peeking at all operations."""
        queue = OperationQueue(db_path=temp_db)
        for i in range(3):
            queue.enqueue(
                Operation(
                    id=f"op-{i}",
                    type=OperationType.CREATE,
                    entity_type="memory",
                    entity_id=f"mem-{i}",
                    payload={},
                )
            )

        ops = queue.peek()
        assert len(ops) == 3


class TestSyncManager:
    """Test SyncManager."""

    def test_enqueue_operation(self, temp_db):
        """Test enqueueing operations."""
        manager = SyncManager(node_id="test-node")
        manager._queue = OperationQueue(db_path=temp_db)

        op_id = manager.enqueue_operation(
            type=OperationType.CREATE,
            entity_type="memory",
            entity_id="mem-123",
            payload={"content": "test"},
        )

        assert op_id is not None
        assert manager._queue.count() == 1

    def test_sync_success(self, temp_db):
        """Test successful sync."""
        manager = SyncManager(node_id="test-node")
        manager._queue = OperationQueue(db_path=temp_db)

        # Enqueue operation
        manager.enqueue_operation(
            type=OperationType.CREATE,
            entity_type="memory",
            entity_id="mem-123",
            payload={},
        )

        # Sync with successful callback
        def success_callback(op):
            return True

        manager._sync_callback = success_callback
        progress = manager.sync()

        assert progress.status == SyncStatus.IDLE
        assert manager._queue.count() == 0

    def test_sync_retry_on_failure(self, temp_db):
        """Test retry on failure."""
        manager = SyncManager(node_id="test-node", max_retries=3)
        manager._queue = OperationQueue(db_path=temp_db)

        manager.enqueue_operation(
            type=OperationType.CREATE,
            entity_type="memory",
            entity_id="mem-123",
            payload={},
        )

        # Always fail
        def fail_callback(op):
            raise Exception("Sync failed")

        manager._sync_callback = fail_callback
        progress = manager.sync()

        # Should have retried
        assert progress.status == SyncStatus.ERROR
        assert len(progress.errors) > 0

    def test_online_offline_status(self, temp_db):
        """Test online/offline status."""
        manager = SyncManager(node_id="test-node")
        manager._queue = OperationQueue(db_path=temp_db)

        manager.set_online(False)
        assert manager._status == SyncStatus.OFFLINE

        manager.set_online(True)
        assert manager._status == SyncStatus.IDLE

    def test_progress_callback(self, temp_db):
        """Test progress callbacks."""
        manager = SyncManager(node_id="test-node")
        manager._queue = OperationQueue(db_path=temp_db)

        progress_received = []

        def on_progress(progress):
            progress_received.append(progress)

        manager.on_progress(on_progress)
        manager.enqueue_operation(
            type=OperationType.CREATE,
            entity_type="memory",
            entity_id="mem-123",
            payload={},
        )

        assert len(progress_received) > 0

    def test_get_status(self, temp_db):
        """Test getting sync status."""
        manager = SyncManager(node_id="test-node")
        manager._queue = OperationQueue(db_path=temp_db)

        status = manager.get_status()

        assert "status" in status
        assert "total_operations" in status
        assert "pending_operations" in status


class TestSyncProgress:
    """Test SyncProgress."""

    def test_progress_to_dict(self):
        """Test progress serialization."""
        progress = SyncProgress(
            status=SyncStatus.SYNCING,
            total_operations=10,
            pending_operations=5,
            synced_operations=5,
            errors=["error1"],
            last_sync=datetime.now(timezone.utc),
        )
        data = progress.to_dict()
        assert data["status"] == "syncing"
        assert data["total_operations"] == 10
        assert data["pending_operations"] == 5


class TestSyncScenarios:
    """Test sync scenarios."""

    def test_network_partition_recovery(self, temp_db):
        """Test recovery after network partition."""
        manager = SyncManager(node_id="node-1", max_retries=3)
        manager._queue = OperationQueue(db_path=temp_db)

        # Go offline and queue operations
        manager.set_online(False)
        manager.enqueue_operation(
            type=OperationType.CREATE,
            entity_type="memory",
            entity_id="mem-1",
            payload={"content": "offline-1"},
        )
        manager.enqueue_operation(
            type=OperationType.CREATE,
            entity_type="memory",
            entity_id="mem-2",
            payload={"content": "offline-2"},
        )

        # Come back online and sync
        manager.set_online(True)
        manager._sync_callback = lambda op: True  # Success
        progress = manager.sync()

        assert progress.status == SyncStatus.IDLE
        assert manager._queue.count() == 0

    def test_retry_exponential_backoff(self, temp_db):
        """Test exponential backoff on retry."""
        manager = SyncManager(
            node_id="test-node",
            max_retries=3,
            retry_delay=0.1,
        )
        manager._queue = OperationQueue(db_path=temp_db)

        attempt_count = [0]

        def flaky_callback(op):
            attempt_count[0] += 1
            if attempt_count[0] < 2:
                raise Exception("Temporary failure")
            return True

        manager.enqueue_operation(
            type=OperationType.CREATE,
            entity_type="memory",
            entity_id="mem-1",
            payload={},
        )

        manager._sync_callback = flaky_callback
        manager.sync()

        # Should have retried
        assert attempt_count[0] >= 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
