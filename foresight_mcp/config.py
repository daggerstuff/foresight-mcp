"""Shared configuration constants for the Foresight MCP server.

Centralizes DB_PATH, USER_ID, ACCOUNT_ID, and rate-limit
defaults so that submodules can import them without creating circular
dependencies on server.py.

Extended with explicit account/workspace memory scoping (PIX-317):
- user_id: Individual user identity
- account_id / workspace_id: Organization/workspace grouping (synonymous)
- app_id / integration_id: Optional source application/integration identifier (set programmatically)
"""

import os
from pathlib import Path

# Database
DEFAULT_DB_PATH = str(Path.home() / ".foresight" / "memory.db")
DB_PATH = os.environ.get("FORESIGHT_DB_PATH", DEFAULT_DB_PATH)

# Neon / PostgreSQL connection string (overrides DB_PATH when set)
# Format: postgresql://user:password@host:port/dbname?sslmode=require
DB_URL = os.environ.get("FORESIGHT_DB_URL", "")

# Optional Redis companion cache (overrides the in-process dict cache when set).
# See foresight_mcp/redis_cache.RedisCache for the consuming API.
# Format: redis://[:password@]host:port[/db]
REDIS_URL = os.environ.get("FORESIGHT_REDIS_URL", "")

# Identity defaults (used when FORESIGHT_IDENTITY not set)
DEFAULT_USER_ID = os.environ.get("USER", "user")
DEFAULT_ACCOUNT_ID = "default"
DEFAULT_WORKSPACE_ID = "default"

# ONE primary env var for users: FORESIGHT_IDENTITY
# Format: "user@account" or just "user" (defaults to account="default")
# Examples: "alice@acme-corp", "bob", "carol@team-alpha"
IDENTITY = os.environ.get("FORESIGHT_IDENTITY", "")


def _parse_identity() -> tuple[str, str]:
    """Parse FORESIGHT_IDENTITY into (user_id, account_id)."""
    if not IDENTITY:
        return (DEFAULT_USER_ID, DEFAULT_ACCOUNT_ID)
    if "@" in IDENTITY:
        user, account = IDENTITY.split("@", 1)
        return (user, account)
    return (IDENTITY, DEFAULT_ACCOUNT_ID)


USER_ID, ACCOUNT_ID = _parse_identity()

# Synonyms
WORKSPACE_ID = ACCOUNT_ID

# Optional programmatic identifiers (not typically set via env)
DEFAULT_APP_ID = None
APP_ID = os.environ.get("FORESIGHT_APP_ID")

DEFAULT_INTEGRATION_ID = None
INTEGRATION_ID = os.environ.get("FORESIGHT_INTEGRATION_ID")

# Backward compatibility
DEFAULT_TENANT_ID = "default"
TENANT_ID = ACCOUNT_ID  # Maps to account_id

DEFAULT_BANK_ID = "default"
BANK_ID = os.environ.get("FORESIGHT_BANK_ID", DEFAULT_BANK_ID)

# Rate limiting
DEFAULT_RATE_LIMIT = 100  # requests per minute
DEFAULT_BURST_LIMIT = 20  # burst requests

# LLM request throttling (environment overrides)
# FORESIGHT_LLM_RATE_LIMIT       -- requests per minute (default: 60)
# FORESIGHT_LLM_BURST_LIMIT      -- burst requests (default: 10)
# FORESIGHT_LLM_MIN_INTERVAL     -- minimum seconds between requests (default: 0.5)
# FORESIGHT_LLM_MAX_PROMPT_CHARS -- max prompt character length (default: 10000)
