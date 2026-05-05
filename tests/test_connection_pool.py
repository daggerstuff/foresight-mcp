"""Tests for the SQLite ConnectionPool."""
import threading
import time

import pytest
from foresight_mcp.connection_pool import (
    ConnectionPool,
    PooledConnection,
    get_pool,
    reset_pool,
)


@pytest.fixture(autouse=True)
def _cleanup_global_pool():
    """Reset the global singleton before and after every test."""
    reset_pool()
    yield
    reset_pool()


@pytest.fixture
def db_path(tmp_path):
    """Return a real SQLite file path (pool uses file paths, not :memory:)."""
    return str(tmp_path / "test.db")


@pytest.fixture
def pool(db_path):
    """Create a small pool suitable for testing."""
    return ConnectionPool(db_path, max_size=3, max_idle_seconds=1)


# ---------------------------------------------------------------------------
# 1. acquire returns a usable connection
# ---------------------------------------------------------------------------
def test_acquire_returns_connection(pool):
    conn = pool.acquire()
    try:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        row = conn.execute("SELECT x FROM t").fetchone()
        assert row[0] == 1
    finally:
        pool.release(conn)


# ---------------------------------------------------------------------------
# 2. released connections reuse the underlying raw connection
# ---------------------------------------------------------------------------
def test_acquire_release_reuse(pool):
    conn = pool.acquire()
    raw = conn._conn
    pool.release(conn)
    conn2 = pool.acquire()
    try:
        # Fresh wrapper but same underlying sqlite3 connection
        assert conn2._conn is raw
    finally:
        pool.release(conn2)


# ---------------------------------------------------------------------------
# 3. acquiring beyond max_size raises RuntimeError
# ---------------------------------------------------------------------------
def test_pool_exhausted_raises(pool):
    conns = [pool.acquire() for _ in range(pool.max_size)]
    try:
        with pytest.raises(RuntimeError, match="Connection pool exhausted"):
            pool.acquire()
    finally:
        for c in conns:
            pool.release(c)


# ---------------------------------------------------------------------------
# 4. close_all empties both idle and in_use
# ---------------------------------------------------------------------------
def test_close_all_clears_pool(pool):
    conn = pool.acquire()
    pool.release(conn)  # one idle
    _conn2 = pool.acquire()  # one in-use
    pool.close_all()
    assert pool.stats == {"idle": 0, "in_use": 0, "max_size": pool.max_size}


# ---------------------------------------------------------------------------
# 5. connections idle longer than max_idle_seconds are discarded
# ---------------------------------------------------------------------------
def test_stale_connection_evicted(db_path):
    p = ConnectionPool(db_path, max_size=3, max_idle_seconds=0)
    conn = p.acquire()
    raw = conn._conn
    p.release(conn)
    time.sleep(0.05)
    conn2 = p.acquire()
    try:
        # The stale raw connection should have been discarded, new one created
        assert conn2._conn is not raw
        assert p.stats["idle"] == 0
    finally:
        p.release(conn2)
        p.close_all()


# ---------------------------------------------------------------------------
# 6. PooledConnection delegates execute/fetchone/commit
# ---------------------------------------------------------------------------
def test_pooled_connection_delegates(pool):
    conn = pool.acquire()
    try:
        conn.execute("CREATE TABLE d (v TEXT)")
        conn.execute("INSERT INTO d VALUES ('hello')")
        conn.commit()
        row = conn.execute("SELECT v FROM d").fetchone()
        assert row[0] == "hello"
    finally:
        pool.release(conn)


# ---------------------------------------------------------------------------
# 7. calling .close() on PooledConnection returns it to the pool
# ---------------------------------------------------------------------------
def test_pooled_connection_close_returns_to_pool(pool):
    conn = pool.acquire()
    assert pool.stats["in_use"] == 1

    conn.close()  # should release, not truly close
    assert pool.stats["in_use"] == 0
    assert pool.stats["idle"] == 1

    # The raw connection should be reusable
    conn2 = pool.acquire()
    try:
        conn2.execute("SELECT 1")
    finally:
        pool.release(conn2)


# ---------------------------------------------------------------------------
# 8. if a pooled connection fails SELECT 1, it's discarded
# ---------------------------------------------------------------------------
def test_dead_connection_discarded(pool):
    conn = pool.acquire()
    # Force-close the underlying sqlite3 connection to simulate a dead conn.
    conn._conn.close()
    pool.release(conn)
    # The dead connection should be dropped during release (it fails SELECT 1).
    assert pool.stats["idle"] == 0

    # Acquire should still work by creating a fresh connection.
    conn2 = pool.acquire()
    try:
        conn2.execute("SELECT 1")
    finally:
        pool.release(conn2)


# ---------------------------------------------------------------------------
# 9. stats reflect current state
# ---------------------------------------------------------------------------
def test_stats_property(pool):
    assert pool.stats == {"idle": 0, "in_use": 0, "max_size": 3}

    c1 = pool.acquire()
    assert pool.stats == {"idle": 0, "in_use": 1, "max_size": 3}

    c2 = pool.acquire()
    assert pool.stats == {"idle": 0, "in_use": 2, "max_size": 3}

    pool.release(c1)
    assert pool.stats == {"idle": 1, "in_use": 1, "max_size": 3}

    pool.release(c2)
    assert pool.stats == {"idle": 2, "in_use": 0, "max_size": 3}


# ---------------------------------------------------------------------------
# 10. get_pool returns same instance across threads
# ---------------------------------------------------------------------------
def test_thread_safe_singleton(db_path):
    results: list[ConnectionPool] = []
    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        p = get_pool(db_path)
        results.append(p)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # All threads must have received the exact same object.
    assert all(p is results[0] for p in results)
