"""FastMCP middleware that resolves tenant from request context."""

from __future__ import annotations

import logging

from fastmcp.server.middleware import Middleware as _Middleware

import re

from .config import DEFAULT_TENANT_ID
from .tenant_context import get_current_tenant_id, set_current_tenant_id

logger = logging.getLogger(__name__)

# Allowlist for tenant IDs — alphanumeric, hyphens, underscores, 1-64 chars.
_VALID_TENANT_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")


def _sanitize_tenant_id(value: str) -> str | None:
    """Return value if it passes the allowlist, else None."""
    if isinstance(value, str) and _VALID_TENANT_RE.match(value):
        return value
    logger.warning("Rejected invalid tenant_id from request context: %r", value)
    return None


class TenantMiddleware(_Middleware):
    """Resolves tenant_id from request context and sets the contextvar.

    Resolution order:
    1. Tool argument ``tenant_id`` (if provided and valid)
    2. Request metadata ``_meta`` (if available from MCP transport and valid)
    3. DEFAULT_TENANT_ID

    After resolution, the tenant ID is stored in the contextvar so that
    downstream code (SQL queries, graph store, etc.) can access it via
    ``get_current_tenant_id()`` without threading the parameter through
    every function call.
    """

    async def on_call_tool(self, context, call_next):
        tenant_id = self._resolve_tenant(context)
        set_current_tenant_id(tenant_id)
        try:
            return await call_next(context)
        finally:
            set_current_tenant_id(DEFAULT_TENANT_ID)

    def _resolve_tenant(self, context) -> str:
        # Try tool arguments first
        message = getattr(context, "message", None)
        if message:
            arguments = getattr(message, "arguments", None) or {}
            if isinstance(arguments, dict) and "tenant_id" in arguments:
                sanitized = _sanitize_tenant_id(arguments["tenant_id"])
                if sanitized:
                    return sanitized

        # Try request metadata
        if message:
            meta = getattr(message, "meta", None)
            if meta and hasattr(meta, "model_extra") and meta.model_extra:
                tid = meta.model_extra.get("tenant_id")
                if tid:
                    sanitized = _sanitize_tenant_id(tid)
                    if sanitized:
                        return sanitized

        return DEFAULT_TENANT_ID
