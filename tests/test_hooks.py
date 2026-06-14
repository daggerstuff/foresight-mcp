from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

from foresight_mcp.event_bus import Event, EventType, get_event_bus, reset_event_bus
from foresight_mcp.hooks import HookExecutor, HookRegistry, HttpHookOptions


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
