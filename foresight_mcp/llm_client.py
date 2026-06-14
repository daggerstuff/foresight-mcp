"""Tenant-isolated LLM client with audit logging and per-tenant overrides.

This module provides a HIPAA-grade LLM client that:
1. Enforces tenant isolation on every call
2. Emits audit log rows with prompt/response hashes (never the raw content)
3. Supports per-tenant API key overrides via environment variables
4. Provides graceful "no LLM configured" failures (not generic exceptions)
5. Supports configurable retry with exponential backoff and jitter

Environment variables (per-tenant override wins over global):

  FORESIGHT_LLM_PROVIDER       -- "anthropic" (default) or "openai"
  FORESIGHT_LLM_MODEL          -- model identifier (provider-specific default)
  FORESIGHT_LLM_API_KEY        -- global API key (fallback if no per-tenant key)
  FORESIGHT_LLM_MAX_RETRIES    -- max retries on failure (default: 2)
  FORESIGHT_LLM_TIMEOUT_MS      -- request timeout in ms (default: 60000)
  FORESIGHT_LLM_TENANT_<TENANT_ID>_API_KEY  -- per-tenant API key override

For example, FORESIGHT_LLM_TENANT_acme_API_KEY=sk-ant-... gives tenant "acme"
its own key, which wins over the global ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .llm_errors import LLMError, LLMNotConfiguredError, LLMProviderError, LLMRateLimitError
from .llm_providers.anthropic import AnthropicClient
from .llm_providers.openai import OpenAIClient
from .rate_limiter import get_rate_limiter, get_request_throttler

__all__ = [
    "LLMClient",
    "LLMError",
    "LLMNotConfiguredError",
    "LLMProviderError",
    "LLMRateLimitError",
    "TenantLLMClient",
    "default_llm_call",
    "get_default_client",
]

logger = logging.getLogger("foresight_llm_client")

# ----------------------------------------------------------------------
# Audit helpers
# ----------------------------------------------------------------------


def _hash_payload(payload: str) -> str:
    """SHA-256 hex digest truncated to 16 chars for log readability."""
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ----------------------------------------------------------------------
# Config dataclass
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class LLMConfig:
    """Tenant-isolated LLM configuration.

    Immutable. All fields are optional so callers can incrementally override.
    """

    provider: str = "anthropic"
    model_version: str = ""
    api_key: str = ""
    max_retries: int = 2
    timeout_ms: int = 60_000
    tenant_id_override: str = ""  # used in test mode to bypass audit

    @classmethod
    def from_env(cls, tenant_id: str) -> LLMConfig:
        """Build config by resolving per-tenant overrides, then global env vars.

        Per-tenant override format: FORESIGHT_LLM_TENANT_<TENANT_ID>_API_KEY
        """
        provider = os.environ.get("FORESIGHT_LLM_PROVIDER", "anthropic").strip().lower()
        model_version = os.environ.get("FORESIGHT_LLM_MODEL", "").strip()
        max_retries = int(os.environ.get("FORESIGHT_LLM_MAX_RETRIES", "2").strip())
        timeout_ms = int(os.environ.get("FORESIGHT_LLM_TIMEOUT_MS", "60000").strip())
        tenant_id_override = os.environ.get("FORESIGHT_LLM_TENANT_OVERRIDE", "").strip()

        # Per-tenant API key wins; fall back to global
        tenant_key_env = f"FORESIGHT_LLM_TENANT_{tenant_id.upper()}_API_KEY"
        per_tenant_key = os.environ.get(tenant_key_env, "").strip()
        global_key = os.environ.get("FORESIGHT_LLM_API_KEY", "").strip()
        api_key = per_tenant_key or global_key

        # Provider-specific key fallbacks
        if not api_key:
            if provider == "anthropic":
                api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
            elif provider == "openai":
                api_key = os.environ.get("OPENAI_API_KEY", "").strip()

        return cls(
            provider=provider,
            model_version=model_version,
            api_key=api_key,
            max_retries=max_retries,
            timeout_ms=timeout_ms,
            tenant_id_override=tenant_id_override,
        )

    def model_for_provider(self) -> str:
        """Return the model identifier for the configured provider."""
        if self.model_version:
            return self.model_version
        if self.provider == "anthropic":
            return "claude-3-5-sonnet-latest"
        if self.provider == "openai":
            return "gpt-4o-mini"
        return ""


# ----------------------------------------------------------------------
# LLMClient protocol (minimal interface every adapter must implement)
# ----------------------------------------------------------------------


class LLMClient(Protocol):
    """Minimal interface every provider adapter must implement."""

    provider: str
    model: str

    def complete(self, prompt: str, *, max_tokens: int = 1024) -> str:
        """Return the model's text response for ``prompt``."""
        ...


