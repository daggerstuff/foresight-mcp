"""Anthropic Messages API adapter.

Uses the public ``https://api.anthropic.com/v1/messages`` endpoint.
No SDK dependency; only the standard library.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from foresight_mcp.llm_errors import LLMError

DEFAULT_MODEL = "claude-3-5-sonnet-latest"
API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


class AnthropicClient:
    provider: str = "anthropic"

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL) -> None:
        if not api_key:
            raise LLMError(
                "Anthropic API key is required. Set ANTHROPIC_API_KEY or "
                "pass api_key=... explicitly."
            )
        if not model:
            raise LLMError("model is required and must be a non-empty string")
        self._api_key = api_key
        self.model = model

    @classmethod
    def from_env(cls) -> AnthropicClient:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        model = os.environ.get("FORESIGHT_LLM_MODEL", DEFAULT_MODEL).strip()
        return cls(api_key=api_key, model=model)

    def complete(self, prompt: str, *, max_tokens: int = 1024) -> str:
        if not isinstance(prompt, str) or not prompt:
            raise LLMError("prompt must be a non-empty string")
        if max_tokens < 1:
            raise LLMError("max_tokens must be at least 1")

        body = {
            "model": self.model,
            "max_tokens": int(max_tokens),
            "messages": [{"role": "user", "content": prompt}],
        }
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            API_URL,
            data=data,
            method="POST",
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LLMError(
                f"Anthropic API returned HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"Anthropic API request failed: {exc.reason}") from exc

        try:
            parsed: dict[str, Any] = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Anthropic API returned malformed JSON: {exc}") from exc

        content = parsed.get("content")
        if not isinstance(content, list) or not content:
            raise LLMError(
                f"Anthropic API returned no content blocks: {payload[:200]}"
            )

        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    return text

        raise LLMError(
            f"Anthropic API returned no text block: {payload[:200]}"
        )


__all__ = ["DEFAULT_MODEL", "AnthropicClient"]
