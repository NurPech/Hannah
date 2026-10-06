"""#417: Syslog-Ziel der Satelliten per MQTT — Collector, Fallback, Debounce."""
import json
import threading

from hannah.component_registry import ComponentRegistry, KIND_CHANNEL, KIND_LOG_COLLECTOR
from hannah.syslog_target import SyslogTarget, payload


class _Collector:
    def __init__(self, host="10.0.0.5", syslog_port=5514):
        self.host = host
        self.port = 50060
        self.syslog_port = syslog_port


class _Timers:
    """Ersetzt threading.Timer: gestartete Timer feuern erst auf Zuruf."""

    def __init__(self, monkeypatch):
        self.pending = []
        timers = self

        class FakeTimer:
            def __init__(self, interval, function):
                self.interval, self.function, self.cancelled = interval, function, False
                self.daemon = False

            def start(self):
                timers.pending.append(self)

            def cancel(self):
                self.cancelled = True

        monkeypatch.setattr(threading, "Timer", FakeTimer)

    def fire(self):
        """Lässt alle noch nicht abgebrochenen Timer feuern."""
        due, self.pending = [t for t in self.pending if not t.cancelled], []
        for timer in due:
            timer.function()


def _setup(monkeypatch, fallback=None, debounce_s=10.0):
    timers = _Timers(monkeypatch)
    registry = ComponentRegistry()
    settings = dict(fallback or {})
    published = []
    target = SyslogTarget(registry, lambda: settings, published.append, debounce_s=debounce_s)
    return registry, settings, published, target, timers


def _decoded(published):
    return [json.loads(p) for p in published]


NONE = {"host": "", "port": 0}


def test_payload_without_target_is_not_empty():
    """Ein leeres Payload würde die retained Message löschen, ohne dass Satelliten es erfahren."""
    assert json.loads(payload(None)) == NONE
    assert json.loads(payload(("10.0.0.5", 5514))) == {"host": "10.0.0.5", "port": 5514}


def test_registered_collector_is_published_at_once(monkeypatch):
    registry, _, published, target, _timers = _setup(monkeypatch)
    target.start()
    assert published == []  # at startup the collector may still be about to connect

    registry.register(KIND_LOG_COLLECTOR, "main", _Collector())

    assert _decoded(published) == [{"host": "10.0.0.5", "port": 5514}]


def test_collector_already_there_at_start(monkeypatch):
    registry, _, published, target, _timers = _setup(monkeypatch)
    registry.register(KIND_LOG_COLLECTOR, "main", _Collector(host="10.0.0.9"))

    target.start()

    assert _decoded(published) == [{"host": "10.0.0.9", "port": 5514}]


def test_no_collector_after_the_debounce_publishes_the_fallback(monkeypatch):
    _, _, published, target, timers = _setup(
        monkeypatch, fallback={"fallback_host": "10.0.0.7", "fallback_port": 514})
    target.start()
    assert published == []

    timers.fire()

    assert _decoded(published) == [{"host": "10.0.0.7", "port": 514}]


def test_no_collector_and_no_fallback_publishes_none(monkeypatch):
    _, _, published, target, timers = _setup(monkeypatch, fallback={"fallback_host": "", "fallback_port": 514})
    target.start()

    timers.fire()

    assert _decoded(published) == [NONE]


def test_collector_leaving_switches_to_the_fallback_only_after_the_debounce(monkeypatch):
    registry, _, published, target, timers = _setup(
        monkeypatch, fallback={"fallback_host": "10.0.0.7", "fallback_port": 514})
    target.start()
    collector = _Collector()
    registry.register(KIND_LOG_COLLECTOR, "main", collector)

    registry.unregister(KIND_LOG_COLLECTOR, "main", collector)
    assert len(published) == 1  # not switched yet

    timers.fire()
    assert _decoded(published)[-1] == {"host": "10.0.0.7", "port": 514}


def test_collector_back_within_the_debounce_never_switches(monkeypatch):
    registry, _, published, target, timers = _setup(
        monkeypatch, fallback={"fallback_host": "10.0.0.7", "fallback_port": 514})
    target.start()
    first = _Collector()
    registry.register(KIND_LOG_COLLECTOR, "main", first)
    registry.unregister(KIND_LOG_COLLECTOR, "main", first)

    registry.register(KIND_LOG_COLLECTOR, "main", _Collector())  # same address again
    timers.fire()  # the cancelled debounce must not fire

    assert _decoded(published) == [{"host": "10.0.0.5", "port": 5514}]  # published once, unchanged


