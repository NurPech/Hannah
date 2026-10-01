"""#386: Geräte über Slots steuern."""
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import grpc
import pytest
from hannah_proto.v2 import hannah_pb2 as pb
from hannah_proto.v2 import hannah_pb2_grpc as pb_grpc

from hannah.device_control import DeviceController, SlotWrite
from hannah.grpc_server import GrpcServer, HannahServicer
from hannah.iobroker import TRUST_DENIED_TEXT, TrustLevelDenied
from hannah.typed_devices import (
    ORIGIN_V1, ORIGIN_V2, DeviceRegistry, Slot, TypedDevice, device_info_to_pb, slot_value_to_pb,
)

K = pb.SlotKind


def _light(origin=ORIGIN_V2, **overrides) -> TypedDevice:
    state = (lambda name: f"x.lamp.{name}") if origin == ORIGIN_V1 else (lambda name: "")
    slots = {
        "on": Slot("on", K.SLOT_KIND_ON, True, writable=True, state_id=state("on")),
        "brightness": Slot("brightness", K.SLOT_KIND_BRIGHTNESS, 60.0, writable=True, state_id=state("level")),
        "color": Slot("color", K.SLOT_KIND_COLOR, 0x112233, writable=True, state_id=state("color")),
        "power": Slot("power", K.SLOT_KIND_POWER, 4.0, writable=False, state_id=state("power")),
        "lock": Slot("lock", K.SLOT_KIND_GENERIC_BOOL, False, writable=True, required_trust_level=8, state_id=state("lock")),
    }
    device = TypedDevice("lamp", "Decke", "wohnzimmer", device_class=pb.DEVICE_CLASS_LIGHT, slots=slots, origin=origin)
    for key, value in overrides.items():
        setattr(device, key, value)
    return device


def _controller(*devices, send_set_slot=None, send_set_state=None, timeout=0.3):
    registry = DeviceRegistry()
    for origin in (ORIGIN_V1, ORIGIN_V2):
        registry.replace(origin, [d for d in devices if d.origin == origin])
    send_set_slot = send_set_slot or MagicMock(return_value=True)
    send_set_state = send_set_state or MagicMock(return_value=True)
    return DeviceController(registry, send_set_slot=send_set_slot, send_set_state=send_set_state,
                            confirm_timeout=timeout), registry, send_set_slot, send_set_state


# ------------------------------------------------------------------
# Werte

class TestSlotValueToPb:
    def test_each_kind_takes_its_value_type(self):
        assert slot_value_to_pb(K.SLOT_KIND_ON, True).boolean is True
        assert slot_value_to_pb(K.SLOT_KIND_BRIGHTNESS, 40).number == 40.0
        assert slot_value_to_pb(K.SLOT_KIND_COLOR, 0xFF0000).rgb == 0xFF0000
        assert slot_value_to_pb(K.SLOT_KIND_COLOR, "#00FF00").rgb == 0x00FF00
        assert slot_value_to_pb(K.SLOT_KIND_GENERIC_TEXT, "x").text == "x"

    @pytest.mark.parametrize("kind,value", [
        (K.SLOT_KIND_ON, 1), (K.SLOT_KIND_ON, "true"), (K.SLOT_KIND_BRIGHTNESS, True),
        (K.SLOT_KIND_BRIGHTNESS, "50"), (K.SLOT_KIND_COLOR, -1), (K.SLOT_KIND_COLOR, "#ZZZZZZ"),
        (K.SLOT_KIND_COLOR, 0x1000000), (K.SLOT_KIND_ON, None),
    ])
    def test_a_value_that_does_not_fit_is_rejected(self, kind, value):
        with pytest.raises(ValueError):
            slot_value_to_pb(kind, value)


# ------------------------------------------------------------------
# hannah.v2: SetSlot und Bestätigung

