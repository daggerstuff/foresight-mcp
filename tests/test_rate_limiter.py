"""Tests for rate limiter and middleware integration."""
import pytest
from foresight_mcp.rate_limiter import (
    RateLimiter,
    RateLimitExceeded,
    get_rate_limiter,
    reset_rate_limiter,
)


@pytest.fixture(autouse=True)
def cleanup():
    reset_rate_limiter()
    yield
    reset_rate_limiter()


class TestTokenBucket:
    """Test token bucket algorithm."""

    def test_allows_within_burst(self):
        limiter = RateLimiter(rate_limit=100, burst_limit=5)
        for _ in range(5):
            assert limiter.acquire("tenant_a")

    def test_rejects_over_burst(self):
        limiter = RateLimiter(rate_limit=100, burst_limit=5)
        for _ in range(5):
            limiter.acquire("tenant_a")
        assert not limiter.acquire("tenant_a")

    def test_tenants_independent(self):
        limiter = RateLimiter(rate_limit=100, burst_limit=5)
        for _ in range(5):
            limiter.acquire("tenant_a")
        # tenant_b has its own bucket
        assert limiter.acquire("tenant_b")

    def test_reset_clears_bucket(self):
        limiter = RateLimiter(rate_limit=100, burst_limit=5)
        for _ in range(5):
            limiter.acquire("tenant_a")
        limiter.reset("tenant_a")
        assert limiter.acquire("tenant_a")

    def test_get_remaining(self):
        limiter = RateLimiter(rate_limit=100, burst_limit=20)
        for _ in range(5):
            limiter.acquire("tenant_a")
        assert limiter.get_remaining("tenant_a") == 15

    def test_custom_per_tenant_limits(self):
        limiter = RateLimiter(rate_limit=100, burst_limit=20)
        # Custom tenant with burst_limit=2
        assert limiter.acquire("low_tenant", burst_limit=2)
        assert limiter.acquire("low_tenant", burst_limit=2)
        assert not limiter.acquire("low_tenant", burst_limit=2)

    def test_thread_safe_singleton(self):
        limiter1 = get_rate_limiter()
        limiter2 = get_rate_limiter()
        assert limiter1 is limiter2


class TestRateLimitExceeded:
    """Test exception structure."""

    def test_exception_attributes(self):
        exc = RateLimitExceeded(remaining=0, reset_time=123.45)
        assert exc.remaining == 0
        assert exc.reset_time == 123.45
        assert "Rate limit exceeded" in str(exc)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
