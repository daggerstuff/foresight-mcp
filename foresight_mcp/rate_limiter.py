"""
Rate Limiter for Multi-Tenant Isolation
Token bucket algorithm for per-tenant rate limiting.
"""
from __future__ import annotations
import time
import threading
from dataclasses import dataclass, field
from typing import Dict, Optional
from collections import defaultdict


class RateLimitExceeded(Exception):
    """Raised when rate limit is exceeded."""
    def __init__(self, remaining: int, reset_time: float):
        self.remaining = remaining
        self.reset_time = reset_time
        super().__init__(f"Rate limit exceeded. Remaining: {remaining}, Reset: {reset_time}")


@dataclass
class TokenBucket:
    """Token bucket for rate limiting."""
    tokens: float
    last_update: float
    rate: float  # tokens per second
    burst: float  # max tokens (burst limit)


@dataclass
class RateLimiter:
    """
    Per-tenant rate limiter using token bucket algorithm.

    Each tenant gets:
    - Fixed rate limit (requests per minute)
    - Burst limit (max requests in short period)
    - Automatic token regeneration
    """
    rate_limit: int = 100  # requests per minute
    burst_limit: int = 20  # burst requests
    _buckets: Dict[str, TokenBucket] = field(default_factory=dict)

    def __post_init__(self):
        if not hasattr(self, '_buckets'):
            self._buckets = {}
        self._lock = threading.Lock()

    def _get_bucket(self, tenant_id: str, rate_limit: Optional[int] = None, burst_limit: Optional[int] = None) -> TokenBucket:
        """Get or create token bucket for tenant."""
        if tenant_id not in self._buckets:
            rl = rate_limit if rate_limit is not None else self.rate_limit
            bl = burst_limit if burst_limit is not None else self.burst_limit
            self._buckets[tenant_id] = TokenBucket(
                tokens=bl,
                last_update=time.time(),
                rate=rl / 60.0,
                burst=bl
            )
        return self._buckets[tenant_id]

    def acquire(self, tenant_id: str, tokens: int = 1, rate_limit: Optional[int] = None, burst_limit: Optional[int] = None) -> bool:
        """
        Acquire tokens from tenant's bucket.

        Returns True if successful, False if rate limited.
        """
        with self._lock:
            bucket = self._get_bucket(tenant_id, rate_limit, burst_limit)
            now = time.time()

            # Regenerate tokens based on time elapsed
            elapsed = now - bucket.last_update
            bucket.tokens = min(
                bucket.burst,
                bucket.tokens + elapsed * bucket.rate
            )
            bucket.last_update = now

            # Check if we have enough tokens
            if bucket.tokens >= tokens:
                bucket.tokens -= tokens
                return True
            return False

    def get_remaining(self, tenant_id: str) -> int:
        """Get remaining tokens for tenant."""
        with self._lock:
            bucket = self._get_bucket(tenant_id)
            now = time.time()

            # Calculate current tokens
            elapsed = now - bucket.last_update
            current = min(
                bucket.burst,
                bucket.tokens + elapsed * bucket.rate
            )
            return int(current)

    def reset(self, tenant_id: str) -> None:
        """Reset tenant's rate limit bucket."""
        with self._lock:
            if tenant_id in self._buckets:
                del self._buckets[tenant_id]


# Global rate limiter instance (thread-safe)
_rate_limiter: Optional[RateLimiter] = None
_rate_limiter_lock = threading.Lock()


def get_rate_limiter() -> RateLimiter:
    """Get the global rate limiter instance (thread-safe)."""
    global _rate_limiter
    with _rate_limiter_lock:
        if _rate_limiter is None:
            _rate_limiter = RateLimiter()
        return _rate_limiter


def reset_rate_limiter() -> None:
    """Reset the global rate limiter (for testing)."""
    global _rate_limiter
    with _rate_limiter_lock:
        _rate_limiter = None
