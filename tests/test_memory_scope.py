"""Tests for explicit memory scoping (PIX-317)."""

import asyncio

import pytest
from foresight_mcp.tenant_context import (
    MemoryScope,
    get_current_account_id,
    get_current_app_id,
    get_current_integration_id,
    get_current_scope,
    get_current_user_id,
    reset_tenant_context,
    set_current_account_id,
    set_current_app_id,
    set_current_integration_id,
    set_current_user_id,
)


def test_memory_scope_defaults():
    """Test default scope construction."""
    reset_tenant_context()
    scope = get_current_scope()
    assert scope.user_id == "vivi"
    assert scope.account_id == "default"
    assert scope.app_id is None
    assert scope.integration_id is None
    assert scope.namespace() == "vivi:default"


def test_memory_scope_user_account_only():
    """Test scope with user and account."""
    reset_tenant_context()
    set_current_user_id("alice")
    set_current_account_id("acme-corp")
    scope = get_current_scope()
    assert scope.user_id == "alice"
    assert scope.account_id == "acme-corp"
    assert scope.namespace() == "alice:acme-corp"


def test_memory_scope_with_app_id():
    """Test scope with app_id."""
    reset_tenant_context()
    set_current_user_id("bob")
    set_current_account_id("workspace-1")
    set_current_app_id("mobile-app")
    scope = get_current_scope()
    assert scope.user_id == "bob"
    assert scope.account_id == "workspace-1"
    assert scope.app_id == "mobile-app"
    assert scope.integration_id is None
    assert scope.namespace() == "bob:workspace-1:mobile-app"


def test_memory_scope_with_integration_id():
    """Test scope with integration_id (when no app_id)."""
    reset_tenant_context()
    set_current_user_id("carol")
    set_current_account_id("team-alpha")
    set_current_integration_id("slack")
    scope = get_current_scope()
    assert scope.user_id == "carol"
    assert scope.account_id == "team-alpha"
    assert scope.app_id is None
    assert scope.integration_id == "slack"
    assert scope.namespace() == "carol:team-alpha:slack"


def test_memory_scope_app_id_priority_over_integration():
    """Test app_id takes priority over integration_id in namespace."""
    reset_tenant_context()
    set_current_user_id("dave")
    set_current_account_id("org-1")
    set_current_app_id("web-app")
    set_current_integration_id("github")
    scope = get_current_scope()
    # app_id should win
    assert scope.namespace() == "dave:org-1:web-app"


def test_scope_cache_invalidation_on_user_change():
    """Test scope is rebuilt when user_id changes."""
    reset_tenant_context()
    set_current_user_id("user1")
    set_current_account_id("account1")
    scope1 = get_current_scope()
    assert scope1.namespace() == "user1:account1"

    set_current_user_id("user2")
    scope2 = get_current_scope()
    assert scope2.namespace() == "user2:account1"
    assert scope1 is not scope2  # new instance


def test_scope_cache_invalidation_on_account_change():
    """Test scope is rebuilt when account_id changes."""
    reset_tenant_context()
    set_current_user_id("user1")
    set_current_account_id("account1")
    scope1 = get_current_scope()
    assert scope1.namespace() == "user1:account1"

    set_current_account_id("account2")
    scope2 = get_current_scope()
    assert scope2.namespace() == "user1:account2"
    assert scope1 is not scope2


def test_scope_cache_invalidation_on_app_change():
    """Test scope is rebuilt when app_id changes."""
    reset_tenant_context()
    set_current_user_id("user1")
    set_current_account_id("account1")
    set_current_app_id("app1")
    scope1 = get_current_scope()
    assert scope1.namespace() == "user1:account1:app1"

    set_current_app_id("app2")
    scope2 = get_current_scope()
    assert scope2.namespace() == "user1:account1:app2"
    assert scope1 is not scope2


def test_scope_immutability():
    """Test MemoryScope is frozen/immutable."""
    scope = MemoryScope(user_id="u1", account_id="a1")
    try:
        scope.user_id = "u2"
        pytest.fail("Should have raised")
    except Exception:
        pass


def test_scope_to_dict():
    """Test scope serialization."""
    reset_tenant_context()
    set_current_user_id("test-user")
    set_current_account_id("test-account")
    set_current_app_id("test-app")
    scope = get_current_scope()
    d = scope.to_dict()
    assert d["user_id"] == "test-user"
    assert d["account_id"] == "test-account"
    assert d["app_id"] == "test-app"
    assert d["namespace"] == "test-user:test-account:test-app"


async def _run_isolation_task(user_id, account_id, app_id, results, key):
    """Helper for async isolation test."""
    set_current_user_id(user_id)
    set_current_account_id(account_id)
    set_current_app_id(app_id)
    scope = get_current_scope()
    results[key] = scope.namespace()
    await asyncio.sleep(0.01)  # yield


def test_contextvar_isolation_across_tasks():
    """Test that concurrent tasks have isolated scope context."""
    reset_tenant_context()
    results = {}

    async def run_all():
        await asyncio.gather(
            _run_isolation_task("user-a", "account-1", "app-x", results, "a"),
            _run_isolation_task("user-b", "account-2", "app-y", results, "b"),
            _run_isolation_task("user-c", "account-1", "app-z", results, "c"),
        )

    asyncio.run(run_all())

    assert results["a"] == "user-a:account-1:app-x"
    assert results["b"] == "user-b:account-2:app-y"
    assert results["c"] == "user-c:account-1:app-z"


def test_reset_clears_all_context():
    """Test reset_tenant_context clears all identity components."""
    reset_tenant_context()
    set_current_user_id("custom-user")
    set_current_account_id("custom-account")
    set_current_app_id("custom-app")
    set_current_integration_id("custom-integration")

    reset_tenant_context()

    assert get_current_user_id() == "vivi"
    assert get_current_account_id() == "default"
    assert get_current_app_id() is None
    assert get_current_integration_id() is None
    scope = get_current_scope()
    assert scope.namespace() == "vivi:default"


def test_workspace_id_synonym():
    """Test workspace_id is treated as synonym for account_id in middleware."""
    # This is tested at middleware level, but verify the concept
    scope = MemoryScope(user_id="u1", account_id="workspace-123")
    assert scope.account_id == "workspace-123"
    assert scope.namespace() == "u1:workspace-123"
