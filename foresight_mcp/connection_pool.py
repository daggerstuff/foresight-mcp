"""SQLite Connection Pool for Foresight MCP.

Provides thread-safe connection pooling with WAL mode, foreign keys,
and automatic cleanup of stale connections.
"""
from __future__ import annotations

import sqlite3
import threading
import time
import os
from collections import deque

from .config import DB_PATH


class ConnectionPool:
    """Thread-safe SQLite connection pool."""

    def __init__(self, db_path: str = DB_PATH, max_size: int = 10, max_idle_seconds: int = 300):
        self.db_path = db_path
        self.max_size = max_size
        self.max_idle_seconds = max_idle_seconds
        self._pool: deque[tuple[sqlite3.Connection, float]] = deque()  # (conn, last_used)
        self._in_use: set[sqlite3.Connection] = set()
        self._lock = threading.Lock()

    def acquire(self) -> "PooledConnection":
        """Get a connection from the pool, reusing idle wrappers when possible."""
        with self._lock:
            # Try to reuse an idle connection (could be raw conn or wrapper)
            while self._pool:
                item, last_used = self._pool.popleft()
                # Discard stale connections based on underlying raw connection
                raw = item._conn if isinstance(item, PooledConnection) else item
                if time.time() - last_used > self.max_idle_seconds:
                    try:
                        raw.close()
                    except Exception:
                        pass
                    continue
                # Validate connection is still alive
                try:
                    raw.execute("SELECT 1")
                    self._in_use.add(raw)
                    # Return the original wrapper if we have one, else wrap anew
                    if isinstance(item, PooledConnection):
                        return item
                    else:
                        return PooledConnection(raw, self)
                except Exception:
                    try:
                        raw.close()
                    except Exception:
                        pass
                    continue

            # Create a new connection only if under the size limit
            if len(self._in_use) >= self.max_size:
                raise RuntimeError(
                    f"Connection pool exhausted ({self.max_size} connections in use)"
                )
            conn = self._new_connection()
            self._in_use.add(conn)
            return PooledConnection(conn, self)

    def _unwrap_connection(self, conn: sqlite3.Connection | PooledConnection) -> sqlite3.Connection:
        """Get underlying sqlite3 connection from either wrapper or raw connection."""
        if isinstance(conn, PooledConnection):
            return conn._conn
        return conn

    def release(self, conn: sqlite3.Connection | PooledConnection) -> None:
        """Return a connection to the pool."""
        if isinstance(conn, PooledConnection) and getattr(conn, "_released", False):
            return
        if isinstance(conn, PooledConnection):
            wrapper_conn = conn
            conn._released = True
        else:
            wrapper_conn = None
        raw_conn = self._unwrap_connection(conn)

        with self._lock:
            if raw_conn not in self._in_use:
                # Avoid double-release or unknown connections from reintroducing duplicates.
                if any(stored_conn is raw_conn for stored_conn, _ in self._pool):
                    if wrapper_conn is not None:
                        wrapper_conn._released = True
                    return
                try:
                    raw_conn.close()
                except Exception:
                    pass
                if wrapper_conn is not None:
                    wrapper_conn._released = True
                return

            self._in_use.discard(raw_conn)
            if len(self._pool) < self.max_size and not any(
                stored_conn is raw_conn for stored_conn, _ in self._pool
            ):
                try:
                    raw_conn.execute("SELECT 1")
                    self._pool.append((wrapper_conn if wrapper_conn is not None else raw_conn, time.time()))
                    # Mark released flag if wrapper exists (already set earlier)
                    # No further action needed
                    return
                except Exception:
                    pass
            # Pool full or connection dead -- close it
            try:
                raw_conn.close()
            except Exception:
                pass
            if wrapper_conn is not None:
                wrapper_conn._released = True

    def _new_connection(self) -> sqlite3.Connection:
        """Create a new database connection with proper settings."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def close_all(self) -> None:
        """Close all connections (for shutdown/testing)."""
        with self._lock:
            for conn, _ in self._pool:
                try:
                    conn.close()
                except Exception:
                    pass
            for conn in list(self._in_use):
                try:
                    conn.close()
                except Exception:
                    pass
            self._pool.clear()
            self._in_use.clear()

    @property
    def stats(self) -> dict:
        """Pool statistics."""
        with self._lock:
            return {
                "idle": len(self._pool),
                "in_use": len(self._in_use),
                "max_size": self.max_size,
            }


# Global pools keyed by db path so tests can use isolated databases safely.
_pools: dict[str, ConnectionPool] = {}
_pool_lock = threading.Lock()


def get_pool(db_path: str | None = None) -> ConnectionPool:
    """Get or create the global connection pool (thread-safe)."""
    global _pools
    with _pool_lock:
        from .config import DB_PATH as default_path

        pool_path = os.path.abspath(db_path or default_path)
        if pool_path not in _pools:
            _pools[pool_path] = ConnectionPool(pool_path)
        return _pools[pool_path]


def reset_pool() -> None:
    """Reset the global pool (for testing)."""
    global _pools
    with _pool_lock:
        for pool in _pools.values():
            pool.close_all()
        _pools = {}


class PooledConnection:
    """Wraps a sqlite3.Connection so .close() returns it to the pool.

    All attribute access is delegated to the underlying connection,
    but calling .close() releases the connection back to the pool
    instead of truly closing it.
    """

    def __init__(self, conn: sqlite3.Connection, pool: ConnectionPool):
        self._conn = conn
        self._pool = pool
        self._released = False

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        if self._released:
            return
        self._pool.release(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def __del__(self):
        # Ensure connections are never leaked if callers forget to close.
        try:
            self.close()
        except Exception:
            pass
