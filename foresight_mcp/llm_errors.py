"""LLM-related error types.

Extracted to a leaf module so provider adapters and the base client
can both import :class:`LLMError` without creating a circular dependency.
"""

from __future__ import annotations


class LLMError(RuntimeError):
    """Raised when an LLM call fails. Carries provider context for diagnostics."""


__all__ = ["LLMError"]
