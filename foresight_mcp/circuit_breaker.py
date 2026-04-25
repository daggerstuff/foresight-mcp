"""Circuit Breaker pattern implementation for preventing cascading failures."""
import time
import threading
from enum import Enum
from typing import Callable, Any, Optional
from dataclasses import dataclass
from functools import wraps


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing recovery


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker."""
    failure_threshold: int = 5        # Failures before opening
    recovery_timeout: float = 30.0    # Seconds before half-open
    half_open_max_calls: int = 3      # Successful calls needed to close
    expected_exceptions: tuple = (Exception,)


class CircuitBreakerOpenError(Exception):
    """Raised when circuit breaker is open."""
    pass


class CircuitBreaker:
    """Circuit breaker for protecting against cascading failures."""

    def __init__(self, config: CircuitBreakerConfig = None):
        self.config = config or CircuitBreakerConfig()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: Optional[float] = None
        self._lock = threading.RLock()

        # Metrics
        self.total_requests = 0
        self.total_failures = 0
        self.total_rejected = 0

    @property
    def state(self) -> CircuitState:
        """Get current circuit state."""
        with self._lock:
            return self._check_state_transition()

    def _check_state_transition(self) -> CircuitState:
        """Check if state should transition based on timeout."""
        if self._state == CircuitState.OPEN and self._last_failure_time:
            elapsed = time.time() - self._last_failure_time
            if elapsed >= self.config.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                self._failure_count = 0
        return self._state

    def call(self, func: Callable, *args, **kwargs) -> Any:
        """Execute function with circuit breaker protection."""
        with self._lock:
            current_state = self._check_state_transition()
            self.total_requests += 1

            if current_state == CircuitState.OPEN:
                self.total_rejected += 1
                raise CircuitBreakerOpenError("Circuit breaker is OPEN")

            if current_state == CircuitState.HALF_OPEN:
                if self._success_count >= self.config.half_open_max_calls:
                    pass  # Continue but track

        # Execute outside lock
        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except self.config.expected_exceptions as e:
            self._on_failure()
            raise

    def _on_success(self):
        """Handle successful call."""
        with self._lock:
            self._success_count += 1

            if self._state == CircuitState.HALF_OPEN:
                if self._success_count >= self.config.half_open_max_calls:
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    self._success_count = 0
            elif self._state == CircuitState.CLOSED:
                self._failure_count = max(0, self._failure_count - 1)

    def _on_failure(self):
        """Handle failed call."""
        with self._lock:
            self._failure_count += 1
            self.total_failures += 1
            self._last_failure_time = time.time()

            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                self._success_count = 0
            elif self._state == CircuitState.CLOSED:
                if self._failure_count >= self.config.failure_threshold:
                    self._state = CircuitState.OPEN

    def reset(self):
        """Reset circuit breaker to initial state."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self._last_failure_time = None


def with_circuit_breaker(config: CircuitBreakerConfig = None):
    """Decorator for circuit breaker protection."""
    breaker = CircuitBreaker(config)

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            return breaker.call(func, *args, **kwargs)
        wrapper.circuit_breaker = breaker
        return wrapper
    return decorator
