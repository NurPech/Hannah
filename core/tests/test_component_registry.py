from hannah.component_registry import (
    ComponentRegistry, KIND_CHANNEL, EVENT_REGISTERED, EVENT_UNREGISTERED,
)


def test_register_and_get():
    registry = ComponentRegistry()
    handle = object()
    assert registry.register(KIND_CHANNEL, "telegram", handle) is None
    assert registry.get(KIND_CHANNEL, "telegram") is handle


def test_register_returns_displaced_handle():
    registry = ComponentRegistry()
    first, second = object(), object()
    registry.register(KIND_CHANNEL, "telegram", first)
    assert registry.register(KIND_CHANNEL, "telegram", second) is first
    assert registry.get(KIND_CHANNEL, "telegram") is second


def test_registering_same_handle_again_displaces_nothing():
    registry = ComponentRegistry()
    handle = object()
    registry.register(KIND_CHANNEL, "telegram", handle)
    assert registry.register(KIND_CHANNEL, "telegram", handle) is None


def test_unregister_only_removes_current_handle():
    """A displaced handle ending its stream must not remove its successor."""
    registry = ComponentRegistry()
    first, second = object(), object()
    registry.register(KIND_CHANNEL, "telegram", first)
    registry.register(KIND_CHANNEL, "telegram", second)

    assert registry.unregister(KIND_CHANNEL, "telegram", first) is False
    assert registry.get(KIND_CHANNEL, "telegram") is second

    assert registry.unregister(KIND_CHANNEL, "telegram", second) is True
    assert registry.get(KIND_CHANNEL, "telegram") is None


def test_entries_filtered_by_kind():
    registry = ComponentRegistry()
    telegram, teams, other = object(), object(), object()
    registry.register(KIND_CHANNEL, "telegram", telegram)
    registry.register(KIND_CHANNEL, "teams", teams)
    registry.register("other", "telegram", other)

    assert set(map(id, registry.entries(KIND_CHANNEL))) == {id(telegram), id(teams)}
    assert registry.entries("other") == [other]
    assert registry.entries("unknown") == []


def test_subscribe_returns_snapshot_then_receives_events():
    registry = ComponentRegistry()
    existing, new = object(), object()
    registry.register(KIND_CHANNEL, "telegram", existing)

    events = []
    snapshot = registry.subscribe(lambda *e: events.append(e))
    assert snapshot == [(KIND_CHANNEL, "telegram", existing)]
    assert events == []

    registry.register(KIND_CHANNEL, "teams", new)
    registry.unregister(KIND_CHANNEL, "teams", new)
    assert events == [
        (EVENT_REGISTERED, KIND_CHANNEL, "teams", new),
        (EVENT_UNREGISTERED, KIND_CHANNEL, "teams", new),
    ]


def test_displaced_handle_ending_late_emits_no_event():
    registry = ComponentRegistry()
    first, second = object(), object()
    registry.register(KIND_CHANNEL, "telegram", first)
    registry.register(KIND_CHANNEL, "telegram", second)

    events = []
    registry.subscribe(lambda *e: events.append(e))
    registry.unregister(KIND_CHANNEL, "telegram", first)
    assert events == []


def test_unsubscribed_listener_gets_no_events():
    registry = ComponentRegistry()
    events = []
    listener = lambda *e: events.append(e)
    registry.subscribe(listener)
    registry.unsubscribe(listener)

    registry.register(KIND_CHANNEL, "telegram", object())
    assert events == []


def test_failing_listener_does_not_break_registration():
    registry = ComponentRegistry()
    events = []

    def broken(*_):
        raise RuntimeError("boom")

    registry.subscribe(broken)
    registry.subscribe(lambda *e: events.append(e))
    handle = object()

    assert registry.register(KIND_CHANNEL, "telegram", handle) is None
    assert registry.get(KIND_CHANNEL, "telegram") is handle
    assert len(events) == 1
