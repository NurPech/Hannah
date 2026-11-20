"""#387 Schritt 4: Steuern über die typisierte Registry (IoBrokerClient.execute → DeviceController)."""
import time

import pytest
from hannah_proto.v2 import hannah_pb2 as pb

from hannah.device_control import DeviceController
from hannah.iobroker import IoBrokerClient, TrustLevelDenied
from hannah.nlu import Intent
from hannah.typed_devices import ORIGIN_V1, DeviceRegistry, Slot, TypedDevice, slot_value_from_pb

C = pb.DeviceClass
K = pb.SlotKind
ROOMS = {"wohnzimmer": "Wohnzimmer"}


def slot(kind, value=None, *, writable=True, trust=None, state_id="", inverted=False):
    return Slot(slot_id=pb.SlotKind.Name(kind)[len("SLOT_KIND_"):].lower(), kind=kind, value=value, writable=writable,
                required_trust_level=trust, state_id=state_id, inverted=inverted)


def device(device_id, name, device_class, *slots, origin="v2"):
    return TypedDevice(device_id=device_id, name=name, room="wohnzimmer", device_class=device_class,
                       slots={s.slot_id: s for s in slots}, origin=origin)


class World:
    """Client + Registry + Controller mit aufgezeichneten Schreibbefehlen."""

    def __init__(self, *devices, confirm_timeout=0.2):
        self.registry = DeviceRegistry()
        self.registry.replace("v2", [d for d in devices if d.origin != ORIGIN_V1])
        self.registry.replace(ORIGIN_V1, [d for d in devices if d.origin == ORIGIN_V1])
        self.slot_writes: list = []     # (device_id, slot_id, Python-Wert)
        self.state_writes: list = []    # (state_id, Wert)
        self.client = IoBrokerClient({})
        self.client.set_setter(lambda *_: True)
        self.client.set_feedback_handler(self._feedback, timeout=confirm_timeout)
        self.feedback: list = []
        self.controller = DeviceController(
            self.registry,
            send_set_slot=lambda device_id, slot_id, value: self.slot_writes.append(
                (device_id, slot_id, slot_value_from_pb(value))) or True,
            send_set_state=lambda state_id, value: self.state_writes.append((state_id, value)) or True,
        )
        self.client.set_slot_control(self.registry, self.controller, lambda: ROOMS)

    def _feedback(self, device, success, text):
        self.feedback.append((device, success, text))

    def run(self, intent, **kwargs):
        kwargs.setdefault("trust_level", None)
        return self.client.execute(intent, **kwargs)


def intent(name, *, device_id=None, value=None, category=None, room_id="wohnzimmer", open_close=False):
    return Intent(name=name, room=ROOMS.get(room_id), room_id=room_id, device_id=device_id,
                  category_filter=category, value=value, is_open_close=open_close)


class TestDevicesOfAV2Adapter:
    def test_a_light_is_switched_and_dimmed_through_its_slots(self):
        world = World(device("lamp", "Stehlampe", C.DEVICE_CLASS_LIGHT, slot(K.SLOT_KIND_ON, False),
                             slot(K.SLOT_KIND_BRIGHTNESS, 10.0)))

        assert world.run(intent("TurnOn", device_id="lamp")) == 1
        assert world.run(intent("SetLevel", device_id="lamp", value=30.0)) == 1

        assert world.slot_writes == [("lamp", "on", True), ("lamp", "brightness", 30.0)]
        assert world.state_writes == []

    def test_a_blind_uses_the_position_slot_in_the_canonical_scale(self):
        world = World(device("rollo", "Rollladen", C.DEVICE_CLASS_COVER, slot(K.SLOT_KIND_POSITION, 0.0)))

        world.run(intent("SetLevel", device_id="rollo", value=100, open_close=True, category="blind"))

        assert world.slot_writes == [("rollo", "position", 100.0)]

    def test_a_room_command_with_a_category_hits_only_that_category(self):
        world = World(
            device("a", "Decke", C.DEVICE_CLASS_LIGHT, slot(K.SLOT_KIND_ON, True)),
            device("b", "Dose", C.DEVICE_CLASS_SOCKET, slot(K.SLOT_KIND_ON, True)),
        )

        assert world.run(intent("TurnOff", category="light")) == 1
        assert world.slot_writes == [("a", "on", False)]


