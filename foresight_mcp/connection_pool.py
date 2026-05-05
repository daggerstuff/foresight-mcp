"""SQLite Connection Pool for Foresight MCP.

Provides thread-safe connection pooling with WAL mode, foreign keys,
and automatic cleanup of stale connections.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections import deque
from contextlib import suppress

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

    def acquire(self) -> PooledConnection:
        """Get a connection from the pool."""
        with self._lock:
            while self._pool:
                raw, last_used = self._pool.popleft()
                if time.time() - last_used > self.max_idle_seconds:
                    with suppress(Exception):
                        raw.close()
                    continue
                try:
                    raw.execute("SELECT 1")
                    self._in_use.add(raw)
                    return PooledConnection(raw, self)
                except Exception:
                    with suppress(Exception):
                        raw.close()
                    continue

            if len(self._in_use) >= self.max_size:
                raise RuntimeError(
                    f"Connection pool exhausted ({self.max_size} connections in use)"
                )
            conn = self._new_connection()
            self._in_use.add(conn)
            return PooledConnection(conn, self)

    def release(self, conn: sqlite3.Connection | PooledConnection) -> None:
        """Return a connection to the pool."""
        if isinstance(conn, PooledConnection):
            if conn._released:
                return
            conn._released = True
            raw = conn._conn
        else:
            raw = conn

        with self._lock:
            if raw not in self._in_use:
                with suppress(Exception):
                    raw.close()
                return

            self._in_use.discard(raw)
            if len(self._pool) < self.max_size and not any(
                stored is raw for stored, _ in self._pool
            ):
                try:
                    raw.execute("SELECT 1")
                    self._pool.append((raw, time.time()))
                    return
                except Exception:
                    pass
            with suppress(Exception):
                raw.close()

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
                with suppress(Exception):
                    conn.close()
            for conn in list(self._in_use):
                with suppress(Exception):
                    conn.close()
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
    with _pool_lock:
        pool_path = os.path.abspath(db_path or DB_PATH)
        if pool_path not in _pools:
            _pools[pool_path] = ConnectionPool(pool_path)
        return _pools[pool_path]


def reset_pool() -> None:
    """Reset the global pool (for testing)."""
    with _pool_lock:
        for pool in _pools.values():
            pool.close_all()
        _pools.clear()


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

    def __exit__(self, *_):
        self.close()

    def __del__(self):
        with suppress(Exception):
            self.close()
