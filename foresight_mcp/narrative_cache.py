"""
SQLite-backed cache for generated reflection narratives.

The cache stores LLM-derived narrative text keyed by tenant, user, report,
model version, and insights hash. Callers choose the SQLite file path so the
file can live in the same tenant-isolated storage tier as the rest of the
memory store.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


DEFAULT_MAX_ENTRIES = 10_000
DEFAULT_TTL_SECONDS = 604_800


class NarrativeCache:
    """Persistent SQLite cache with tenant/user isolation and LRU eviction."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than 0")

        self.db_path = Path(db_path)
        self.max_entries = max_entries
        self.ttl_seconds = float(ttl_seconds)
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        self._eviction_count = 0
        self._closed = False

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._initialize()
        atexit.register(self.close)

    def get(
        self,
        report_id: str,
        *,
        tenant_id: str,
        user_id: str,
        model_version: str,
        insights_hash: str,
    ) -> str | None:
        """Return a cached narrative, or ``None`` on miss or TTL expiry."""
        self._validate_parts(
            report_id=report_id,
            tenant_id=tenant_id,
            user_id=user_id,
            model_version=model_version,
            insights_hash=insights_hash,
        )
        cache_key = self._cache_key(
            tenant_id=tenant_id,
            user_id=user_id,
            report_id=report_id,
            model_version=model_version,
            insights_hash=insights_hash,
        )
        now = time.time()

        with self._lock:
            row = self._conn.execute(
                """
                SELECT narrative, created_at
                FROM narrative_cache
                WHERE cache_key = ? AND tenant_id = ? AND user_id = ?
                """,
                (cache_key, tenant_id, user_id),
            ).fetchone()

            if row is None:
                self._misses += 1
                return None

            if row["created_at"] < now - self.ttl_seconds:
                self._conn.execute(
                    """
                    DELETE FROM narrative_cache
                    WHERE cache_key = ? AND tenant_id = ? AND user_id = ?
                    """,
                    (cache_key, tenant_id, user_id),
                )
                self._misses += 1
                return None

            self._conn.execute(
                """
                UPDATE narrative_cache
                SET last_accessed_at = ?, access_count = access_count + 1
                WHERE cache_key = ? AND tenant_id = ? AND user_id = ?
                """,
                (now, cache_key, tenant_id, user_id),
            )
            self._harden_file_permissions()
            self._hits += 1
            return str(row["narrative"])

    def put(
        self,
        report_id: str,
        narrative: str,
        *,
        tenant_id: str,
        user_id: str,
        model_version: str,
        insights_hash: str,
    ) -> None:
        """Insert or replace a cached narrative and enforce size bounds."""
        self._validate_parts(
            report_id=report_id,
            tenant_id=tenant_id,
            user_id=user_id,
            model_version=model_version,
            insights_hash=insights_hash,
        )
        if not isinstance(narrative, str):
            raise TypeError("narrative must be a string")

        cache_key = self._cache_key(
            tenant_id=tenant_id,
            user_id=user_id,
            report_id=report_id,
            model_version=model_version,
            insights_hash=insights_hash,
        )
        now = time.time()

        with self._lock:
            if self._size() >= int(self.max_entries * 0.9):
                self._delete_expired(now)

            self._conn.execute(
                """
                INSERT INTO narrative_cache (
                    cache_key,
                    tenant_id,
                    user_id,
                    report_id,
                    model_version,
                    insights_hash,
                    narrative,
                    created_at,
                    last_accessed_at,
                    access_count
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(cache_key) DO UPDATE SET
                    tenant_id = excluded.tenant_id,
                    user_id = excluded.user_id,
                    report_id = excluded.report_id,
                    model_version = excluded.model_version,
                    insights_hash = excluded.insights_hash,
                    narrative = excluded.narrative,
                    created_at = excluded.created_at,
                    last_accessed_at = excluded.last_accessed_at,
                    access_count = 0
                """,
                (
                    cache_key,
                    tenant_id,
                    user_id,
                    report_id,
                    model_version,
                    insights_hash,
                    narrative,
                    now,
                    now,
                ),
            )
            self._evict_lru()
            self._harden_file_permissions()

    def clear(self, tenant_id: str | None = None) -> int:
        """Clear all cache entries, or only entries for one tenant."""
        with self._lock:
            if tenant_id is None:
                cursor = self._conn.execute("DELETE FROM narrative_cache")
            else:
                cursor = self._conn.execute(
                    "DELETE FROM narrative_cache WHERE tenant_id = ?",
                    (tenant_id,),
                )
            self._harden_file_permissions()
            return int(cursor.rowcount)

    def stats(self) -> dict[str, Any]:
        """Return cache size and in-process hit/eviction counters."""
        with self._lock:
            size = self._size()
            requests = self._hits + self._misses
            hit_rate = self._hits / requests if requests else 0.0
            return {
                "size": size,
                "max_entries": self.max_entries,
                "ttl_seconds": self.ttl_seconds,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": hit_rate,
                "eviction_count": self._eviction_count,
            }

    def close(self) -> None:
        """Close the SQLite connection. Safe to call more than once."""
        with self._lock:
            if self._closed:
                return
            self._conn.close()
            self._closed = True

    def _initialize(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS narrative_cache (
                    cache_key TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    report_id TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    insights_hash TEXT NOT NULL,
                    narrative TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_accessed_at REAL NOT NULL,
                    access_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_narrative_cache_tenant_user
                ON narrative_cache(tenant_id, user_id)
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_narrative_cache_created
                ON narrative_cache(created_at)
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_narrative_cache_last_accessed
                ON narrative_cache(last_accessed_at)
                """
            )
            self._harden_file_permissions()

    def _size(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS count FROM narrative_cache").fetchone()
        return int(row["count"])

    def _delete_expired(self, now: float) -> None:
        cursor = self._conn.execute(
            "DELETE FROM narrative_cache WHERE created_at < ?",
            (now - self.ttl_seconds,),
        )
        self._eviction_count += max(int(cursor.rowcount), 0)

    def _evict_lru(self) -> None:
        overflow = self._size() - self.max_entries
        if overflow <= 0:
            return

        cursor = self._conn.execute(
            """
            DELETE FROM narrative_cache
            WHERE cache_key IN (
                SELECT cache_key
                FROM narrative_cache
                ORDER BY last_accessed_at ASC, created_at ASC
                LIMIT ?
            )
            """,
            (overflow,),
        )
        self._eviction_count += max(int(cursor.rowcount), 0)

    def _harden_file_permissions(self) -> None:
        for path in (
            self.db_path,
            Path(f"{self.db_path}-wal"),
            Path(f"{self.db_path}-shm"),
        ):
            if path.exists():
                path.chmod(0o600)

    @staticmethod
    def _cache_key(
        *,
        tenant_id: str,
        user_id: str,
        report_id: str,
        model_version: str,
        insights_hash: str,
    ) -> str:
        payload = json.dumps(
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "report_id": report_id,
                "model_version": model_version,
                "insights_hash": insights_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_parts(
        *,
        report_id: str,
        tenant_id: str,
        user_id: str,
        model_version: str,
        insights_hash: str,
    ) -> None:
        values = {
            "report_id": report_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "model_version": model_version,
            "insights_hash": insights_hash,
        }
        for name, value in values.items():
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} is required and must be a non-empty string")


__all__ = ["DEFAULT_MAX_ENTRIES", "DEFAULT_TTL_SECONDS", "NarrativeCache"]