# ----------------------------------------------------------------------
# Retry helper
# ----------------------------------------------------------------------


def _retry_with_backoff(
    fn: Callable[[], str],
    max_retries: int,
    timeout_ms: int = 0,
) -> str:
    """Call ``fn`` with exponential backoff and jitter on :exc:`LLMProviderError`.

    If *timeout_ms* > 0, each individual call is wrapped in a
    :class:`socket.timeout` guard so a single hung request cannot
    block the process indefinitely.
    """
    last_exc: LLMError | None = None
    for attempt in range(max_retries + 1):
        try:
            if timeout_ms > 0:
                original_timeout = socket.getdefaulttimeout()
                try:
                    socket.setdefaulttimeout(timeout_ms / 1000.0)
                    result = fn()
                finally:
                    socket.setdefaulttimeout(original_timeout)
                return result
            return fn()
        except LLMRateLimitError:
            raise  # Do not retry rate limits
        except LLMNotConfiguredError:
            raise  # Config errors propagate immediately without wrapping
        except (socket.timeout, TimeoutError) as exc:
            last_exc = LLMProviderError(f"LLM call timed out after {timeout_ms}ms: {exc}")
            if attempt < max_retries:
                sleep_s = (2**attempt) + random.random()
                logger.warning(
                    "LLM call timed out (attempt %d/%d), retrying in %.2fs", attempt + 1, max_retries + 1, sleep_s
                )
                time.sleep(sleep_s)
            else:
                break
        except LLMProviderError as exc:
            last_exc = exc
            if attempt < max_retries:
                sleep_s = (2**attempt) + random.random()
                logger.warning(
                    "LLM call failed (attempt %d/%d), retrying in %.2fs: %s", attempt + 1, max_retries + 1, sleep_s, exc
                )
                time.sleep(sleep_s)
            else:
                break
        except LLMError as exc:
            last_exc = exc
            break

    raise LLMProviderError(f"LLM call failed after {max_retries + 1} attempt(s): {last_exc}") from last_exc


# ----------------------------------------------------------------------
# TenantLLMClient
# ----------------------------------------------------------------------


