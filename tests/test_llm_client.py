"""Tests for the LLM client base, factory, and provider adapters."""

from __future__ import annotations

import io
import json
import logging as stdlib_logging
import sys
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from foresight_mcp.llm_client import (
    LLMConfig,
    LLMError,
    LLMNotConfiguredError,
    LLMProviderError,
    LLMRateLimitError,
    TenantLLMClient,
    default_llm_call,
    get_default_client,
)
from foresight_mcp.llm_providers.anthropic import AnthropicClient
from foresight_mcp.llm_providers.openai import OpenAIClient


def _header_lookup(headers: Any, key: str) -> str:
    lowered = key.lower()
    for actual, value in headers.items():
        if actual.lower() == lowered:
            return value
    raise KeyError(key)


# ============================================================
# Factory + env
# ============================================================


def test_get_default_client_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORESIGHT_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = get_default_client()
    assert isinstance(client, AnthropicClient)
    assert client.provider == "anthropic"
    assert client.model == AnthropicClient.DEFAULT_MODEL if hasattr(AnthropicClient, "DEFAULT_MODEL") else client.model
    assert client._api_key == "test-anthropic-key"


def test_get_default_client_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORESIGHT_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = get_default_client()
    assert isinstance(client, OpenAIClient)
    assert client.provider == "openai"
    assert client._api_key == "test-openai-key"


def test_get_default_client_unknown_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORESIGHT_LLM_PROVIDER", "bogus")
    with pytest.raises(LLMError, match="Unknown LLM provider"):
        get_default_client()


def test_get_default_client_anthropic_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORESIGHT_LLM_PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMError, match="Anthropic API key is required"):
        get_default_client()


def test_get_default_client_openai_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORESIGHT_LLM_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(LLMError, match="OpenAI API key is required"):
        get_default_client()


def test_default_llm_call_delegates_to_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORESIGHT_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    fake_client = MagicMock()
    fake_client.complete.return_value = "narrative text"

    with patch.object(TenantLLMClient, "_build_provider", return_value=fake_client):
        out = default_llm_call("prompt", "tenant-1", "user-1")
    assert out == "narrative text"
    fake_client.complete.assert_called_once_with("prompt", max_tokens=1024)


def test_default_llm_call_propagates_llm_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORESIGHT_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    fake_client = MagicMock()
    fake_client.complete.side_effect = LLMError("upstream 500")
    with (
        patch.object(TenantLLMClient, "_build_provider", return_value=fake_client),
        pytest.raises(LLMError, match="upstream 500"),
    ):
        default_llm_call("p", "t", "u")


# ============================================================
# Anthropic adapter
# ============================================================


def _make_urlopen_response(payload: dict[str, Any]) -> MagicMock:
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode("utf-8")
    response.__enter__ = MagicMock(return_value=response)
    response.__exit__ = MagicMock(return_value=False)
    return response


def test_anthropic_client_complete_success() -> None:
    client = AnthropicClient(api_key="test-key", model="claude-test")
    payload = {
        "id": "msg_123",
        "content": [{"type": "text", "text": "hello world"}],
    }
    with patch("urllib.request.urlopen", return_value=_make_urlopen_response(payload)) as mock_urlopen:
        out = client.complete("summarize this")
    assert out == "hello world"
    sent_request = mock_urlopen.call_args.args[0]
    assert _header_lookup(sent_request.headers, "x-api-key") == "test-key"
    assert _header_lookup(sent_request.headers, "anthropic-version") == "2023-06-01"
    body = json.loads(sent_request.data.decode("utf-8"))
    assert body["model"] == "claude-test"
    assert body["max_tokens"] == 1024
    assert body["messages"] == [{"role": "user", "content": "summarize this"}]


def test_anthropic_client_rejects_empty_prompt() -> None:
    client = AnthropicClient(api_key="test-key")
    with pytest.raises(LLMError, match="prompt must be a non-empty string"):
        client.complete("")


def test_anthropic_client_rejects_zero_max_tokens() -> None:
    client = AnthropicClient(api_key="test-key")
    with pytest.raises(LLMError, match="max_tokens must be at least 1"):
        client.complete("p", max_tokens=0)


