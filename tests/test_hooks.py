from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from foresight_mcp.event_bus import Event, EventType, get_event_bus, reset_event_bus
from foresight_mcp.hooks import (
    HookExecutor,
    HookRegistry,
    HookResult,
    HttpHookOptions,
    MemoryHookContext,
    MemoryHookType,
    get_memory_hook_registry,
    reset_memory_hook_registry,
)


def _make_event() -> Event:
    return Event(
        id=f"evt-{uuid.uuid4()}",
        event_type=EventType.MEMORY_STORED,
        timestamp=datetime.now(timezone.utc),
        actor="tester",
        entity_id="memory-1",
        payload={"value": "hello"},
    )


def test_sync_publish_runs_in_memory_async_hooks_without_active_loop(tmp_path):
    reset_event_bus()
    executor = HookExecutor(registry=HookRegistry(db_path=str(tmp_path / "hooks.db")))
    seen = threading.Event()
    received: list[str] = []

    async def async_handler(event: Event) -> None:
        received.append(event.entity_id)
        seen.set()

    executor.register_async(EventType.MEMORY_STORED, async_handler)

    try:
        get_event_bus().publish(_make_event())
        assert seen.wait(timeout=2), "async hook did not finish on background loop"
        assert received == ["memory-1"]
    finally:
        executor.close()
        reset_event_bus()


def test_sync_publish_runs_http_hooks_without_active_loop(tmp_path, monkeypatch):
    reset_event_bus()
    executor = HookExecutor(registry=HookRegistry(db_path=str(tmp_path / "hooks.db")))
    seen = threading.Event()
    observed: list[str] = []

    async def fake_execute_http(hook, event: Event) -> None:
        observed.append(f"{hook.name}:{event.entity_id}")
        seen.set()

    monkeypatch.setattr(executor, "_execute_http", fake_execute_http)
    executor.register_http_hook(
        name="audit-webhook",
        event_type=EventType.MEMORY_STORED,
        url="https://example.test/hooks",
        options=HttpHookOptions(),
    )

    try:
        get_event_bus().publish(_make_event())
        assert seen.wait(timeout=2), "http hook did not finish on background loop"
        assert observed == ["audit-webhook:memory-1"]
    finally:
        executor.close()
        reset_event_bus()


# =============================================================================
# Memory Hook Registry Tests
# =============================================================================


def _make_ctx(action: str = "store", **kwargs: Any) -> MemoryHookContext:
    return MemoryHookContext(action=action, user_id="test_user", tenant_id="default", **kwargs)


def test_registry_singleton():
    """get_memory_hook_registry returns the same instance; reset clears it."""
    reset_memory_hook_registry()
    r1 = get_memory_hook_registry()
    r2 = get_memory_hook_registry()
    assert r1 is r2
    reset_memory_hook_registry()
    r3 = get_memory_hook_registry()
    assert r3 is not r1


def test_register_and_list_handlers():
    """Handlers appear in list_handlers output."""
    reset_memory_hook_registry()
    reg = get_memory_hook_registry()

    def h1(_ctx: MemoryHookContext) -> None:
        pass

    def h2(_ctx: MemoryHookContext) -> HookResult:
        return HookResult()

    reg.register(MemoryHookType.PRE_STORE, h1, name="handler_one")
    reg.register(MemoryHookType.PRE_STORE, h2, name="handler_two")
    reg.register(MemoryHookType.POST_RETRIEVE, h1, name="post_handler")

    all_h = reg.list_handlers()
    assert len(all_h) == 3

    pre_store_h = reg.list_handlers(MemoryHookType.PRE_STORE)
    assert len(pre_store_h) == 2
    assert pre_store_h[0]["name"] == "handler_one"

    assert len(reg.list_handlers(MemoryHookType.POST_RETRIEVE)) == 1
    assert len(reg.list_handlers(MemoryHookType.PRE_DELETE)) == 0


def test_unregister():
    """unregister removes a specific handler."""
    reset_memory_hook_registry()
    reg = get_memory_hook_registry()

    def h(_ctx: MemoryHookContext) -> None:
        pass

    reg.register(MemoryHookType.PRE_STORE, h, name="to_remove")
    assert len(reg.list_handlers(MemoryHookType.PRE_STORE)) == 1

    assert reg.unregister(MemoryHookType.PRE_STORE, h) is True
    assert len(reg.list_handlers(MemoryHookType.PRE_STORE)) == 0

    # Unregistering unknown handler returns False
    assert reg.unregister(MemoryHookType.PRE_STORE, h) is False