class TestV2Device:
    def test_sends_set_slot_with_the_typed_value(self):
        controller, _, send_slot, send_state = _controller(_light(ORIGIN_V2))

        result = controller.set_slot("lamp", "brightness", 25, trust_level=0)

        assert result == SlotWrite(found=True, sent=True, confirmed=None)
        (device_id, slot_id, value), _ = send_slot.call_args
        assert (device_id, slot_id, value.number) == ("lamp", "brightness", 25.0)
        send_state.assert_not_called()

    def test_not_sent_when_no_v2_adapter_is_connected(self):
        controller, _, _, _ = _controller(_light(ORIGIN_V2), send_set_slot=MagicMock(return_value=False))

        assert controller.set_slot("lamp", "on", False, trust_level=0) == SlotWrite(found=True, sent=False, confirmed=None)

    def test_confirmed_by_a_slot_update_with_ack(self):
        controller, _, _, _ = _controller(_light(ORIGIN_V2), timeout=2.0)
        confirm = lambda ack: controller.handle_slot_update(pb.SlotUpdate(device_id="lamp", slot_id="on", ack=ack))

        def adapter_answers(*_):
            threading.Timer(0.05, confirm, args=(True,)).start()
            return True

        controller._send_set_slot = adapter_answers
        result = controller.set_slot("lamp", "on", False, trust_level=0, wait_confirm=True)

        assert result == SlotWrite(found=True, sent=True, confirmed=True)

    def test_an_update_without_ack_does_not_confirm(self):
        controller, _, _, _ = _controller(_light(ORIGIN_V2), timeout=0.2)

        def adapter_answers(*_):
            threading.Timer(0.02, controller.handle_slot_update,
                            args=(pb.SlotUpdate(device_id="lamp", slot_id="on", ack=False),)).start()
            return True

        controller._send_set_slot = adapter_answers
        result = controller.set_slot("lamp", "on", False, trust_level=0, wait_confirm=True)

        assert result.sent and result.confirmed is False

    def test_a_confirmation_for_another_slot_does_not_count(self):
        controller, _, _, _ = _controller(_light(ORIGIN_V2), timeout=0.2)

        def adapter_answers(*_):
            threading.Timer(0.02, controller.handle_slot_update,
                            args=(pb.SlotUpdate(device_id="lamp", slot_id="brightness", ack=True),)).start()
            return True

        controller._send_set_slot = adapter_answers

        assert controller.set_slot("lamp", "on", False, trust_level=0, wait_confirm=True).confirmed is False

    def test_the_waiter_is_gone_after_the_call(self):
        controller, _, _, _ = _controller(_light(ORIGIN_V2), timeout=0.05)

        controller.set_slot("lamp", "on", False, trust_level=0, wait_confirm=True)

        assert controller._waiters == {}

    def test_no_waiting_when_the_command_did_not_go_out(self):
        controller, _, _, _ = _controller(_light(ORIGIN_V2), send_set_slot=MagicMock(return_value=False), timeout=5)

        started = time.monotonic()
        result = controller.set_slot("lamp", "on", False, trust_level=0, wait_confirm=True)

        assert result.sent is False and result.confirmed is None
        assert time.monotonic() - started < 1


# ------------------------------------------------------------------
# Trust-Level pro Slot, Schreibbarkeit, unbekannt

class TestChecks:
    def test_trust_level_is_checked_per_slot(self):
        controller, _, send_slot, _ = _controller(_light(ORIGIN_V2))

        controller.set_slot("lamp", "on", True, trust_level=0)           # kein Trust nötig
        with pytest.raises(TrustLevelDenied):
            controller.set_slot("lamp", "lock", True, trust_level=5)     # braucht 8
        controller.set_slot("lamp", "lock", True, trust_level=8)

        assert send_slot.call_count == 2

    def test_no_user_is_not_checked_and_an_explicit_zero_requirement_admits_guests(self):
        device = _light(ORIGIN_V2)
        device.slots["lock"].required_trust_level = 0
        controller, _, send_slot, _ = _controller(device)

        controller.set_slot("lamp", "lock", True, trust_level=0)
        controller.set_slot("lamp", "lock", True, trust_level=None)

        assert send_slot.call_count == 2

    def test_a_denied_command_sends_nothing(self):
        controller, _, send_slot, send_state = _controller(_light(ORIGIN_V1))

        with pytest.raises(TrustLevelDenied):
            controller.set_slot("lamp", "lock", True, trust_level=1)

        send_slot.assert_not_called()
        send_state.assert_not_called()

    def test_read_only_slot_is_not_written(self):
        controller, _, send_slot, _ = _controller(_light(ORIGIN_V2))

        assert controller.set_slot("lamp", "power", 5, trust_level=0) == SlotWrite(found=True, sent=False)
        send_slot.assert_not_called()

    def test_unknown_device_or_slot(self):
        controller, _, _, _ = _controller(_light(ORIGIN_V2))

        assert controller.set_slot("nope", "on", True, trust_level=0) == SlotWrite(found=False, sent=False)
        assert controller.set_slot("lamp", "nope", True, trust_level=0) == SlotWrite(found=False, sent=False)

    def test_a_value_that_does_not_fit_the_slot_is_an_error(self):
        controller, _, send_slot, _ = _controller(_light(ORIGIN_V2))

        with pytest.raises(ValueError):
            controller.set_slot("lamp", "brightness", True, trust_level=0)

        send_slot.assert_not_called()


