"""FastMCP middleware that resolves identity from request context."""

from __future__ import annotations

import logging
import re

from fastmcp.server.middleware import Middleware as _Middleware

from .config import DEFAULT_ACCOUNT_ID, DEFAULT_USER_ID
from .tenant_context import (
    reset_tenant_context,
    set_current_account_id,
    set_current_app_id,
    set_current_integration_id,
    set_current_user_id,
)

logger = logging.getLogger(__name__)

# Allowlist for identity IDs — alphanumeric, hyphens, underscores, 1-64 chars.
_VALID_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")


def _sanitize_id(value: str) -> str | None:
    """Return value if it passes the allowlist, else None."""
    if isinstance(value, str) and _VALID_ID_RE.match(value):
        return value
    logger.warning("Rejected invalid ID from request context: %r", value)
    return None


def resolve_tenant_id_from_message(message) -> str:
    """Resolve tenant_id from an MCP message object (backward compatibility).

    This is kept for backward compatibility with auth.py and other modules.
    New code should use resolve_identity_from_message().
    """
    identity = resolve_identity_from_message(message)
    return identity["account_id"]


def resolve_identity_from_message(message) -> dict:
    """Resolve identity components from an MCP message object.

    Resolution order for each field (later sources override earlier):
    1. Default from config
    2. Request metadata _meta (if available from MCP transport and valid)
    3. Tool argument (if provided and valid) - HIGHEST PRIORITY

    Returns dict with: user_id, account_id, app_id, integration_id
    """
    result = {
        "user_id": DEFAULT_USER_ID,
        "account_id": DEFAULT_ACCOUNT_ID,
        "app_id": None,
        "integration_id": None,
    }

    if not message:
        return result

    # Check request metadata first (lower priority)
    meta = getattr(message, "meta", None)
    if meta and hasattr(meta, "model_extra") and meta.model_extra:
        extra = meta.model_extra
        for key in ("user_id", "account_id", "workspace_id", "app_id", "integration_id"):
            if key in extra:
                sanitized = _sanitize_id(extra[key])
                if sanitized:
                    if key == "workspace_id":
                        result["account_id"] = sanitized
                    else:
                        result[key] = sanitized

    # Backward compatibility: tenant_id in metadata
    if meta and hasattr(meta, "model_extra") and meta.model_extra:
        extra = meta.model_extra
        if "tenant_id" in extra:
            sanitized = _sanitize_id(extra["tenant_id"])
            if sanitized:
                result["account_id"] = sanitized

    # Check tool arguments second (higher priority - overrides metadata)
    arguments = getattr(message, "arguments", None) or {}
    if isinstance(arguments, dict):
        for key in ("user_id", "account_id", "workspace_id", "app_id", "integration_id"):
            if key in arguments:
                sanitized = _sanitize_id(arguments[key])
                if sanitized:
                    if key == "workspace_id":
                        result["account_id"] = sanitized
                    else:
                        result[key] = sanitized

    # Backward compatibility: tenant_id in arguments
    if "tenant_id" in arguments:
        sanitized = _sanitize_id(arguments["tenant_id"])
        if sanitized:
            result["account_id"] = sanitized

    return result


class TenantMiddleware(_Middleware):
    """Resolves identity from request context and sets contextvars.

    Resolution order for each identity component:
    1. Default from config
    2. Request metadata _meta (if available from MCP transport and valid)
    3. Tool argument (if provided and valid) - HIGHEST PRIORITY

    After resolution, identity components are stored in contextvars so that
    downstream code can access them via get_current_*() functions or
    get_current_scope() for the derived MemoryScope.
    """

    async def on_call_tool(self, context, call_next):
        identity = self._resolve_identity(context)
        set_current_user_id(identity["user_id"])
        set_current_account_id(identity["account_id"])
        set_current_app_id(identity["app_id"])
        set_current_integration_id(identity["integration_id"])
        try:
            return await call_next(context)
        finally:
            reset_tenant_context()

    def _resolve_identity(self, context) -> dict:
        message = getattr(context, "message", None)
        return resolve_identity_from_message(message)
