from __future__ import annotations

from datetime import datetime, timezone

import foresight_mcp.event_bus as event_bus_module
from foresight_mcp.event_bus import Event, EventType, get_event_bus, reset_event_bus


class _FakeStore:
    def append(self, event: Event) -> None:
        self.last_event = event


class _DummyPublisher:
    def __init__(self):
        self.published: list[Event] = []

    def publish_event(self, event: Event) -> None:
        self.published.append(event)


def _make_event() -> Event:
    return Event(
        id="evt-stream",
        event_type=EventType.MEMORY_STORED,
        timestamp=datetime.now(timezone.utc),
        actor="tester",
        entity_id="memory-1",
        payload={"value": "hello"},
    )


def test_get_event_bus_attaches_late_stream_publisher(monkeypatch):
    reset_event_bus()
    monkeypatch.setattr(event_bus_module, "EventStore", _FakeStore)
    publisher = _DummyPublisher()

    bus = get_event_bus()
    same_bus = get_event_bus(stream_publisher=publisher)
    event = _make_event()
    same_bus.publish(event)

    assert same_bus is bus
    assert publisher.published == [event]

    reset_event_bus()
