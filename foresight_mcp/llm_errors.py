"""LLM-related error types.

Extracted to a leaf module so provider adapters and the base client
can both import :class:`LLMError` without creating a circular dependency.
"""

from __future__ import annotations


class LLMError(RuntimeError):
    """Raised when an LLM call fails. Carries provider context for diagnostics."""


class LLMNotConfiguredError(LLMError):
    """Raised when no LLM provider is configured (graceful failure, NOT a generic Exception)."""


class LLMProviderError(LLMError):
    """Raised when the upstream LLM provider returns an error (HTTP failure, bad response, etc.)."""


class LLMRateLimitError(LLMError):
    """Raised when the LLM provider rate-limits the request."""


__all__ = ["LLMError", "LLMNotConfiguredError", "LLMProviderError", "LLMRateLimitError"]
