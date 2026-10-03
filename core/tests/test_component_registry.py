import logging

import pytest

from hannah.component_registry import (
    ComponentRegistry, ComponentTracker, KIND_CHANNEL, KIND_COMPONENT, KIND_LOG_COLLECTOR,
    EVENT_REGISTERED, EVENT_UNREGISTERED, PRESENCE_WINDOW_S,
)
from hannah.grpc_interceptors import CallerIdentity


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


# ------------------------------------------------------------------
# ComponentTracker (#398)

class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return _Clock()


@pytest.fixture
def registry():
    return ComponentRegistry()


@pytest.fixture
def tracker(registry, clock):
    return ComponentTracker(registry, clock=clock, wall_clock=lambda: 1_700_000_000.0 + clock.now)


def _caller(component="proxy", version="1.0.0", instance_id="a" * 32):
    return CallerIdentity(component, version, instance_id)


def _names(registry):
    return sorted(name for kind, name, _ in registry.items() if kind == KIND_COMPONENT)


def test_the_first_call_enters_the_component_and_announces_it(registry, tracker):
    events = []
    registry.subscribe(lambda event, kind, name, handle: events.append((event, kind, name)))

    entry = tracker.seen(_caller("proxy", "1.2.3", "abc"))

    assert registry.get(KIND_COMPONENT, "proxy/abc") is entry
    assert (entry.component, entry.version, entry.instance_id) == ("proxy", "1.2.3", "abc")
    assert events == [(EVENT_REGISTERED, KIND_COMPONENT, "proxy/abc")]


def test_every_further_call_refreshes_the_last_sighting_without_registering_again(registry, tracker, clock):
    events = []
    entry = tracker.seen(_caller("proxy", "1.0.0", "abc"))
    first_seen = entry.first_seen
    registry.subscribe(lambda event, kind, name, handle: events.append(event))
    clock.advance(30)

    again = tracker.seen(_caller("proxy", "1.0.0", "abc"))

    assert again is entry
    assert entry.first_seen == first_seen and entry.last_seen == first_seen + 30
    assert events == []


def test_two_instances_of_one_component_are_two_entries(registry, tracker):
    tracker.seen(_caller("proxy", "1.0.0", "aaa"))
    tracker.seen(_caller("proxy", "1.0.0", "bbb"))

    assert _names(registry) == ["proxy/aaa", "proxy/bbb"]


def test_a_new_version_of_the_same_instance_updates_the_entry_and_logs_it(tracker, caplog):
    entry = tracker.seen(_caller("telegram", "1.0.0", "abc"))

    with caplog.at_level(logging.INFO, logger="hannah.component_registry"):
        tracker.seen(_caller("telegram", "1.1.0", "abc"))

    assert entry.version == "1.1.0"
    assert [r.getMessage() for r in caplog.records if "spricht mit Core" in r.getMessage()] == [
        "[components] telegram 1.1.0 (Instanz abc) spricht mit Core"
    ]


def test_the_legacy_path_is_remembered_and_logged(tracker, caplog):
    with caplog.at_level(logging.INFO, logger="hannah.component_registry"):
        entry = tracker.seen(_caller("proxy", "0.9.0", "abc"), legacy=True)

    assert entry.legacy is True
    assert "eingefrorenen alten Pfad" in caplog.records[-1].getMessage()
    assert tracker.seen(_caller("proxy", "0.9.0", "abc"), legacy=False).legacy is False


def test_a_component_without_stream_is_gone_after_the_window(registry, tracker, clock):
    tracker.seen(_caller("timer", instance_id="t1"))

    clock.advance(PRESENCE_WINDOW_S - 1)
    assert tracker.snapshot() != []

    clock.advance(2)
    assert tracker.snapshot() == []
    assert _names(registry) == []


def test_a_heartbeat_keeps_the_component_alive(tracker, clock):
    for _ in range(5):
        tracker.seen(_caller("timer", instance_id="t1"))  # what a Heartbeat call does
        clock.advance(30)

    assert [c["component"] for c in tracker.snapshot()] == ["timer"]


def test_an_open_stream_keeps_the_component_without_any_call(tracker, clock):
    entry = tracker.seen(_caller("telegram", instance_id="t1"))
    tracker.stream_opened(entry)

    clock.advance(10 * PRESENCE_WINDOW_S)

    assert [c["component"] for c in tracker.snapshot()] == ["telegram"]