class TestColorAndColorTemperatureDoNotCollide:
    @pytest.fixture
    def world(self):
        return World(device("lamp", "Decke", C.DEVICE_CLASS_LIGHT, slot(K.SLOT_KIND_ON, True),
                            slot(K.SLOT_KIND_COLOR, 0), slot(K.SLOT_KIND_COLOR_TEMPERATURE, 2700.0)))

    def test_a_color_word_sets_the_color_slot(self, world):
        world.run(intent("SetColor", device_id="lamp", value="#FF0000"))

        assert world.slot_writes == [("lamp", "color", 0xFF0000)]

    def test_warm_and_cold_white_set_the_color_temperature_in_kelvin(self, world):
        world.run(intent("SetColor", device_id="lamp", value="warm"))
        world.run(intent("SetColor", device_id="lamp", value="kalt"))

        assert world.slot_writes == [("lamp", "color_temperature", 2700.0), ("lamp", "color_temperature", 6500.0)]

    def test_a_lamp_without_color_temperature_says_so(self):
        world = World(device("lamp", "Decke", C.DEVICE_CLASS_LIGHT, slot(K.SLOT_KIND_ON, True), slot(K.SLOT_KIND_COLOR, 0)))
        unsupported = []

        count = world.run(intent("SetColor", device_id="lamp", value="warm"), unsupported=unsupported)

        assert (count, world.slot_writes) == (0, [])
        assert unsupported == ["Decke im Wohnzimmer lässt sich nicht auf Warm- oder Kaltweiß stellen."]


class TestInvertedBlindsOfAV1Adapter:
    """#270: öffnen/schließen sind kanonisch (100 = offen), ein genannter Prozentwert geht unverändert raus."""

    @pytest.fixture
    def world(self):
        return World(device("markise", "Markise", C.DEVICE_CLASS_COVER,
                            slot(K.SLOT_KIND_POSITION, 70.0, state_id="x.Markise.level", inverted=True), origin="v1"))

    def test_open_is_written_as_the_actuators_zero(self, world):
        world.run(intent("SetLevel", device_id="markise", value=100, open_close=True))

        assert world.state_writes == [("x.Markise.level", 0)]

    def test_a_named_percentage_is_not_converted(self, world):
        world.run(intent("SetLevel", device_id="markise", value=30.0))

        assert world.state_writes == [("x.Markise.level", 30)]

    def test_a_category_command_converts_only_the_inverted_blinds(self):
        world = World(
            device("markise", "Markise", C.DEVICE_CLASS_COVER,
                   slot(K.SLOT_KIND_POSITION, 70.0, state_id="x.Markise.level", inverted=True), origin="v1"),
            device("rollo", "Rollo", C.DEVICE_CLASS_COVER,
                   slot(K.SLOT_KIND_POSITION, 20.0, state_id="x.Rollo.level"), origin="v1"),
        )

        assert world.run(intent("SetLevel", value=100, open_close=True, category="blind")) == 2

        assert sorted(world.state_writes) == [("x.Markise.level", 0), ("x.Rollo.level", 100)]


class TestTrustLevelPerSlot:
    @pytest.fixture
    def world(self):
        return World(device("tor", "Garagentor", C.DEVICE_CLASS_GENERIC_BINARY_SWITCH, slot(K.SLOT_KIND_ON, False, trust=8)))

    def test_a_guest_is_denied_and_nothing_is_sent(self, world):
        denied = []

        count = world.run(intent("TurnOn", device_id="tor"), trust_level=0, denied=denied)

        assert (count, world.slot_writes, denied) == (0, [], ["Garagentor im Wohnzimmer"])

    def test_a_trusted_user_may(self, world):
        assert world.run(intent("TurnOn", device_id="tor"), trust_level=9) == 1

    def test_a_room_command_skips_only_the_protected_device(self):
        world = World(
            device("tor", "Garagentor", C.DEVICE_CLASS_GENERIC_BINARY_SWITCH, slot(K.SLOT_KIND_ON, False, trust=8)),
            device("lampe", "Decke", C.DEVICE_CLASS_LIGHT, slot(K.SLOT_KIND_ON, False)),
        )
        denied = []

        count = world.run(intent("TurnOn"), trust_level=0, denied=denied)

        assert (count, world.slot_writes, denied) == (1, [("lampe", "on", True)], ["Garagentor im Wohnzimmer"])

    def test_a_menu_command_of_a_guest_is_denied(self, world):
        with pytest.raises(TrustLevelDenied):
            world.client.control_direct("tor", "on", "true", trust_level=0)

        assert world.slot_writes == []

    def test_a_menu_command_of_a_trusted_user_is_sent(self, world):
        assert world.client.control_direct("tor", "on", "true", trust_level=10) is True
        assert world.slot_writes == [("tor", "on", True)]


