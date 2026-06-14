"""OpenAI Chat Completions API adapter.

Uses the public ``https://api.openai.com/v1/chat/completions`` endpoint.
No SDK dependency; only the standard library.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from foresight_mcp.llm_errors import LLMError

DEFAULT_MODEL = "gpt-4o-mini"
API_URL = "https://api.openai.com/v1/chat/completions"


class OpenAIClient:
    provider: str = "openai"

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, timeout: int = 60) -> None:
        if not api_key:
            raise LLMError("OpenAI API key is required. Set OPENAI_API_KEY or pass api_key=... explicitly.")
        if not model:
            raise LLMError("model is required and must be a non-empty string")
        self._api_key = api_key
        self.model = model
        self._timeout = timeout

    @classmethod
    def from_env(cls) -> OpenAIClient:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        model = os.environ.get("FORESIGHT_LLM_MODEL", DEFAULT_MODEL).strip()
        timeout_s = int(os.environ.get("FORESIGHT_LLM_TIMEOUT_MS", "60000")) // 1000
        return cls(api_key=api_key, model=model, timeout=timeout_s)

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
                "Authorization": f"Bearer {self._api_key}",
                "content-type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LLMError(f"OpenAI API returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"OpenAI API request failed: {exc.reason}") from exc

        try:
            parsed: dict[str, Any] = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise LLMError(f"OpenAI API returned malformed JSON: {exc}") from exc

        choices = parsed.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMError(f"OpenAI API returned no choices: {payload[:200]}")

        for choice in choices:
            if isinstance(choice, dict):
                message = choice.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str):
                        return content

        raise LLMError(f"OpenAI API returned no message content: {payload[:200]}")


__all__ = ["DEFAULT_MODEL", "OpenAIClient"]
