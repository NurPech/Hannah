"""Versionsvergleich der laufenden Komponenten mit den öffentlichen Versionen (#504)."""
from unittest.mock import MagicMock

import pytest
import requests
from hannah_proto.v2 import hannah_pb2 as pb

from hannah import version_check
from hannah.component_registry import ComponentRegistry, ComponentTracker
from hannah.grpc_interceptors import CallerIdentity
from hannah.grpc_server import HannahServicer
from hannah.settings_manager import DEFAULT_VERSION_CHECK_SETTINGS
from hannah.version_check import (
    NEWER_THAN_PUBLIC, UP_TO_DATE, UPDATE_AVAILABLE, PublicVersions, VersionChecker, compare, component_versions,
)


# --- Vergleich ---------------------------------------------------------------------------------

@pytest.mark.parametrize("running,public,expected", [
    ("0.102.10", "0.102.10", UP_TO_DATE),
    ("0.102.9", "0.102.10", UPDATE_AVAILABLE),
    ("0.103.0", "0.102.10", NEWER_THAN_PUBLIC),
    ("1.0.0", "0.999.999", NEWER_THAN_PUBLIC),
    ("0.9.0", "0.10.0", UPDATE_AVAILABLE),               # numerisch, nicht als Text
    ("v0.102.10", "0.102.10", UP_TO_DATE),               # führendes v
    ("0.102.10+build5", "0.102.10", UP_TO_DATE),         # Build-Metadaten zählen nicht
    ("0.103.0-rc.1", "0.102.10", NEWER_THAN_PUBLIC),     # rc vor der Veröffentlichung
    ("0.103.0-rc.1", "0.103.0", UPDATE_AVAILABLE),       # die Final ist höher als ihr rc
    ("0.103.0-rc.1", "0.103.0-rc.1", UP_TO_DATE),
    ("0.103.0-rc.2", "0.103.0-rc.10", UPDATE_AVAILABLE),  # numerische Teile numerisch
    ("0.103.0-alpha", "0.103.0-beta", UPDATE_AVAILABLE),
    ("0.103.0-rc", "0.103.0-rc.1", UPDATE_AVAILABLE),    # mehr Teile sind höher
    ("0.103.0", "0.103.0-rc.1", NEWER_THAN_PUBLIC),
])
def test_compare_follows_semver(running, public, expected):
    assert compare(running, public) == expected


@pytest.mark.parametrize("running,public", [
    ("", "0.102.10"), ("0.102.10", ""), ("dev", "0.102.10"), ("0.102", "0.102.10"), ("01.2.3", "1.2.3"),
])
def test_compare_gives_up_on_something_that_is_not_semver(running, public):
    assert compare(running, public) is None


def test_component_versions_compares_each_instance_with_its_components_public_version():
    instances = [
        {"component": "telegram", "instance_id": "t1", "version": "0.5.0"},
        {"component": "telegram", "instance_id": "t2", "version": "0.4.0"},   # Rollout: zweite Version
        {"component": "tg_responder", "instance_id": "r1", "version": "1.0.0"},  # kein Aggregator-Schlüssel
    ]

    result = component_versions(instances, {"telegram": "0.5.0"})

    assert [(r["instance_id"], r["latest_version"], r["comparison"]) for r in result] == [
        ("t1", "0.5.0", UP_TO_DATE), ("t2", "0.5.0", UPDATE_AVAILABLE), ("r1", "", None),
    ]


def test_component_versions_names_an_instance_by_its_host_and_falls_back_to_the_id():
    """#506 — der Host ersetzt die zufällige ID in der Anzeige, ohne Host bleibt die ID."""
    instances = [
        {"component": "proxy", "instance_id": "p1", "host": "psrvhva01", "version": "0.5.0"},
        {"component": "proxy", "instance_id": "p2", "host": "", "version": "0.5.0"},
        {"component": "proxy", "instance_id": "p3", "version": "0.5.0"},   # Eintrag ohne das Feld
    ]

    result = component_versions(instances, {})

    assert [r["instance_id"] for r in result] == ["psrvhva01", "p2", "p3"]


# --- Abruf -------------------------------------------------------------------------------------

class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _response(payload, status=200):
    response = MagicMock()
    response.json.return_value = payload
    response.content = b"{}"
    response.raise_for_status.side_effect = None if status < 400 else requests.HTTPError(f"{status}")
    return response


@pytest.fixture
def clock():
    return _Clock()


def _checker(clock, http_get, settings=None):
    settings = dict(DEFAULT_VERSION_CHECK_SETTINGS) if settings is None else settings
    return VersionChecker(lambda: settings, http_get=http_get, clock=clock, wall_clock=lambda: 1_700_000_000.0)


def test_the_aggregator_keys_are_mapped_to_component_names_and_satellite_esp_is_left_out(clock):
    http_get = MagicMock(return_value=_response({
        "core": "0.102.10", "iobroker-adapter": "1.9.0", "satellite-esp": "0.20.0", "broken": 5,
    }))

    public = _checker(clock, http_get).public_versions()

    assert public == PublicVersions({"core": "0.102.10", "iobroker": "1.9.0"}, True, 1_700_000_000)


def test_the_first_call_fetches_with_the_configured_url_and_later_calls_come_from_the_cache(clock):
    http_get = MagicMock(return_value=_response({"core": "1.0.0"}))
    checker = _checker(clock, http_get, {"enabled": True, "url": "http://aggregator.test/versions", "cache_ttl_s": 600})

    checker.public_versions()
    clock.now += 599
    checker.public_versions()

    http_get.assert_called_once()
    assert http_get.call_args.args == ("http://aggregator.test/versions",)
    assert http_get.call_args.kwargs["timeout"] == version_check.FETCH_TIMEOUT_S


