"""Tests for the LLM client base, factory, and provider adapters."""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from foresight_mcp.llm_client import (
    LLMError,
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

    fake_client = MagicMock(spec=AnthropicClient)
    fake_client.complete.return_value = "narrative text"

    with patch("foresight_mcp.llm_client.get_default_client", return_value=fake_client):
        out = default_llm_call("prompt", "tenant-1", "user-1")
    assert out == "narrative text"
    fake_client.complete.assert_called_once_with("prompt")


def test_default_llm_call_propagates_llm_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORESIGHT_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    fake_client = MagicMock(spec=AnthropicClient)
    fake_client.complete.side_effect = LLMError("upstream 500")
    with (
        patch("foresight_mcp.llm_client.get_default_client", return_value=fake_client),
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
        code=429,
        msg="Too Many Requests",
        hdrs=MagicMock(),
        fp=io.BytesIO(b"rate limit"),
    )
    with (
        patch("urllib.request.urlopen", side_effect=http_err),
        pytest.raises(LLMError, match="OpenAI API returned HTTP 429"),
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