def test_clear_all_handlers():
    """clear() removes every registered handler."""
    reset_memory_hook_registry()
    reg = get_memory_hook_registry()

    def h(_ctx: MemoryHookContext) -> None:
        pass

    reg.register(MemoryHookType.PRE_STORE, h)
    reg.register(MemoryHookType.POST_DELETE, h)
    assert len(reg.list_handlers()) == 2

    reg.clear()
    assert len(reg.list_handlers()) == 0


def test_pre_hook_can_abort_operation():
    """Pre-hook returning abort=True prevents further processing."""
    reset_memory_hook_registry()
    reg = get_memory_hook_registry()
    abort_hook_called = threading.Event()

    def abort_handler(ctx: MemoryHookContext) -> HookResult:
        abort_hook_called.set()
        return HookResult(abort=True, message="Blocked by policy")

    reg.register(MemoryHookType.PRE_STORE, abort_handler)

    ctx = _make_ctx(content="test content")
    results = reg.emit_pre(MemoryHookType.PRE_STORE, ctx)

    assert abort_hook_called.is_set()
    assert len(results) == 1
    assert results[0].abort is True
    assert "Blocked by policy" in results[0].message


def test_pre_hook_can_modify_context():
    """Pre-hook returning modified_context shadows fields on the context."""
    reset_memory_hook_registry()
    reg = get_memory_hook_registry()

    def modify_handler(ctx: MemoryHookContext) -> HookResult:
        return HookResult(modified_context={"content": "modified content"})

    reg.register(MemoryHookType.PRE_STORE, modify_handler)

    ctx = _make_ctx(content="original content")
    reg.emit_pre(MemoryHookType.PRE_STORE, ctx)

    assert ctx.content == "modified content"


def test_multiple_pre_hooks_chain():
    """Multiple pre-hooks run in order; later hooks see earlier modifications."""
    reset_memory_hook_registry()
    reg = get_memory_hook_registry()
    order: list[str] = []

    def first(ctx: MemoryHookContext) -> HookResult:
        order.append("first")
        return HookResult(modified_context={"content": f"{ctx.content} + first"})

    def second(ctx: MemoryHookContext) -> HookResult:
        order.append("second")
        return HookResult(modified_context={"content": f"{ctx.content} + second"})

    reg.register(MemoryHookType.PRE_STORE, first, name="first")
    reg.register(MemoryHookType.PRE_STORE, second, name="second")

    ctx = _make_ctx(content="start")
    reg.emit_pre(MemoryHookType.PRE_STORE, ctx)

    assert order == ["first", "second"]
    assert ctx.content == "start + first + second"


def test_pre_hook_abort_stops_chain():
    """When a pre-hook aborts, no later handlers run."""
    reset_memory_hook_registry()
    reg = get_memory_hook_registry()
    second_called = threading.Event()

    def blocking(ctx: MemoryHookContext) -> HookResult:
        return HookResult(abort=True, message="blocked")

    def second(ctx: MemoryHookContext) -> HookResult:
        second_called.set()
        return HookResult()

    reg.register(MemoryHookType.PRE_STORE, blocking, name="blocker")
    reg.register(MemoryHookType.PRE_STORE, second, name="second")

    ctx = _make_ctx(content="test")
    reg.emit_pre(MemoryHookType.PRE_STORE, ctx)

    assert not second_called.is_set()


def test_post_hook_is_fire_and_forget():
    """Post-hook results are ignored even if abort=True."""
    reset_memory_hook_registry()
    reg = get_memory_hook_registry()
    post_called = threading.Event()

    def post_handler(ctx: MemoryHookContext) -> HookResult:
        post_called.set()
        return HookResult(abort=True, message="should be ignored")

    reg.register(MemoryHookType.POST_STORE, post_handler)

    ctx = _make_ctx(content="test")
    # emit_post should not raise despite abort=True
    reg.emit_post(MemoryHookType.POST_STORE, ctx)

    assert post_called.is_set()


def test_handler_isolation_sync_failure():
    """One failing handler does not break other handlers."""
    reset_memory_hook_registry()
    reg = get_memory_hook_registry()
    good_called = threading.Event()

    def failing(_ctx: MemoryHookContext) -> HookResult:
        raise RuntimeError("handler crashed")

    def good(_ctx: MemoryHookContext) -> HookResult:
        good_called.set()
        return HookResult(abort=True, message="good blocks")

    reg.register(MemoryHookType.PRE_STORE, failing, name="failing")
    reg.register(MemoryHookType.PRE_STORE, good, name="good")

    ctx = _make_ctx(content="test")
    results = reg.emit_pre(MemoryHookType.PRE_STORE, ctx)

    # Good handler still ran and produced abort
    assert good_called.is_set()
    assert any(r.abort for r in results)