# ------------------------------------------------------------------
# hannah.v1: SetState wie vorher

class TestV1Device:
    def test_sends_set_state_to_the_slots_state_and_not_set_slot(self):
        controller, _, send_slot, send_state = _controller(_light(ORIGIN_V1))

        result = controller.set_slot("lamp", "on", False, trust_level=0)

        assert result == SlotWrite(found=True, sent=True, confirmed=None)
        send_state.assert_called_once_with("x.lamp.on", False)
        send_slot.assert_not_called()

    @pytest.mark.parametrize("slot_id,value,expected", [
        ("brightness", 50, 50), ("brightness", 12.5, 12.5), ("brightness", 50.0, 50),
        ("color", 0xFF0000, "#FF0000"), ("color", "#00ff00", "#00FF00"), ("color", 0x0000FF, "#0000FF"),
    ])
    def test_values_go_out_in_the_v1_format(self, slot_id, value, expected):
        controller, _, _, send_state = _controller(_light(ORIGIN_V1))

        controller.set_slot("lamp", slot_id, value, trust_level=0)

        (_, wire), _ = send_state.call_args
        assert wire == expected and type(wire) is type(expected)

    def test_inverted_cover_is_converted_back(self):
        cover = TypedDevice("rollo", "Rollo", "bad", device_class=pb.DEVICE_CLASS_COVER, origin=ORIGIN_V1, slots={
            "position": Slot("position", K.SLOT_KIND_POSITION, 100.0, writable=True, state_id="x.rollo.level", inverted=True),
        })
        controller, _, _, send_state = _controller(cover)

        controller.set_slot("rollo", "position", 100, trust_level=0)   # 100 = offen
        controller.set_slot("rollo", "position", 30, trust_level=0)

        assert [c.args for c in send_state.call_args_list] == [("x.rollo.level", 0), ("x.rollo.level", 70)]

    def test_cover_that_is_not_inverted_is_passed_through(self):
        cover = TypedDevice("rollo", "Rollo", "bad", origin=ORIGIN_V1, slots={
            "position": Slot("position", K.SLOT_KIND_POSITION, 0.0, writable=True, state_id="x.rollo.level"),
        })
        controller, _, _, send_state = _controller(cover)

        controller.set_slot("rollo", "position", 30, trust_level=0)

        send_state.assert_called_once_with("x.rollo.level", 30)

    def test_the_registry_shows_the_new_value_right_away(self):
        controller, registry, _, _ = _controller(_light(ORIGIN_V1))

        controller.set_slot("lamp", "brightness", 10, trust_level=0)

        assert registry.get("lamp").slots["brightness"].value == 10.0

    def test_a_command_that_did_not_go_out_leaves_the_value_alone(self):
        controller, registry, _, _ = _controller(_light(ORIGIN_V1), send_set_state=MagicMock(return_value=False))

        assert controller.set_slot("lamp", "brightness", 10, trust_level=0).sent is False
        assert registry.get("lamp").slots["brightness"].value == 60.0


# ------------------------------------------------------------------
# Servicer: agent_set_slot, ControlDevice und GetDevices

def _servicer(**kwargs) -> HannahServicer:
    return HannahServicer(
        user_manager=kwargs.pop("user_manager", MagicMock()), satellite_manager=MagicMock(), handle_text=MagicMock(),
        handle_voice=MagicMock(), announce=MagicMock(), notificate=MagicMock(), get_satellites=MagicMock(),
        get_car_state=MagicMock(), **kwargs,
    )