class TenantLLMClient:
    """Immutable, tenant-isolated LLM client.

    All calls route through :meth:`generate`, which:
    1. Resolves the correct API key (per-tenant override > global env > provider fallback)
    2. Builds the provider adapter
    3. Calls with configurable retry
    4. Emits an audit log row with tenant_id, user_id, prompt_hash, latency_ms, outcome

    For test mode (tenant_id_override is set), audit logging is bypassed.
    """

    __slots__ = ("_config", "_tenant_id")

    def __init__(self, config: LLMConfig, tenant_id: str) -> None:
        object.__setattr__(self, "_config", config)
        object.__setattr__(self, "_tenant_id", tenant_id)

    @classmethod
    def from_env(cls, tenant_id: str) -> TenantLLMClient:
        """Build a TenantLLMClient from environment variables."""
        return cls(LLMConfig.from_env(tenant_id), tenant_id)

    @property
    def provider(self) -> str:
        return self._config.provider

    @property
    def model(self) -> str:
        return self._config.model_for_provider()

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    def _build_provider(self) -> LLMClient:
        """Build and return the provider adapter for the current config."""
        api_key = self._config.api_key
        model = self._config.model_for_provider()
        provider = self._config.provider
        timeout_s = self._config.timeout_ms // 1000

        if not api_key:
            raise LLMNotConfiguredError(
                f"No API key configured for provider '{provider}'. "
                f"Set FORESIGHT_LLM_API_KEY or FORESIGHT_LLM_TENANT_{self._tenant_id.upper()}_API_KEY. "
                f"Alternatively, set ANTHROPIC_API_KEY (anthropic) or OPENAI_API_KEY (openai)."
            )

        if provider == "anthropic":
            return AnthropicClient(api_key=api_key, model=model, timeout=timeout_s)
        if provider == "openai":
            return OpenAIClient(api_key=api_key, model=model, timeout=timeout_s)

        raise LLMProviderError(
            f"Unknown LLM provider '{provider}'. Set FORESIGHT_LLM_PROVIDER to 'anthropic' or 'openai'."
        )

    def _emit_audit(
        self,
        user_id: str,
        prompt_hash: str,
        response_hash: str | None,
        latency_ms: float,
        outcome: str,
        error_type: str | None = None,
    ) -> None:
        """Emit an audit event (bypassed in test mode when tenant_id_override is set)."""
        if self._config.tenant_id_override:
            # Test mode: skip audit
            logger.debug(
                "[TEST MODE] tenant=%s user=%s provider=%s model=%s outcome=%s latency_ms=%.1f",
                self._tenant_id,
                user_id,
                self.provider,
                self.model,
                outcome,
                latency_ms,
            )
            return

        logger.info(
            "[AUDIT] tenant=%s user=%s provider=%s model=%s prompt_hash=%s response_hash=%s latency_ms=%.1f outcome=%s error=%s",
            self._tenant_id,
            user_id,
            self.provider,
            self.model,
            prompt_hash,
            response_hash or "",
            latency_ms,
            outcome,
            error_type or "",
        )

    def _truncate_prompt(self, prompt: str) -> str:
        max_chars = int(os.environ.get("FORESIGHT_LLM_MAX_PROMPT_CHARS", "10000"))
        if len(prompt) > max_chars:
            logger.warning(
                "Prompt truncated from %d to %d chars (set FORESIGHT_LLM_MAX_PROMPT_CHARS to adjust)",
                len(prompt),
                max_chars,
            )
            return prompt[:max_chars]
        return prompt

    def generate(
        self,
        prompt: str,
        *,
        user_id: str = "",
        max_tokens: int = 1024,
    ) -> str:
        """Generate a response using the configured provider (tenant-isolated, audited).

        Args:
            prompt: The prompt to send to the LLM.
            user_id: The user making the request (used in audit logs).
            max_tokens: Maximum tokens to generate (default: 1024).

        Returns:
            The model's text response.

        Raises:
            LLMNotConfiguredError: If no API key is configured.
            LLMProviderError: If the provider call fails after retries.
            LLMRateLimitError: If the provider rate-limits and retries are exhausted.
        """
        if not prompt or not isinstance(prompt, str):
            raise LLMProviderError("prompt must be a non-empty string")

        prompt = self._truncate_prompt(prompt)

        rate_limiter = get_rate_limiter()
        throttler = get_request_throttler()

        if not rate_limiter.acquire(self._tenant_id):
            raise LLMRateLimitError(
                f"Rate limited for tenant '{self._tenant_id}': remaining={rate_limiter.get_remaining(self._tenant_id)}"
            )

        throttler.throttle(key=self._tenant_id)

        prompt_hash = _hash_payload(prompt)
        start = time.perf_counter()
        outcome = "error"
        response_hash: str | None = None
        error_type: str | None = None

        try:

            def call() -> str:
                provider = self._build_provider()
                return provider.complete(prompt, max_tokens=max_tokens)

            response = _retry_with_backoff(
                call,
                max_retries=self._config.max_retries,
                timeout_ms=self._config.timeout_ms,
            )

            outcome = "success"
            response_hash = _hash_payload(response)
            return response

        except LLMRateLimitError:
            outcome = "rate_limited"
            error_type = "rate_limit"
            raise

        except LLMNotConfiguredError:
            outcome = "not_configured"
            error_type = "not_configured"
            raise

        except LLMError as exc:
            outcome = "error"
            error_type = type(exc).__name__
            raise

        finally:
            latency_ms = (time.perf_counter() - start) * 1000.0
            self._emit_audit(
                user_id=user_id,
                prompt_hash=prompt_hash,
                response_hash=response_hash,
                latency_ms=latency_ms,
                outcome=outcome,
                error_type=error_type,
            )


# ----------------------------------------------------------------------
# Backward-compatible factory and default callable
# ----------------------------------------------------------------------


def get_default_client() -> LLMClient:
    """Build the default :class:`LLMClient` from environment variables.

    Deprecated: Prefer :class:`TenantLLMClient` for tenant-isolated access.
    """
    provider = os.environ.get("FORESIGHT_LLM_PROVIDER", "anthropic").strip().lower()
    if provider == "anthropic":
        return AnthropicClient.from_env()
    if provider == "openai":
        return OpenAIClient.from_env()
    raise LLMError(f"Unknown LLM provider '{provider}'. Set FORESIGHT_LLM_PROVIDER to 'anthropic' or 'openai'.")


def default_llm_call(prompt: str, tenant_id: str, user_id: str) -> str:
    """Default LLM callable matching :data:`LLMCallable`.

    Uses :class:`TenantLLMClient` to enforce tenant isolation and emit audit logs.
    The ``tenant_id`` and ``user_id`` arguments are forwarded to the audit log.

    Raises:
        LLMNotConfiguredError: If no API key is configured for the provider.
        LLMProviderError: If the provider call fails.
    """
    return TenantLLMClient.from_env(tenant_id).generate(prompt, user_id=user_id)
