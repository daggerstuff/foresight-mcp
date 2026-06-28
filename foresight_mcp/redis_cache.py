"""
Redis-backed cache for generated reflection narratives.

Mirrors the public API of :class:`foresight_mcp.narrative_cache.NarrativeCache`
so callers can swap the in-process SQLite cache for a shared Redis cache
without changing call sites. Useful for multi-agent deployments where several
processes serve the same tenant/user pair and want to share cached
narratives.

Tenant / user isolation
-----------------------

Every entry is namespaced by ``tenant_id`` and ``user_id`` in addition to the
deterministic cache key derived from
``tenant_id, user_id, report_id, model_version, insights_hash``. The
Redis key layout is::

    {prefix}:narrative:{tenant_id}:{user_id}:{sha256_cache_key}

and an auxiliary LRU sorted set per shard::

    {prefix}:zset:{tenant_id}:{user_id}

The cache validates parts and computes cache keys through the same static
methods as :class:`NarrativeCache`, guaranteeing that ``put(NarrativeCache)``
followed by ``get(RedisCache)`` (or vice versa) returns the same row.

Eviction
--------

Two layers of eviction cooperate:

* Native Redis TTL via ``SETEX`` — entries expire after ``ttl_seconds``.
* LRU eviction on the auxiliary ZSET — when a shard grows beyond
  ``max_entries`` entries, the oldest by ``last_accessed_at`` are dropped.

The cache is thread-safe; an ``RLock`` guards the in-process hit/miss/
eviction counters. Connection close is idempotent.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

import redis

from .narrative_cache import NarrativeCache

DEFAULT_PREFIX = "foresight"
DEFAULT_MAX_ENTRIES = 10_000
DEFAULT_TTL_SECONDS = 604_800


class RedisCache:
    """Redis-backed cache for reflection narratives with LRU + TTL semantics."""

    def __init__(
        self,
        url: str,
        *,
        prefix: str = DEFAULT_PREFIX,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        if not url or not isinstance(url, str):
            raise ValueError("url is required and must be a non-empty string")
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than 0")
        if ":" in prefix:
            raise ValueError("prefix must not contain ':' (Redis key separator)")

        self._url = url
        self.prefix = prefix
        self.max_entries = max_entries
        self.ttl_seconds = float(ttl_seconds)

        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        self._eviction_count = 0
        self._closed = False

        self._client = redis.Redis.from_url(url, decode_responses=True)
        # Eagerly probe the connection so misconfiguration surfaces here
        # rather than on the first cache operation.
        self._client.ping()

    # ============================================================
    # Public API — mirrors NarrativeCache
    # ============================================================

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
        NarrativeCache._validate_parts(
            report_id=report_id,
            tenant_id=tenant_id,
            user_id=user_id,
            model_version=model_version,
            insights_hash=insights_hash,
        )
        cache_key = NarrativeCache._cache_key(
            tenant_id=tenant_id,
            user_id=user_id,
            report_id=report_id,
            model_version=model_version,
            insights_hash=insights_hash,
        )
        entry_key = self._entry_key(tenant_id, user_id, cache_key)
        zset_key = self._zset_key(tenant_id, user_id)

        with self._lock:
            value = self._client.get(entry_key)
            if value is None:
                self._misses += 1
                return None
            # Update LRU access timestamp on hit.
            self._client.zadd(zset_key, {cache_key: time.time()})
            self._hits += 1
            return str(value)

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
        NarrativeCache._validate_parts(
            report_id=report_id,
            tenant_id=tenant_id,
            user_id=user_id,
            model_version=model_version,
            insights_hash=insights_hash,
        )
        if not isinstance(narrative, str):
            raise TypeError("narrative must be a string")

        cache_key = NarrativeCache._cache_key(
            tenant_id=tenant_id,
            user_id=user_id,
            report_id=report_id,
            model_version=model_version,
            insights_hash=insights_hash,
        )
        entry_key = self._entry_key(tenant_id, user_id, cache_key)
        zset_key = self._zset_key(tenant_id, user_id)
        now = time.time()

        with self._lock:
            # SETEX primes value + TTL atomically.
            self._client.setex(entry_key, int(self.ttl_seconds), narrative)
            self._client.zadd(zset_key, {cache_key: now})
            # Give the auxiliary zset a longer TTL so unused shards don't
            # accumulate indefinitely when a tenant goes idle.
            self._client.expire(zset_key, max(int(self.ttl_seconds), 60))
            self._evict_lru(tenant_id, user_id)

    def clear(self, tenant_id: str | None = None) -> int:
        """Clear all cache entries, or only entries for one tenant."""
        with self._lock:
            if tenant_id is None:
                entries_pattern = f"{self.prefix}:narrative:*"
                zset_pattern = f"{self.prefix}:zset:*"
            else:
                NarrativeCache._validate_parts(
                    report_id="x",
                    tenant_id=tenant_id,
                    user_id="x",
                    model_version="x",
                    insights_hash="x",
                )
                entries_pattern = f"{self.prefix}:narrative:{tenant_id}:*"
                zset_pattern = f"{self.prefix}:zset:{tenant_id}:*"
            return self._delete_matching(entries_pattern) + self._delete_matching(zset_pattern)

    def stats(self) -> dict[str, Any]:
        """Return in-process hit/miss/eviction counters.

        ``size`` is reported as ``-1`` because a precise shard size would
        require a SCAN (deferred to manual ops); callers can monitor
        ``hits``/``misses`` for the cache utility signal they actually need.
        """
        with self._lock:
            requests = self._hits + self._misses
            hit_rate = self._hits / requests if requests else 0.0
            return {
                "size": -1,
                "max_entries": self.max_entries,
                "ttl_seconds": self.ttl_seconds,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": hit_rate,
                "eviction_count": self._eviction_count,
                "url": self._sanitize_url(self._url),
            }

    def close(self) -> None:
        """Close the underlying Redis connection. Idempotent."""
        with self._lock:
            if self._closed:
                return
            try:
                self._client.close()
            finally:
                self._closed = True

    # ============================================================
    # Private helpers
    # ============================================================

    def _evict_lru(self, tenant_id: str, user_id: str) -> None:
        zset_key = self._zset_key(tenant_id, user_id)
        size = int(self._client.zcard(zset_key))
        if size <= self.max_entries:
            return
        overflow = size - self.max_entries
        # ``decode_responses=True`` on the client returns str, but redis-py
        # stubs do not propagate the flag through .zrange(); coerce explicitly.
        oldest: list[str] = [str(k) for k in self._client.zrange(zset_key, 0, overflow - 1)]
        if not oldest:
            return
        pipe = self._client.pipeline()
        for cache_key in oldest:
            pipe.delete(self._entry_key(tenant_id, user_id, cache_key))
        pipe.zrem(zset_key, *oldest)
        pipe.execute()
        self._eviction_count += len(oldest)

    def _entry_key(self, tenant_id: str, user_id: str, cache_key: str) -> str:
        return f"{self.prefix}:narrative:{tenant_id}:{user_id}:{cache_key}"

    def _zset_key(self, tenant_id: str, user_id: str) -> str:
        return f"{self.prefix}:zset:{tenant_id}:{user_id}"

    def _delete_matching(self, pattern: str) -> int:
        """Delete every key matching ``pattern`` via SCAN (non-blocking)."""
        deleted = 0
        for key in self._client.scan_iter(match=pattern, count=200):
            self._client.delete(key)
            deleted += 1
        return deleted

    @staticmethod
    def _sanitize_url(url: str) -> str:
        """Redact password from ``redis://:password@host:port`` for logs."""
        return re.sub(r":[^:@]*@", ":***@", url)


__all__ = [
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_PREFIX",
    "DEFAULT_TTL_SECONDS",
    "RedisCache",
]
