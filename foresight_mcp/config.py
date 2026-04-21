"""Shared configuration constants for the Foresight MCP server.

Centralizes DB_PATH, USER_ID, BANK_ID, TENANT_ID, and rate-limit
defaults so that submodules can import them without creating circular
dependencies on server.py.
"""
import os
from pathlib import Path

# Database
DEFAULT_DB_PATH = str(Path.home() / ".foresight" / "memory.db")
DB_PATH = os.environ.get("FORESIGHT_DB_PATH", DEFAULT_DB_PATH)

# Identity
DEFAULT_USER_ID = os.environ.get("USER", "user")
USER_ID = os.environ.get("FORESIGHT_USER_ID", DEFAULT_USER_ID)

DEFAULT_BANK_ID = "default"
BANK_ID = os.environ.get("FORESIGHT_BANK_ID", DEFAULT_BANK_ID)

DEFAULT_TENANT_ID = "default"
TENANT_ID = os.environ.get("FORESIGHT_TENANT_ID", DEFAULT_TENANT_ID)

# Rate limiting
DEFAULT_RATE_LIMIT = 100  # requests per minute
DEFAULT_BURST_LIMIT = 20  # burst requests
