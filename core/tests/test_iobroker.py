import threading
import time

import pytest
from hannah_proto.v2 import hannah_pb2 as pb
from hannah.iobroker import _camel_to_words, _iaq_label, IoBrokerClient
from hannah.typed_devices import ORIGIN_V1, DeviceRegistry, Slot, TypedDevice
from hannah_proto.v1.hannah_pb2 import AgentDevice, AgentStateValue


class TestCamelToWords:
    def test_camel_case(self):
        assert _camel_to_words("DeckeSeite") == "decke seite"

    def test_underscore(self):
        assert _camel_to_words("Zimmer_Sued") == "zimmer süd"

    def test_number_suffix(self):
        assert _camel_to_words("Deckenlampe_Spot1") == "deckenlampe spot 1"

    def test_umlaut_ae(self):
        assert _camel_to_words("BueroRene") == "büro rene"

    def test_single_word(self):
        assert _camel_to_words("Wohnzimmer") == "wohnzimmer"

    def test_sued_to_sued(self):
        assert _camel_to_words("Sued") == "süd"

    def test_multiple_uppercase(self):
        assert _camel_to_words("EG") == "e g"


class TestParsePayload:
    def test_true(self):
        assert IoBrokerClient._parse_payload("true") is True

    def test_true_mixed_case(self):
        assert IoBrokerClient._parse_payload("True") is True

    def test_false(self):
        assert IoBrokerClient._parse_payload("false") is False

    def test_integer(self):
        result = IoBrokerClient._parse_payload("42")
        assert result == 42
        assert isinstance(result, int)

    def test_negative_integer(self):
        assert IoBrokerClient._parse_payload("-5") == -5

    def test_float(self):
        assert IoBrokerClient._parse_payload("3.14") == pytest.approx(3.14)

    def test_string(self):
        assert IoBrokerClient._parse_payload("hello") == "hello"

    def test_whitespace_stripped(self):
        assert IoBrokerClient._parse_payload("  42  ") == 42

    def test_empty_string(self):
        assert IoBrokerClient._parse_payload("") == ""


class TestIaqLabel:
    def test_good(self):
        assert _iaq_label(50) == "gut"

    def test_okay(self):
        assert _iaq_label(75) == "okay"
        assert _iaq_label(100) == "okay"

    def test_slightly_polluted(self):
        assert _iaq_label(101) == "leicht belastet"
        assert _iaq_label(150) == "leicht belastet"

    def test_bad(self):
        assert _iaq_label(151) == "schlecht"
        assert _iaq_label(286) == "schlecht"


class TestHandleStateUpdate:
    """#522: ein Live-Update bestätigt wartende Befehle und pflegt den State-Cache. Die Live-Werte der
    Geräte stehen in der typisierten Registry (legacy_devices.apply_state_update)."""

    @pytest.fixture
    def client(self):
        return IoBrokerClient({"host": "localhost", "port": 8093})

    def _pending(self, expected):
        return {
            "expected": expected, "device": "sat-1", "deadline": time.monotonic() + 30, "label": "Lampe im Bad",
            "confirmed": False, "requester_name": "",
        }

    def test_a_state_without_a_room_updates_the_state_cache(self, client):
        client._state_cache["weather.temp"] = 1.0

        client.handle_state_update("weather.temp", "12.5")

        assert client._state_cache["weather.temp"] == 12.5

    def test_the_expected_value_confirms_a_pending_command(self, client):
        feedback = []
        client.set_feedback_handler(lambda device, success, text: feedback.append((device, success)))
        client._pending["x.lamp.on"] = self._pending(True)

        client.handle_state_update("x.lamp.on", "true")

        assert feedback == [("sat-1", True)]
        assert "x.lamp.on" not in client._pending

    def test_another_value_fails_a_pending_command(self, client):
        feedback = []
        client.set_feedback_handler(lambda device, success, text: feedback.append((device, success)))
        client._pending["x.lamp.on"] = self._pending(True)

        client.handle_state_update("x.lamp.on", "false")

        assert feedback == [("sat-1", False)]

    def test_a_confirm_waiter_of_a_text_channel_is_released(self, client):
        event = threading.Event()
        client._confirm_waiters["x.lamp.on"] = {"expected": True, "event": event, "success": False}

        client.handle_state_update("x.lamp.on", "true")

        assert event.is_set()
        assert client._confirm_waiters["x.lamp.on"]["success"] is True

class TestGetStateRaw:
    @pytest.fixture
    def client(self):
        return IoBrokerClient({"host": "localhost", "port": 8093})

    def test_state_cache_hit(self, client):
        client._state_cache["openweathermap.0.current.temperature"] = 21.5
        assert client.get_state_raw("openweathermap.0.current.temperature") == "21.5"

    def test_state_cache_none_value(self, client):
        client._state_cache["some.state"] = None
        assert client.get_state_raw("some.state") is None

    def test_unknown_state_returns_none(self, client):
        assert client.get_state_raw("nonexistent.0.state") is None

    def test_device_state_hit(self, client):
        """#522: der Wert kommt live aus der Registry, nicht aus dem alten Gerätebaum."""
        registry = DeviceRegistry()
        slot = Slot("on", pb.SLOT_KIND_ON, state_id="virtualDevice.Licht.EG.Wohnzimmer.Decke.on", value=True)
        registry.replace(ORIGIN_V1, [TypedDevice("virtualDevice.Licht.EG.Wohnzimmer.Decke", "Decke", "wohnzimmer",
                                                 slots={"on": slot})])
        client.set_slot_control(registry, controller=object())

        assert client.get_state_raw("virtualDevice.Licht.EG.Wohnzimmer.Decke.on") == "True"

    def test_a_state_of_the_registry_that_the_state_cache_also_has_comes_from_the_cache(self, client):
        client._state_cache["x.lamp.on"] = False
        registry = DeviceRegistry()
        registry.replace(ORIGIN_V1, [TypedDevice("lamp", "Lampe", "bad", slots={
            "on": Slot("on", pb.SLOT_KIND_ON, state_id="x.lamp.on", value=True)})])
        client.set_slot_control(registry, controller=object())

        assert client.get_state_raw("x.lamp.on") == "False"


class TestHandleDeviceSnapshot:
    """#522: der Snapshot-Handler hält nur noch die States ohne Raum. Geräte mit Raum gehören in die
    typisierte Registry (legacy_devices.load_snapshot), einen Gerätebaum gibt es nicht mehr."""

    @staticmethod
    def _state(state_id, value, room=""):
        return AgentDevice(state_id=state_id, room=room, device_id="x.dev" if room else "",
                           value=AgentStateValue(value=value, ack=True))

    def test_states_without_a_room_land_in_the_state_cache(self):
        client = IoBrokerClient({})

        client.handle_device_snapshot([
            self._state("openweathermap.0.current.temperature", "21.5"),
            self._state("vw.0.car.parked", "true"),
        ])

        assert client.get_state_raw("openweathermap.0.current.temperature") == "21.5"
        assert client.get_state_raw("vw.0.car.parked") == "True"

    def test_a_state_with_a_room_is_not_kept_here(self):
        client = IoBrokerClient({})

        client.handle_device_snapshot([self._state("x.dev.on", "true", room="bad")])

        assert client.get_state_raw("x.dev.on") is None

    def test_a_new_snapshot_replaces_the_old_cache(self):
        client = IoBrokerClient({})
        client.handle_device_snapshot([self._state("a.b.c", "1")])

        client.handle_device_snapshot([self._state("d.e.f", "2")])

        assert client.get_state_raw("a.b.c") is None
        assert client.get_state_raw("d.e.f") == "2"
