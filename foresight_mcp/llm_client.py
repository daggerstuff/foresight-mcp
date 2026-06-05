"""Tenant-isolated LLM client base class and provider factory.

The ``foresight-mcp`` package does not bundle an LLM SDK. The adapters
under ``foresight_mcp.llm_providers`` use only the standard library so
no new top-level dependencies are introduced.

Provider selection is driven by environment variables. Callers can also
instantiate a specific provider directly.

Environment variables:

* ``FORESIGHT_LLM_PROVIDER`` -- ``"anthropic"`` (default) or ``"openai"``
* ``FORESIGHT_LLM_MODEL`` -- model identifier (provider-specific default)
* ``ANTHROPIC_API_KEY`` -- required for the Anthropic provider
* ``OPENAI_API_KEY`` -- required for the OpenAI provider
"""

from __future__ import annotations

import os
from typing import Protocol

from .llm_errors import LLMError
from .llm_providers.anthropic import AnthropicClient
from .llm_providers.openai import OpenAIClient

__all__ = ["LLMClient", "LLMError", "default_llm_call", "get_default_client"]


class LLMClient(Protocol):
    """Minimal interface every provider adapter must implement."""

    provider: str
    model: str

    def complete(self, prompt: str, *, max_tokens: int = 1024) -> str:
        """Return the model's text response for ``prompt``.

        Raises:
            LLMError: If the provider returns a non-2xx status, the
                response body is malformed, or the configured API key is
                missing.
        """
        ...


def get_default_client() -> LLMClient:
    """Build the default :class:`LLMClient` from environment variables.

    Raises:
        LLMError: If the configured provider is unknown or its API key
            is missing.
    """
    provider = os.environ.get("FORESIGHT_LLM_PROVIDER", "anthropic").strip().lower()

    if provider == "anthropic":
        return AnthropicClient.from_env()

    if provider == "openai":
        return OpenAIClient.from_env()

    raise LLMError(
        f"Unknown LLM provider '{provider}'. "
        f"Set FORESIGHT_LLM_PROVIDER to 'anthropic' or 'openai'."
    )


def default_llm_call(prompt: str, tenant_id: str, user_id: str) -> str:
    """Default LLM callable matching :data:`LLMCallable`.

    Builds the default client from environment variables and delegates
    to its :meth:`LLMClient.complete` method. The ``tenant_id`` and
    ``user_id`` arguments are accepted for signature compatibility with
    :func:`foresight_mcp.reflection_narrative.generate_insight_narrative`
    but are not forwarded to the upstream provider — the provider is
    not aware of the foresight tenant model. Tenant isolation is
    enforced at the memory layer (cache keys, audit rows) and at the
    network boundary (the caller's gateway, not the model itself).

    Raises:
        LLMError: If the provider is misconfigured or the upstream call
            fails. Propagates the :class:`foresight_mcp.reflection_narrative.ReflectionNarrativeError`
            contract used by the narrative pipeline.
    """
    _ = (tenant_id, user_id)
    return get_default_client().complete(prompt)