class _Session:
    def __init__(self, generation):
        self.generation = generation
        self.commands = []

    def put(self, command, presence_state=None):
        self.commands.append(command)


class TestAgentSetSlot:
    def test_only_v2_adapters_get_the_command(self):
        servicer = _servicer()
        v1, v2 = _Session("v1"), _Session("v2")
        servicer._agent_sessions = [v1, v2]

        assert servicer.agent_set_slot("d", "on", pb.SlotValue(boolean=True)) is True

        assert v1.commands == []
        [command] = v2.commands
        assert (command.set_slot.device_id, command.set_slot.slot_id, command.set_slot.value.boolean) == ("d", "on", True)

    def test_false_without_a_v2_adapter(self):
        servicer = _servicer()
        servicer._agent_sessions = [_Session("v1")]

        assert servicer.agent_set_slot("d", "on", pb.SlotValue(boolean=True)) is False


class TestControlDeviceNative:
    def _user_manager(self, trust=8):
        return MagicMock(get_user_by_linked_account=MagicMock(return_value=SimpleNamespace(id=1, trust_level=trust)))

    def _request(self, **kwargs):
        return pb.ControlDeviceRequest(device_id="lamp", slot_id="brightness", value=pb.SlotValue(number=40),
                                       source_service="telegram", source_user_id="42", **kwargs)

    def test_the_request_goes_to_the_slot_with_the_trust_level_of_the_user(self):
        control_slot = MagicMock(return_value=SlotWrite(True, True))
        servicer = _servicer(user_manager=self._user_manager(8), control_slot=control_slot)

        response = servicer.ControlDevice(self._request(), MagicMock())

        assert response.ok is True and response.message == "OK"
        control_slot.assert_called_once_with("lamp", "brightness", 40.0, 8)

    def test_unlinked_requester_is_a_guest(self):
        control_slot = MagicMock(return_value=SlotWrite(True, True))
        servicer = _servicer(user_manager=self._user_manager(8), control_slot=control_slot)

        servicer.ControlDevice(pb.ControlDeviceRequest(device_id="lamp", slot_id="on", value=pb.SlotValue(boolean=True)), MagicMock())

        assert control_slot.call_args.args[3] == 0

    def test_denied_request_gets_the_rejection_text(self):
        servicer = _servicer(control_slot=MagicMock(side_effect=TrustLevelDenied("x")))

        response = servicer.ControlDevice(self._request(), MagicMock())

        assert response.ok is False and response.message == TRUST_DENIED_TEXT

    def test_a_value_that_does_not_fit_is_reported(self):
        servicer = _servicer(control_slot=MagicMock(side_effect=ValueError("keine Zahl")))

        response = servicer.ControlDevice(self._request(), MagicMock())

        assert response.ok is False and "keine Zahl" in response.message

    def test_not_sent_is_not_ok(self):
        servicer = _servicer(control_slot=MagicMock(return_value=SlotWrite(True, False)))

        assert servicer.ControlDevice(self._request(), MagicMock()).ok is False

    def test_a_device_the_registry_does_not_know_falls_back_to_the_state_based_path(self):
        control_device = MagicMock(return_value=True)
        servicer = _servicer(
            user_manager=MagicMock(get_user_by_linked_account=MagicMock(return_value=None)),
            control_slot=MagicMock(return_value=SlotWrite(False, False)), control_device=control_device,
        )

        response = servicer.ControlDevice(self._request(), MagicMock())

        assert response.ok is True
        control_device.assert_called_once_with("lamp", "level", "40", 0)