def test_the_cache_is_refreshed_after_the_ttl(clock):
    http_get = MagicMock(side_effect=[_response({"core": "1.0.0"}), _response({"core": "1.1.0"})])
    checker = _checker(clock, http_get, {"enabled": True, "url": "u", "cache_ttl_s": 600})

    checker.public_versions()
    clock.now += 601

    assert checker.public_versions().versions == {"core": "1.1.0"}


def test_disabled_makes_no_request(clock):
    http_get = MagicMock()

    public = _checker(clock, http_get, {"enabled": False}).public_versions()

    http_get.assert_not_called()
    assert public == PublicVersions({}, False, 0)


def test_missing_settings_fall_back_to_the_defaults(clock):
    http_get = MagicMock(return_value=_response({"core": "1.0.0"}))

    _checker(clock, http_get, {}).public_versions()

    assert http_get.call_args.args == (version_check.DEFAULT_URL,)


def test_an_unreachable_aggregator_is_not_an_error_and_is_retried_with_a_growing_pause(clock):
    http_get = MagicMock(side_effect=requests.ConnectionError("down"))
    checker = _checker(clock, http_get, {"enabled": True, "url": "u", "cache_ttl_s": 3600})

    assert checker.public_versions() == PublicVersions({}, False, 0)
    clock.now += 59
    checker.public_versions()                       # noch in der Wartezeit: kein neuer Abruf
    assert http_get.call_count == 1
    clock.now += 2
    checker.public_versions()                       # 60 s sind um
    assert http_get.call_count == 2
    clock.now += 100
    checker.public_versions()                       # die zweite Wartezeit ist 120 s
    assert http_get.call_count == 2
    clock.now += 21
    checker.public_versions()
    assert http_get.call_count == 3


def test_a_failed_refresh_keeps_the_last_good_answer(clock):
    http_get = MagicMock(side_effect=[_response({"core": "1.0.0"}), requests.Timeout("slow")])
    checker = _checker(clock, http_get, {"enabled": True, "url": "u", "cache_ttl_s": 600})

    checker.public_versions()
    clock.now += 601
    public = checker.public_versions()

    assert public == PublicVersions({"core": "1.0.0"}, True, 1_700_000_000)


@pytest.mark.parametrize("bad", [
    _response(["not", "an", "object"]),
    _response({}, status=500),
])
def test_an_answer_that_is_not_the_aggregators_counts_as_a_failure(clock, bad):
    assert _checker(clock, MagicMock(return_value=bad)).public_versions().available is False


def test_an_oversized_answer_counts_as_a_failure(clock):
    big = _response({"core": "1.0.0"})
    big.content = b"x" * (version_check.MAX_RESPONSE_BYTES + 1)

    assert _checker(clock, MagicMock(return_value=big)).public_versions().available is False


def test_a_broken_settings_read_does_not_raise(clock):
    def broken():
        raise RuntimeError("db gone")

    checker = VersionChecker(broken, http_get=MagicMock(return_value=_response({"core": "1.0.0"})), clock=clock)

    assert checker.public_versions().versions == {"core": "1.0.0"}   # Defaults


# --- gRPC --------------------------------------------------------------------------------------

def _servicer(public, instances):
    registry = ComponentRegistry()
    tracker = ComponentTracker(registry)
    servicer = HannahServicer.__new__(HannahServicer)   # nur die beiden Felder, die der RPC braucht
    servicer._components = tracker
    servicer._public_versions = lambda: public
    for component, version, instance_id in instances:
        tracker.register_permanent(component, version, instance_id)
    return servicer


def test_get_component_versions_returns_the_comparison_per_instance():
    servicer = _servicer(
        PublicVersions({"core": "0.102.10", "telegram": "0.5.0"}, True, 1_700_000_000),
        [("core", "0.103.0-rc.1", "c1"), ("telegram", "0.4.0", "t1"), ("voice-collector", "0.1.0", "v1")],
    )

    response = servicer.GetComponentVersions(pb.Empty(), None)

    got = {c.component: (c.version, c.latest_version, c.comparison) for c in response.components}
    assert got == {
        "core": ("0.103.0-rc.1", "0.102.10", pb.VERSION_COMPARISON_NEWER_THAN_PUBLIC),
        "telegram": ("0.4.0", "0.5.0", pb.VERSION_COMPARISON_UPDATE_AVAILABLE),
        "voice-collector": ("0.1.0", "", pb.VERSION_COMPARISON_UNSPECIFIED),
    }
    assert (response.latest_available, response.latest_checked_at) == (True, 1_700_000_000)


def test_get_component_versions_names_the_instance_by_its_host():
    """#506"""
    servicer = _servicer(PublicVersions({}, False, 0), [])
    servicer._components.seen(CallerIdentity("proxy", "0.5.0", "p1", "psrvhva01"))
    servicer._components.seen(CallerIdentity("timer", "0.4.0", "t1"))
    servicer._components.register_permanent("core", "0.103.0", "c1", "psrvhva02")

    response = servicer.GetComponentVersions(pb.Empty(), None)

    assert {c.component: c.instance_id for c in response.components} == {
        "proxy": "psrvhva01", "timer": "t1", "core": "psrvhva02",
    }


def test_get_component_versions_without_public_versions_still_lists_the_running_ones():
    servicer = _servicer(PublicVersions({}, False, 0), [("core", "0.102.10", "c1")])

    response = servicer.GetComponentVersions(pb.Empty(), None)

    assert [(c.component, c.comparison) for c in response.components] == [("core", pb.VERSION_COMPARISON_UNSPECIFIED)]
    assert (response.latest_available, response.latest_checked_at) == (False, 0)