def test_collector_returning_after_the_fallback_takes_over_again(monkeypatch):
    registry, _, published, target, timers = _setup(
        monkeypatch, fallback={"fallback_host": "10.0.0.7", "fallback_port": 514})
    target.start()
    timers.fire()
    assert _decoded(published) == [{"host": "10.0.0.7", "port": 514}]

    registry.register(KIND_LOG_COLLECTOR, "main", _Collector())

    assert _decoded(published)[-1] == {"host": "10.0.0.5", "port": 5514}


def test_collector_without_a_syslog_receiver_counts_as_none(monkeypatch):
    """A hannah.v1 collector, or one without syslog.listen, announces port 0."""
    registry, _, published, target, timers = _setup(
        monkeypatch, fallback={"fallback_host": "10.0.0.7", "fallback_port": 514})
    target.start()

    registry.register(KIND_LOG_COLLECTOR, "main", _Collector(syslog_port=0))
    assert published == []
    timers.fire()

    assert _decoded(published) == [{"host": "10.0.0.7", "port": 514}]


def test_changed_fallback_settings_apply_at_once_while_on_the_fallback(monkeypatch):
    _, settings, published, target, timers = _setup(
        monkeypatch, fallback={"fallback_host": "10.0.0.7", "fallback_port": 514})
    target.start()
    timers.fire()

    settings["fallback_host"] = "10.0.0.8"
    target.refresh()

    assert _decoded(published)[-1] == {"host": "10.0.0.8", "port": 514}


def test_refresh_does_not_publish_the_same_thing_twice(monkeypatch):
    _, _, published, target, timers = _setup(
        monkeypatch, fallback={"fallback_host": "10.0.0.7", "fallback_port": 514})
    target.start()
    timers.fire()
    target.refresh()
    target.refresh()

    assert len(published) == 1


def test_changed_fallback_while_a_collector_is_up_changes_nothing(monkeypatch):
    registry, settings, published, target, _timers = _setup(monkeypatch)
    target.start()
    registry.register(KIND_LOG_COLLECTOR, "main", _Collector())

    settings.update({"fallback_host": "10.0.0.7", "fallback_port": 514})
    target.refresh()

    assert _decoded(published) == [{"host": "10.0.0.5", "port": 5514}]


def test_invalid_fallback_counts_as_none(monkeypatch):
    for settings in ({"fallback_host": "10.0.0.7", "fallback_port": 0},
                     {"fallback_host": "10.0.0.7", "fallback_port": 70000},
                     {"fallback_host": "10.0.0.7", "fallback_port": "abc"},
                     {"fallback_host": "   ", "fallback_port": 514}):
        _, _, published, target, timers = _setup(monkeypatch, fallback=settings)
        target.start()
        timers.fire()
        assert _decoded(published) == [NONE], settings


def test_other_kinds_are_ignored(monkeypatch):
    registry, _, published, target, timers = _setup(monkeypatch)
    target.start()

    registry.register(KIND_CHANNEL, "telegram", object())
    timers.fire()

    assert _decoded(published) == [NONE]  # only the startup debounce, nothing from the channel


def test_several_collectors_pick_one_deterministically(monkeypatch):
    registry, _, published, target, _timers = _setup(monkeypatch)
    target.start()
    registry.register(KIND_LOG_COLLECTOR, "zeta", _Collector(host="10.0.0.2"))
    registry.register(KIND_LOG_COLLECTOR, "alpha", _Collector(host="10.0.0.1"))

    assert _decoded(published)[-1]["host"] == "10.0.0.1"


def test_a_failing_publish_is_tried_again_at_the_next_occasion(monkeypatch):
    registry = ComponentRegistry()
    calls = []

    def publish(text):
        calls.append(text)
        if len(calls) == 1:
            raise RuntimeError("broker down")

    target = SyslogTarget(registry, lambda: {}, publish)
    registry.register(KIND_LOG_COLLECTOR, "main", _Collector())
    target.start()  # first attempt fails
    target.refresh()  # the next occasion

    assert len(calls) == 2