class TestARoomClarificationFindsTheDevice:
    """#268: der Gerätename war in mehreren Räumen mehrdeutig, die Raum-Rückfrage hat ihn aufgelöst. Dann ist
    die device_id noch unbekannt und das Gerät wird im bestätigten Raum über seinen Suchbegriff gefunden."""

    def test_the_device_is_found_by_its_key_in_the_confirmed_room(self):
        world = World(device("pc", "Computer", C.DEVICE_CLASS_SOCKET, slot(K.SLOT_KIND_ON, False)))
        clarified = Intent(name="TurnOn", room="Wohnzimmer", room_id="wohnzimmer", device="computer", device_key="computer")

        assert world.run(clarified) == 1

        assert world.slot_writes == [("pc", "on", True)]
        assert clarified.device_id == "pc"

    def test_a_key_nobody_in_that_room_has_sets_nothing(self):
        world = World(device("pc", "Computer", C.DEVICE_CLASS_SOCKET, slot(K.SLOT_KIND_ON, False)))
        clarified = Intent(name="TurnOn", room="Wohnzimmer", room_id="wohnzimmer", device="drucker", device_key="drucker")

        assert (world.run(clarified), world.slot_writes) == (0, [])


class TestConfirmationOfAV2Device:
    @pytest.fixture
    def world(self):
        return World(device("lamp", "Stehlampe", C.DEVICE_CLASS_LIGHT, slot(K.SLOT_KIND_ON, False)))

    @staticmethod
    def ack(device_id, slot_id, **value):
        return pb.SlotUpdate(device_id=device_id, slot_id=slot_id, value=pb.SlotValue(**value), ack=True)

    def test_a_text_channel_waits_for_the_adapters_ack(self, world):
        import threading

        offline = []
        threading.Timer(0.05, lambda: world.client.handle_slot_ack(self.ack("lamp", "on", boolean=True))).start()

        count = world.run(intent("TurnOn", device_id="lamp"), wait_confirm=True, offline=offline)

        assert (count, offline) == (1, [])

    def test_no_ack_in_time_reports_the_device_as_offline(self, world):
        offline = []

        count = world.run(intent("TurnOn", device_id="lamp"), wait_confirm=True, offline=offline)

        assert (count, offline) == (1, ["Stehlampe im Wohnzimmer"])

    def test_a_different_value_than_asked_does_not_confirm(self, world):
        import threading

        offline = []
        threading.Timer(0.05, lambda: world.client.handle_slot_ack(self.ack("lamp", "on", boolean=False))).start()

        world.run(intent("TurnOn", device_id="lamp"), wait_confirm=True, offline=offline)

        assert offline == ["Stehlampe im Wohnzimmer"]

    def test_a_satellite_gets_its_feedback_when_the_ack_arrives(self, world):
        count = world.run(intent("TurnOn", device_id="lamp"), satellite_device="kueche-esp")
        assert (count, world.feedback) == (1, [])

        world.client.handle_slot_ack(self.ack("lamp", "on", boolean=True))

        assert [(device, success) for device, success, _ in world.feedback] == [("kueche-esp", True)]

    def test_the_requester_name_reaches_the_satellite_feedback_text(self, world, monkeypatch):
        monkeypatch.setattr("hannah.responses.random.random", lambda: 0.0)
        monkeypatch.setattr("hannah.responses.random.choice", lambda seq: seq[0])

        world.run(intent("TurnOn", device_id="lamp"), satellite_device="kueche-esp", requester_name="Leonie")
        world.client.handle_slot_ack(self.ack("lamp", "on", boolean=True))

        assert world.feedback == [("kueche-esp", True, "Sehr gerne, Leonie.")]

    def test_without_wait_confirm_nothing_is_reported_offline(self, world):
        offline = []

        count = world.run(intent("TurnOn", device_id="lamp"), offline=offline)

        assert (count, offline) == (1, [])

    def test_an_update_without_ack_confirms_nothing(self, world):
        world.run(intent("TurnOn", device_id="lamp"), satellite_device="kueche-esp")

        world.client.handle_slot_ack(pb.SlotUpdate(device_id="lamp", slot_id="on", value=pb.SlotValue(boolean=True)))

        assert world.feedback == []


