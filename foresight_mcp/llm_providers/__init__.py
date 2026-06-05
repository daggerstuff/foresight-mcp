"""LLM provider adapters for Anthropic and OpenAI.

Both adapters use only the Python standard library so the
``foresight-mcp`` package does not gain new top-level dependencies.
"""

from .anthropic import AnthropicClient
from .openai import OpenAIClient

__all__ = ["AnthropicClient", "OpenAIClient"]
