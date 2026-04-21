"""SQLite Connection Pool for Foresight MCP.

Provides thread-safe connection pooling with WAL mode, foreign keys,
and automatic cleanup of stale connections.
"""
import sqlite3
import threading
import time
from typing import Optional

from .config import DB_PATH


class ConnectionPool:
    """Thread-safe SQLite connection pool."""

    def __init__(self, db_path: str = DB_PATH, max_size: int = 10, max_idle_seconds: int = 300):
        self.db_path = db_path
        self.max_size = max_size
        self.max_idle_seconds = max_idle_seconds
        self._pool: list[tuple[sqlite3.Connection, float]] = []  # (conn, last_used)
        self._in_use: set[sqlite3.Connection] = set()
        self._lock = threading.Lock()

    def acquire(self) -> sqlite3.Connection:
        """Get a connection from the pool."""
        with self._lock:
            # Try to reuse an idle connection
            while self._pool:
                conn, last_used = self._pool.pop()
                # Discard stale connections
                if time.time() - last_used > self.max_idle_seconds:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    continue
                # Validate connection is still alive
                try:
                    conn.execute("SELECT 1")
                    self._in_use.add(conn)
                    return conn
                except Exception:
                    try:
                        conn.close()
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
            return conn

    def release(self, conn: sqlite3.Connection) -> None:
        """Return a connection to the pool."""
        with self._lock:
            self._in_use.discard(conn)
            if len(self._pool) < self.max_size:
                try:
                    conn.execute("SELECT 1")
                    self._pool.append((conn, time.time()))
                    return
                except Exception:
                    pass
            # Pool full or connection dead -- close it
            try:
                conn.close()
            except Exception:
                pass

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


# Global pool instance
_pool: Optional[ConnectionPool] = None
_pool_lock = threading.Lock()


def get_pool(db_path: Optional[str] = None) -> ConnectionPool:
    """Get or create the global connection pool (thread-safe)."""
    global _pool
    with _pool_lock:
        if _pool is None:
            from .config import DB_PATH as default_path
            _pool = ConnectionPool(db_path or default_path)
        return _pool


def reset_pool() -> None:
    """Reset the global pool (for testing)."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close_all()
        _pool = None


class PooledConnection:
    """Wraps a sqlite3.Connection so .close() returns it to the pool.

    All attribute access is delegated to the underlying connection,
    but calling .close() releases the connection back to the pool
    instead of truly closing it.
    """

    def __init__(self, conn: sqlite3.Connection, pool: ConnectionPool):
        self._conn = conn
        self._pool = pool

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        self._pool.release(self._conn)