def test_an_unreachable_adapter_counts_nothing():
    world = World(device("lamp", "Stehlampe", C.DEVICE_CLASS_LIGHT, slot(K.SLOT_KIND_ON, False)))
    world.controller._send_set_slot = lambda *_: False

    assert world.run(intent("TurnOn", device_id="lamp")) == 0


class TestWithoutTheOldDeviceTree:
    """#387 Schritt 6: Bestätigung und State-Werte brauchen den State-basierten Gerätebaum nicht mehr (#522: es gibt ihn nicht mehr)."""

    @pytest.fixture
    def world(self):
        return World(device("lampe", "Lampe", C.DEVICE_CLASS_LIGHT,
                            slot(K.SLOT_KIND_ON, False, state_id="x.Lampe.on"),
                            slot(K.SLOT_KIND_BRIGHTNESS, 40.0, state_id="x.Lampe.level"), origin="v1"))

    def test_a_state_update_confirms_a_command_for_a_satellite(self, world):
        world.run(intent("TurnOn", device_id="lampe"), satellite_device="kueche-esp")
        world.client.handle_state_update("x.Lampe.on", "true")

        assert [(device, success) for device, success, _ in world.feedback] == [("kueche-esp", True)]

    def test_a_different_value_reports_a_failure(self, world):
        world.run(intent("TurnOn", device_id="lampe"), satellite_device="kueche-esp")

        world.client.handle_state_update("x.Lampe.on", "false")

        assert [(device, success) for device, success, _ in world.feedback] == [("kueche-esp", False)]

    def test_get_state_raw_reads_the_registry(self, world):
        assert world.client.get_state_raw("x.Lampe.level") == "40"
        assert world.client.get_state_raw("x.Lampe.on") == "False"
        assert world.client.get_state_raw("x.gibt.es.nicht") is None


class TestUnreachableDevice:
    """#523: ein Gerät, das sich als nicht erreichbar meldet, bekommt keinen Befehl und wird so genannt."""

    @staticmethod
    def lamp(name="Decke", device_id="d1", available=True):
        d = device(device_id, name, C.DEVICE_CLASS_LIGHT, slot(K.SLOT_KIND_ON, False))
        d.available = available
        return d

    def test_nothing_is_sent_and_the_device_is_named(self):
        world = World(self.lamp(available=False))
        unsupported: list = []

        count = world.run(intent("TurnOn", device_id="d1"), unsupported=unsupported, wait_confirm=True)

        assert count == 0
        assert world.slot_writes == []
        assert unsupported == ["Decke im Wohnzimmer ist gerade nicht erreichbar."]

    def test_no_waiting_for_a_confirmation_that_cannot_come(self):
        world = World(self.lamp(available=False))
        offline: list = []
        start = time.monotonic()

        world.run(intent("TurnOn", device_id="d1"), offline=offline, wait_confirm=True)

        assert time.monotonic() - start < 0.1
        assert offline == ["Decke im Wohnzimmer"]

    def test_a_collective_command_reaches_the_others_and_names_the_unreachable_one_as_offline(self):
        world = World(self.lamp("Decke", "d1"), self.lamp("Stehlampe", "d2", available=False))
        offline: list = []

        count = world.run(intent("TurnOn", category="light"), offline=offline)

        assert count == 1
        assert world.slot_writes == [("d1", "on", True)]
        assert offline == ["Stehlampe im Wohnzimmer"]

    def test_a_reachable_device_is_switched_as_before(self):
        world = World(self.lamp())
        unsupported: list = []

        assert world.run(intent("TurnOn", device_id="d1"), unsupported=unsupported) == 1
        assert unsupported == []