class TestGetDevicesNative:
    def test_devices_come_from_the_registry_grouped_by_room(self):
        devices = [
            _light(ORIGIN_V2, room="wohnzimmer"),
            TypedDevice("fenster", "Fenster", "bad", device_class=pb.DEVICE_CLASS_CONTACT, slots={
                "open": Slot("open", K.SLOT_KIND_OPEN, False),
            }),
        ]
        servicer = _servicer(
            get_typed_devices=lambda: devices,
            get_rooms=lambda: [{"room_id": "wohnzimmer", "display_name": "Wohnzimmer"}],
        )

        response = servicer.GetDevices(None, None)

        assert [(r.key, r.name) for r in response.rooms] == [("bad", "bad"), ("wohnzimmer", "Wohnzimmer")]
        light = response.rooms[1].devices[0]
        assert (light.id, light.name, light.device_class, light.available) == ("lamp", "Decke", pb.DEVICE_CLASS_LIGHT, True)
        slots = {s.slot_id: s for s in light.slots}
        assert slots["brightness"].value.number == 60.0 and slots["brightness"].writable
        assert slots["color"].value.rgb == 0x112233
        assert slots["lock"].required_trust_level == 8 and not slots["on"].HasField("required_trust_level")

    def test_an_empty_registry_falls_back_to_the_state_based_list(self):
        rooms = [{"key": "bad", "name": "Bad", "devices": [{
            "id": "x.lamp", "name": "Decke", "category": "light", "states": ["on"], "current": {"on": "True"},
            "state_types": {"on": 1}, "state_enum_values": {}, "state_writable": {"on": True},
        }]}]
        servicer = _servicer(get_typed_devices=lambda: [], get_devices=lambda: rooms)

        response = servicer.GetDevices(None, None)

        [device] = response.rooms[0].devices
        assert device.device_class == pb.DEVICE_CLASS_LIGHT and device.slots[0].slot_id == "on"

    def test_unknown_value_is_left_unset(self):
        device = TypedDevice("l", "L", "bad", slots={"on": Slot("on", K.SLOT_KIND_ON, None)})

        info = device_info_to_pb(device)

        assert info.slots[0].value.WhichOneof("value") is None


# ------------------------------------------------------------------
# End-to-end: ein v2-Adapter bekommt SetSlot und bestätigt mit ack

def test_a_v2_adapter_receives_set_slot_and_confirms_it():
    registry = DeviceRegistry()
    registry.handle_typed_snapshot([pb.TypedDevice(
        device_id="lamp", name="Decke", room="bad", device_class=pb.DEVICE_CLASS_LIGHT, available=True,
        slots=[pb.Slot(slot_id="brightness", kind=K.SLOT_KIND_BRIGHTNESS, writable=True)],
    )])
    holder = {}
    controller = DeviceController(
        registry, send_set_slot=lambda *a: holder["servicer"].agent_set_slot(*a),
        send_set_state=MagicMock(return_value=True), confirm_timeout=5.0,
    )
    servicer = _servicer(
        get_typed_devices=registry.devices,
        control_slot=lambda d, s, v, t: controller.set_slot(d, s, v, trust_level=t, wait_confirm=True),
        on_agent_slot_update=controller.handle_slot_update,
    )
    holder["servicer"] = servicer
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = GrpcServer({"host": "127.0.0.1", "port": port}, servicer)
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    stub = pb_grpc.HannahServiceStub(channel)
    release = threading.Event()

    def adapter_messages():
        yield pb.AgentMessage(device_availability=pb.DeviceAvailability(device_id="lamp", available=True))
        release.wait(10)

    try:
        stream = stub.AgentConnect(adapter_messages(), timeout=15)
        deadline = time.monotonic() + 5
        while not servicer.agent_connected() and time.monotonic() < deadline:
            time.sleep(0.02)

        responses = []
        caller = threading.Thread(target=lambda: responses.append(stub.ControlDevice(
            pb.ControlDeviceRequest(device_id="lamp", slot_id="brightness", value=pb.SlotValue(number=35)), timeout=10)))
        caller.start()
        command = next(stream)
        assert (command.set_slot.device_id, command.set_slot.slot_id, command.set_slot.value.number) == ("lamp", "brightness", 35.0)

        # der Adapter bestätigt mit einem SlotUpdate ack=true, über einen zweiten Stream derselben Verbindung
        confirm_stream = stub.AgentConnect(iter([pb.AgentMessage(slot_update=pb.SlotUpdate(
            device_id="lamp", slot_id="brightness", value=pb.SlotValue(number=35), ack=True))]), timeout=5)
        list(confirm_stream)
        caller.join(10)
    finally:
        release.set()
        channel.close()
        server.stop()

    assert responses and responses[0].ok is True