def test_handler_isolation_post_failure():
    """Post-hook failures are caught and do not propagate."""
    reset_memory_hook_registry()
    reg = get_memory_hook_registry()
    good_called = threading.Event()

    def failing(_ctx: MemoryHookContext) -> None:
        raise RuntimeError("post crash")

    def good(_ctx: MemoryHookContext) -> None:
        good_called.set()

    reg.register(MemoryHookType.POST_STORE, failing, name="failing")
    reg.register(MemoryHookType.POST_STORE, good, name="good")

    ctx = _make_ctx(content="test")
    # Must not raise
    reg.emit_post(MemoryHookType.POST_STORE, ctx)

    assert good_called.is_set()


def test_async_handler_on_pre():
    """Async handlers registered on pre-hooks are resolved."""
    reset_memory_hook_registry()
    reg = get_memory_hook_registry()

    async def async_handler(ctx: MemoryHookContext) -> HookResult:
        await asyncio.sleep(0.01)
        return HookResult(abort=True, message="async blocked")

    reg.register(MemoryHookType.PRE_STORE, async_handler, name="async_blocker")

    ctx = _make_ctx(content="test")
    results = reg.emit_pre(MemoryHookType.PRE_STORE, ctx)

    assert len(results) == 1
    assert results[0].abort is True
    assert "async blocked" in results[0].message


def test_async_handler_on_post():
    """Async handlers on post-hooks complete without raising."""
    reset_memory_hook_registry()
    reg = get_memory_hook_registry()
    done = threading.Event()

    async def async_handler(ctx: MemoryHookContext) -> None:
        await asyncio.sleep(0.01)
        done.set()

    reg.register(MemoryHookType.POST_RETRIEVE, async_handler)

    ctx = _make_ctx(action="retrieve")
    reg.emit_post(MemoryHookType.POST_RETRIEVE, ctx)

    assert done.wait(timeout=2)


def test_memory_hook_context_to_dict_roundtrip():
    """to_dict() and from_dict() are symmetric."""
    ctx = MemoryHookContext(
        action="store",
        memory_id="mem-1",
        user_id="u1",
        tenant_id="t1",
        content="hello",
        category="fact",
        scope="session",
        retention="short_term",
        importance=0.8,
        tags=["tag1", "tag2"],
        query="search term",
        metadata={"source": "test"},
    )
    d = ctx.to_dict()
    restored = MemoryHookContext.from_dict(d)
    assert restored.action == "store"
    assert restored.memory_id == "mem-1"
    assert restored.content == "hello"
    assert restored.query == "search term"
    assert restored.metadata == {"source": "test"}
    assert restored.importance == 0.8


def test_thread_safe_concurrent_registrations():
    """Concurrent registrations from multiple threads do not race."""
    reset_memory_hook_registry()
    reg = get_memory_hook_registry()
    n = 20
    barrier = threading.Barrier(n)
    errors: list[Exception] = []

    def register_one(idx: int) -> None:
        def handler(_ctx: MemoryHookContext) -> None:
            pass

        barrier.wait()
        try:
            reg.register(MemoryHookType.PRE_STORE, handler, name=f"t{idx}")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=register_one, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0
    assert len(reg.list_handlers(MemoryHookType.PRE_STORE)) == n


def test_emit_pre_for_hook_type_without_handlers():
    """emit_pre with no registered handlers returns empty list."""
    reset_memory_hook_registry()
    reg = get_memory_hook_registry()
    ctx = _make_ctx(content="test")
    results = reg.emit_pre(MemoryHookType.PRE_STORE, ctx)
    assert results == []


def test_emit_post_for_hook_type_without_handlers():
    """emit_post with no registered handlers runs without error."""
    reset_memory_hook_registry()
    reg = get_memory_hook_registry()
    ctx = _make_ctx(content="test")
    # Must not raise
    reg.emit_post(MemoryHookType.POST_STORE, ctx)


def test_hook_result_defaults():
    """HookResult defaults are sane (no abort, empty message, no modified_context)."""
    r = HookResult()
    assert r.abort is False
    assert r.message == ""
    assert r.modified_context is None


def test_hook_result_with_context_override():
    """HookResult modified_context overrides specified fields."""
    r = HookResult(modified_context={"content": "new", "importance": 0.9})
    ctx = _make_ctx(content="old", importance=0.5)

    if r.modified_context:
        for k, v in r.modified_context.items():
            if hasattr(ctx, k):
                setattr(ctx, k, v)

    assert ctx.content == "new"
    assert ctx.importance == 0.9