def test_anthropic_client_rejects_missing_api_key() -> None:
    with pytest.raises(LLMError, match="Anthropic API key is required"):
        AnthropicClient(api_key="")


def test_anthropic_client_handles_http_error() -> None:
    client = AnthropicClient(api_key="test-key")
    http_err = urllib.error.HTTPError(
        url="https://api.anthropic.com/v1/messages",
        code=500,
        msg="Internal Server Error",
        hdrs=MagicMock(),
        fp=io.BytesIO(b"internal error"),
    )
    with (
        patch("urllib.request.urlopen", side_effect=http_err),
        pytest.raises(LLMError, match="Anthropic API returned HTTP 500"),
    ):
        client.complete("p")


def test_anthropic_client_handles_rate_limit() -> None:
    client = AnthropicClient(api_key="test-key")
    http_err = urllib.error.HTTPError(
        url="https://api.anthropic.com/v1/messages",
        code=429,
        msg="Too Many Requests",
        hdrs=MagicMock(),
        fp=io.BytesIO(b"rate limit"),
    )
    with (
        patch("urllib.request.urlopen", side_effect=http_err),
        pytest.raises(LLMRateLimitError, match="Anthropic API rate limit \\(429\\)"),
    ):
        client.complete("p")


def test_anthropic_client_handles_empty_content() -> None:
    client = AnthropicClient(api_key="test-key")
    payload = {"id": "msg_123", "content": []}
    with (
        patch("urllib.request.urlopen", return_value=_make_urlopen_response(payload)),
        pytest.raises(LLMError, match="no content blocks"),
    ):
        client.complete("p")


# ============================================================
# OpenAI adapter
# ============================================================


def test_openai_client_complete_success() -> None:
    client = OpenAIClient(api_key="test-key", model="gpt-test")
    payload = {
        "id": "chatcmpl-123",
        "choices": [{"message": {"role": "assistant", "content": "hi from openai"}}],
    }
    with patch("urllib.request.urlopen", return_value=_make_urlopen_response(payload)) as mock_urlopen:
        out = client.complete("summarize this")
    assert out == "hi from openai"
    sent_request = mock_urlopen.call_args.args[0]
    assert _header_lookup(sent_request.headers, "Authorization") == "Bearer test-key"
    body = json.loads(sent_request.data.decode("utf-8"))
    assert body["model"] == "gpt-test"
    assert body["max_tokens"] == 1024
    assert body["messages"] == [{"role": "user", "content": "summarize this"}]


def test_openai_client_rejects_empty_prompt() -> None:
    client = OpenAIClient(api_key="test-key")
    with pytest.raises(LLMError, match="prompt must be a non-empty string"):
        client.complete("")


def test_openai_client_rejects_zero_max_tokens() -> None:
    client = OpenAIClient(api_key="test-key")
    with pytest.raises(LLMError, match="max_tokens must be at least 1"):
        client.complete("p", max_tokens=0)


def test_openai_client_rejects_missing_api_key() -> None:
    with pytest.raises(LLMError, match="OpenAI API key is required"):
        OpenAIClient(api_key="")


def test_openai_client_handles_http_error() -> None:
    client = OpenAIClient(api_key="test-key")
    http_err = urllib.error.HTTPError(
        url="https://api.openai.com/v1/chat/completions",
        code=500,
        msg="Internal Server Error",
        hdrs=MagicMock(),
        fp=io.BytesIO(b"internal error"),
    )
    with (
        patch("urllib.request.urlopen", side_effect=http_err),
        pytest.raises(LLMError, match="OpenAI API returned HTTP 500"),
    ):
        client.complete("p")


def test_openai_client_handles_rate_limit() -> None:
    client = OpenAIClient(api_key="test-key")
    http_err = urllib.error.HTTPError(
        url="https://api.openai.com/v1/chat/completions",
        code=429,
        msg="Too Many Requests",
        hdrs=MagicMock(),
        fp=io.BytesIO(b"rate limit"),
    )
    with (
        patch("urllib.request.urlopen", side_effect=http_err),
        pytest.raises(LLMRateLimitError, match="OpenAI API rate limit \\(429\\)"),
    ):
        client.complete("p")


