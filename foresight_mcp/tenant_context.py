"""Request-scoped tenant context using contextvars.

Replaces the global _tenant_context singleton and TENANT_ID constant
with per-request isolation that works correctly with asyncio and threading.

Extended with explicit account/workspace memory scoping (PIX-317):
- user_id: Individual user identity
- account_id / workspace_id: Organization/workspace grouping (synonymous)
- app_id / integration_id: Optional source application/integration identifier
- scope: Derived memory namespace for isolation
"""

from __future__ import annotations

import warnings
from contextvars import ContextVar
from dataclasses import dataclass

from .config import DEFAULT_ACCOUNT_ID, DEFAULT_TENANT_ID, DEFAULT_USER_ID

__all__ = [
    "DEFAULT_TENANT_ID",
    "MemoryScope",
    "get_current_account_id",
    "get_current_app_id",
    "get_current_integration_id",
    "get_current_scope",
    "get_current_tenant_id",
    "get_current_user_id",
    "reset_tenant_context",
    "set_current_account_id",
    "set_current_app_id",
    "set_current_integration_id",
    "set_current_tenant_id",
    "set_current_user_id",
]


@dataclass(frozen=True)
class MemoryScope:
    """Explicit memory access scope derived from identity context.

    This is the canonical scope key used for all memory operations.
    It combines user, account/workspace, and optional app identifiers
    into a single namespace string for database queries and cache keys.
    """

    user_id: str
    account_id: str  # Also used as workspace_id (synonymous)
    app_id: str | None = None
    integration_id: str | None = None

    def namespace(self) -> str:
        """Return the canonical namespace string for DB queries and caching."""
        parts = [self.user_id, self.account_id]
        if self.app_id:
            parts.append(self.app_id)
        elif self.integration_id:
            parts.append(self.integration_id)
        return ":".join(parts)

    def cache_key_suffix(self) -> str:
        """Suffix for cache invalidation keys."""
        return self.namespace()

    def to_dict(self) -> dict:
        """Serialize for logging/debugging."""
        return {
            "user_id": self.user_id,
            "account_id": self.account_id,
            "app_id": self.app_id,
            "integration_id": self.integration_id,
            "namespace": self.namespace(),
        }


# Context variables for each identity component
_current_user: ContextVar[str] = ContextVar("foresight_user_id", default=DEFAULT_USER_ID)
_current_account: ContextVar[str] = ContextVar("foresight_account_id", default=DEFAULT_ACCOUNT_ID)
_current_app: ContextVar[str | None] = ContextVar("foresight_app_id", default=None)
_current_integration: ContextVar[str | None] = ContextVar("foresight_integration_id", default=None)

# Composite scope (derived, not directly settable)
_current_scope: ContextVar[MemoryScope | None] = ContextVar("foresight_memory_scope", default=None)


def get_current_user_id() -> str:
    """Get the user ID for the current request context."""
    return _current_user.get()


def set_current_user_id(user_id: str) -> None:
    """Set the user ID for the current request context."""
    _current_user.set(user_id)
    _invalidate_scope()


def get_current_account_id() -> str:
    """Get the account/workspace ID for the current request context."""
    return _current_account.get()


def set_current_account_id(account_id: str) -> None:
    """Set the account/workspace ID for the current request context."""
    _current_account.set(account_id)
    _invalidate_scope()


def get_current_app_id() -> str | None:
    """Get the optional app ID for the current request context."""
    return _current_app.get()


def set_current_app_id(app_id: str | None) -> None:
    """Set the optional app ID for the current request context."""
    _current_app.set(app_id)
    _invalidate_scope()


def get_current_integration_id() -> str | None:
    """Get the optional integration ID for the current request context."""
    return _current_integration.get()


def set_current_integration_id(integration_id: str | None) -> None:
    """Set the optional integration ID for the current request context."""
    _current_integration.set(integration_id)
    _invalidate_scope()


def get_current_scope() -> MemoryScope:
    """Get the derived memory scope for the current request context.

    Lazily constructs the MemoryScope from individual identity components.
    This is the primary function downstream code should use for memory operations.
    """
    scope = _current_scope.get()
    if scope is None:
        scope = MemoryScope(
            user_id=get_current_user_id(),
            account_id=get_current_account_id(),
            app_id=get_current_app_id(),
            integration_id=get_current_integration_id(),
        )
        _current_scope.set(scope)
    return scope


def _invalidate_scope() -> None:
    """Invalidate cached scope when any identity component changes."""
    _current_scope.set(None)


def set_current_tenant_id(tenant_id: str) -> None:
    """Set the tenant ID for the current request context (deprecated).

    For backward compatibility, this sets the account_id.
    New code should use set_current_account_id() directly.
    """

    warnings.warn(
        "set_current_tenant_id() is deprecated; use set_current_account_id() instead",
        DeprecationWarning,
        stacklevel=2,
    )
    set_current_account_id(tenant_id)


def get_current_tenant_id() -> str:
    """Get the tenant ID for the current request context (deprecated).

    For backward compatibility, this returns the account_id.
    New code should use get_current_account_id() or get_current_scope() directly.
    """

    warnings.warn(
        "get_current_tenant_id() is deprecated; use get_current_account_id() or get_current_scope() instead",
        DeprecationWarning,
        stacklevel=2,
    )
    return get_current_account_id()


def reset_tenant_context() -> None:
    """Reset all identity context to defaults (for testing)."""
    _current_user.set(DEFAULT_USER_ID)
    _current_account.set(DEFAULT_ACCOUNT_ID)
    _current_app.set(None)
    _current_integration.set(None)
    _current_scope.set(None)
