"""Tests for TenantMiddleware."""

from unittest.mock import MagicMock

import pytest
from foresight_mcp.tenant_context import (
    DEFAULT_TENANT_ID,
    get_current_tenant_id,
    reset_tenant_context,
    set_current_tenant_id,
)
from foresight_mcp.tenant_middleware import TenantMiddleware


async def _run_middleware(context_obj, tenant_in_args=None, tenant_in_meta=None):
    """Run middleware and capture tenant ID during the call_next invocation."""
    mw = TenantMiddleware()
    captured_tenant = {}

    async def capturing_call_next(ctx):
        captured_tenant["value"] = get_current_tenant_id()
        return "ok"

    message = MagicMock()
    arguments = {}
    if tenant_in_args:
        arguments["tenant_id"] = tenant_in_args
    message.arguments = arguments

    if tenant_in_meta:
        meta = MagicMock()
        meta.model_extra = {"tenant_id": tenant_in_meta}
        message.meta = meta
    else:
        message.meta = None

    context_obj.message = message
    await mw.on_call_tool(context_obj, capturing_call_next)
    return captured_tenant.get("value", DEFAULT_TENANT_ID)


@pytest.mark.asyncio
async def test_tenant_from_tool_arguments():
    reset_tenant_context()
    ctx = MagicMock()
    result = await _run_middleware(ctx, tenant_in_args="acme-corp")
    assert result == "acme-corp"


@pytest.mark.asyncio
async def test_tenant_from_metadata():
    reset_tenant_context()
    ctx = MagicMock()
    result = await _run_middleware(ctx, tenant_in_meta="via-meta")
    assert result == "via-meta"


@pytest.mark.asyncio
async def test_default_tenant_when_nothing_provided():
    reset_tenant_context()
    ctx = MagicMock()
    result = await _run_middleware(ctx)
    assert result == DEFAULT_TENANT_ID


@pytest.mark.asyncio
async def test_arguments_take_priority_over_meta():
    reset_tenant_context()
    ctx = MagicMock()
    result = await _run_middleware(ctx, tenant_in_args="from-args", tenant_in_meta="from-meta")
    assert result == "from-args"


@pytest.mark.asyncio
async def test_tenant_resets_after_request():
    reset_tenant_context()
    set_current_tenant_id("pre-existing")
    ctx = MagicMock()
    await _run_middleware(ctx, tenant_in_args="during-request")
    assert get_current_tenant_id() == DEFAULT_TENANT_ID