def test_openai_client_handles_empty_choices() -> None:
    client = OpenAIClient(api_key="test-key")
    payload = {"id": "chatcmpl-123", "choices": []}
    with (
        patch("urllib.request.urlopen", return_value=_make_urlopen_response(payload)),
        pytest.raises(LLMError, match="no choices"),
    ):
        client.complete("p")
    # =================================================================
    # TenantLLMClient tests
    # =================================================================


def test_tenant_llm_client_from_env_global_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """TenantLLMClient reads global env vars when no per-tenant override exists."""
    monkeypatch.setenv("FORESIGHT_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "global-key")
    monkeypatch.setenv("FORESIGHT_LLM_MODEL", "claude-test-model")
    monkeypatch.setenv("FORESIGHT_LLM_MAX_RETRIES", "3")
    monkeypatch.setenv("FORESIGHT_LLM_TIMEOUT_MS", "30000")

    client = TenantLLMClient.from_env("tenant-1")
    assert client.tenant_id == "tenant-1"
    assert client.provider == "anthropic"
    assert client.model == "claude-test-model"

    fake = MagicMock()
    fake.complete.return_value = "response"
    with patch.object(TenantLLMClient, "_build_provider", return_value=fake):
        result = client.generate("hello", user_id="user-1")
    assert result == "response"
    fake.complete.assert_called_once_with("hello", max_tokens=1024)


def test_tenant_llm_client_per_tenant_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-tenant API key takes precedence over global key."""
    monkeypatch.setenv("FORESIGHT_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "global-key")
    monkeypatch.setenv("FORESIGHT_LLM_TENANT_ACME_API_KEY", "acme-override-key")
    monkeypatch.setenv("FORESIGHT_LLM_MODEL", "claude-global")

    # For tenant "acme", the per-tenant key should win
    config = LLMConfig.from_env("acme")
    assert config.api_key == "acme-override-key"

    # For tenant "other", global key is used
    config_other = LLMConfig.from_env("other")
    assert config_other.api_key == "global-key"


def test_tenant_llm_client_missing_config_raises_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing API key raises LLMNotConfiguredError (NOT a generic Exception)."""
    monkeypatch.setenv("FORESIGHT_LLM_PROVIDER", "anthropic")
    # No ANTHROPIC_API_KEY or FORESIGHT_LLM_API_KEY set
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("FORESIGHT_LLM_API_KEY", raising=False)

    client = TenantLLMClient.from_env("tenant-1")
    # _build_provider() raises LLMNotConfiguredError when no API key
    with pytest.raises(LLMNotConfiguredError, match="No API key configured"):
        client.generate("hello", user_id="u1")


def test_tenant_llm_client_audit_log_emitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """generate() emits a structured audit log entry via logger.info."""

    monkeypatch.setenv("FORESIGHT_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("FORESIGHT_LLM_MODEL", "gpt-test")

    client = TenantLLMClient.from_env("my-tenant")
    fake = MagicMock()
    fake.complete.return_value = "narrative response"

    audit_records: list[stdlib_logging.LogRecord] = []

    class AuditHandler(stdlib_logging.Handler):
        def emit(self, record: stdlib_logging.LogRecord) -> None:
            audit_records.append(record)

    logger = stdlib_logging.getLogger("foresight_llm_client")
    original_level = logger.level
    logger.setLevel(stdlib_logging.INFO)
    handler = AuditHandler()
    handler.setLevel(stdlib_logging.INFO)
    logger.addHandler(handler)
    try:
        with patch.object(TenantLLMClient, "_build_provider", return_value=fake):
            client.generate("test prompt", user_id="user-abc")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)

    assert len(audit_records) >= 1
    # Verify AUDIT log contains tenant and outcome
    audit_msg = audit_records[0].getMessage()
    assert "my-tenant" in audit_msg
    assert "user-abc" in audit_msg
    assert "success" in audit_msg


def test_tenant_llm_client_test_mode_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    """tenant_id_override=test bypasses audit logging (test mode)."""
    monkeypatch.setenv("FORESIGHT_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("FORESIGHT_LLM_TENANT_OVERRIDE", "test")

    client = TenantLLMClient.from_env("my-tenant")
    fake = MagicMock()
    fake.complete.return_value = "response"

    audit_records: list[stdlib_logging.LogRecord] = []

    class AuditHandler(stdlib_logging.Handler):
        def emit(self, record: stdlib_logging.LogRecord) -> None:
            if "[AUDIT]" in record.getMessage():
                audit_records.append(record)

    logger = stdlib_logging.getLogger("foresight_llm_client")
    original_level = logger.level
    logger.setLevel(stdlib_logging.DEBUG)
    handler = AuditHandler()
    logger.addHandler(handler)
    try:
        with patch.object(TenantLLMClient, "_build_provider", return_value=fake):
            result = client.generate("hello", user_id="u1")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)

    assert result == "response"
    # In test mode, no [AUDIT] entries should be emitted
    assert len(audit_records) == 0


def test_tenant_llm_client_retry_on_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provider errors are retried max_retries times before raising."""
    monkeypatch.setenv("FORESIGHT_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    client = TenantLLMClient.from_env("t1")

    call_count = 0

    def flaky_complete(_prompt: str, *, max_tokens: int = 1024) -> str:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise LLMProviderError("temporary failure")
        return "success on try " + str(call_count)

    fake = MagicMock()
    fake.complete.side_effect = flaky_complete
    with patch.object(TenantLLMClient, "_build_provider", return_value=fake):
        result = client.generate("prompt", user_id="u1")
    assert result == "success on try 3"
    assert call_count == 3


def test_tenant_llm_client_rate_limit_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLMRateLimitError is NOT retried; it propagates immediately."""
    monkeypatch.setenv("FORESIGHT_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("FORESIGHT_LLM_MAX_RETRIES", "3")

    client = TenantLLMClient.from_env("t1")

    fake = MagicMock()
    fake.complete.side_effect = LLMRateLimitError("rate limited")
    with patch.object(TenantLLMClient, "_build_provider", return_value=fake):
        with pytest.raises(LLMRateLimitError, match="rate limited"):
            client.generate("prompt", user_id="u1")
        # Only one call made (no retries for rate limits)
        assert fake.complete.call_count == 1


def test_llm_config_from_env_all_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLMConfig.from_env reads all supported environment variables."""
    monkeypatch.setenv("FORESIGHT_LLM_PROVIDER", "openai")
    monkeypatch.setenv("FORESIGHT_LLM_MODEL", "gpt-4o")
    monkeypatch.setenv("FORESIGHT_LLM_API_KEY", "my-global-key")
    monkeypatch.setenv("FORESIGHT_LLM_MAX_RETRIES", "5")
    monkeypatch.setenv("FORESIGHT_LLM_TIMEOUT_MS", "45000")
    monkeypatch.setenv("FORESIGHT_LLM_TENANT_OVERRIDE", "my-test-tenant")

    config = LLMConfig.from_env("any-tenant")
    assert config.provider == "openai"
    assert config.model_version == "gpt-4o"
    assert config.api_key == "my-global-key"
    assert config.max_retries == 5
    assert config.timeout_ms == 45000
    assert config.tenant_id_override == "my-test-tenant"


def test_llm_config_model_for_provider_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """model_for_provider returns provider-specific defaults when model_version is empty."""
    monkeypatch.setenv("FORESIGHT_LLM_PROVIDER", "anthropic")
    monkeypatch.delenv("FORESIGHT_LLM_MODEL", raising=False)

    config = LLMConfig(provider="anthropic", model_version="")
    assert config.model_for_provider() == "claude-3-5-sonnet-latest"

    config_openai = LLMConfig(provider="openai", model_version="")
    assert config_openai.model_for_provider() == "gpt-4o-mini"

    config_explicit = LLMConfig(provider="openai", model_version="gpt-4")
    assert config_explicit.model_for_provider() == "gpt-4"


def test_tenant_llm_client_is_immutable(monkeypatch: pytest.MonkeyPatch) -> None:
    """TenantLLMClient uses __slots__ to prevent arbitrary attribute addition."""
    monkeypatch.setenv("FORESIGHT_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")

    client = TenantLLMClient.from_env("t1")
    # With __slots__, you can't add new attributes
    with pytest.raises(AttributeError, match="'TenantLLMClient' object has no attribute 'foo'"):
        client.foo = "bar"