def test_closing_the_last_stream_removes_a_component_that_was_silent_for_long(registry, tracker, clock):
    entry = tracker.seen(_caller("telegram", instance_id="t1"))
    tracker.stream_opened(entry)
    clock.advance(10 * PRESENCE_WINDOW_S)

    tracker.stream_closed(entry)

    assert _names(registry) == []


def test_closing_the_last_stream_keeps_a_component_that_was_heard_just_now(registry, tracker, clock):
    entry = tracker.seen(_caller("telegram", instance_id="t1"))
    tracker.stream_opened(entry)
    clock.advance(10)

    tracker.stream_closed(entry)

    assert _names(registry) == ["telegram/t1"]  # a heartbeat may still follow, the window decides


def test_one_of_several_streams_ending_does_not_remove_the_component(registry, tracker, clock):
    entry = tracker.seen(_caller("telegram", instance_id="t1"))
    tracker.stream_opened(entry)
    tracker.stream_opened(entry)
    clock.advance(10 * PRESENCE_WINDOW_S)

    tracker.stream_closed(entry)

    assert _names(registry) == ["telegram/t1"]
    assert tracker.snapshot()[0]["open_streams"] == 1

    tracker.stream_closed(entry)

    assert _names(registry) == []


def test_a_restarted_component_is_a_new_instance_and_the_old_one_expires(registry, tracker, clock):
    tracker.seen(_caller("proxy", instance_id="before-restart"))
    clock.advance(5)
    tracker.seen(_caller("proxy", instance_id="after-restart"))
    assert _names(registry) == ["proxy/after-restart", "proxy/before-restart"]

    clock.advance(PRESENCE_WINDOW_S)
    tracker.seen(_caller("proxy", instance_id="after-restart"))

    assert _names(registry) == ["proxy/after-restart"]


def test_an_expiry_is_announced_to_subscribers(registry, tracker, clock):
    tracker.seen(_caller("timer", instance_id="t1"))
    events = []
    registry.subscribe(lambda event, kind, name, handle: events.append((event, kind, name)))

    clock.advance(PRESENCE_WINDOW_S + 1)
    tracker.snapshot()

    assert events == [(EVENT_UNREGISTERED, KIND_COMPONENT, "timer/t1")]


def test_a_call_of_another_component_cleans_up_the_expired(registry, tracker, clock):
    """No thread of its own: expired entries go on calls (at most once a second) and on access."""
    tracker.seen(_caller("timer", instance_id="t1"))
    clock.advance(PRESENCE_WINDOW_S + 1)

    tracker.seen(_caller("proxy", instance_id="p1"))

    assert _names(registry) == ["proxy/p1"]


def test_core_itself_never_expires(tracker, clock):
    tracker.register_permanent("core", "0.95.0", "c0")

    clock.advance(100 * PRESENCE_WINDOW_S)

    assert [c["component"] for c in tracker.snapshot()] == ["core"]


def test_a_role_hangs_on_the_instance_that_holds_it(registry, tracker):
    class Sub:
        def __init__(self, caller):
            self.caller = caller

    telegram = _caller("telegram", "1.0.0", "t1")
    tracker.seen(telegram)
    tracker.seen(_caller("telegram", "1.0.0", "t2"))
    registry.register(KIND_CHANNEL, "telegram", Sub(telegram))
    registry.register(KIND_LOG_COLLECTOR, "main", Sub(_caller("logcollector", "0.4.0", "l1")))
    registry.register(KIND_CHANNEL, "legacy", Sub(None))  # a client without identity has no instance to hang on

    by_id = {c["instance_id"]: c for c in tracker.snapshot()}

    assert by_id["t1"]["roles"] == [{"kind": KIND_CHANNEL, "name": "telegram"}]
    assert by_id["t2"]["roles"] == []


def test_the_list_is_bounded_and_drops_the_longest_silent_without_stream(registry, clock):
    tracker = ComponentTracker(registry, max_components=3, clock=clock)
    streaming = tracker.seen(_caller("a", instance_id="1"))
    tracker.stream_opened(streaming)
    for i in range(2, 6):
        clock.advance(1)
        tracker.seen(_caller("a", instance_id=str(i)))

    assert _names(registry) == ["a/1", "a/4", "a/5"]


def test_entries_are_ordered_by_last_sighting(tracker, clock):
    tracker.seen(_caller("one", instance_id="1"))
    clock.advance(1)
    tracker.seen(_caller("two", instance_id="2"))
    clock.advance(1)
    tracker.seen(_caller("one", instance_id="1"))

    assert [c["component"] for c in tracker.snapshot()] == ["one", "two"]
