"""#387 Schritt 6: hannah.v1-Clients (Telegram, WebUI) bekommen GetDevices aus der typisierten Registry
und steuern darüber, auch Geräte eines hannah.v2-Adapters."""
import socket
from unittest.mock import MagicMock

import grpc
import pytest
from google.protobuf.empty_pb2 import Empty
from hannah_proto.v1 import hannah_pb2 as pb1
from hannah_proto.v1 import hannah_pb2_grpc as pb1_grpc
from hannah_proto.v2 import hannah_pb2 as pb

from hannah import legacy_devices
from hannah.device_control import DeviceController
from hannah.grpc_server import GrpcServer, HannahServicer
from hannah.iobroker import IoBrokerClient
from hannah.typed_devices import DeviceRegistry, Slot, TypedDevice, slot_value_from_pb

C = pb.DeviceClass
K = pb.SlotKind
BASE = "javascript.0.virtualDevice.Licht.EG.Wohnzimmer.Decke"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _light_state(suffix, canonical, value, state_type):
    return pb1.AgentDevice(
        state_id=f"{BASE}.{suffix}", device_id=BASE, canonical_key=canonical, room="wohnzimmer", device="Decke",
        device_type="light", value=pb1.AgentStateValue(value=value, ack=True), room_names={"de": "Wohnzimmer"},
        state_type=state_type, writable=True,
    )


def _slot(kind, value, writable=True, options=None):
    return Slot(slot_id=pb.SlotKind.Name(kind)[len("SLOT_KIND_"):].lower(), kind=kind, value=value, writable=writable,
                options=list(options or []))


class Setup:
    def __init__(self):
        snapshot = [_light_state("on", "on", "true", pb1.StateType.BOOLEAN),
                    _light_state("level", "level", "60", pb1.StateType.NUMERIC)]
        self.iobroker = IoBrokerClient({})
        self.iobroker.handle_device_snapshot(snapshot)
        self.iobroker.set_setter(lambda *_: True)
        self.registry = DeviceRegistry()
        legacy_devices.load_snapshot(self.registry, snapshot)
        self.registry.replace("v2", [
            TypedDevice("klima", "Klimaanlage", "wohnzimmer", device_class=C.DEVICE_CLASS_CLIMATE, slots={
                "on": _slot(K.SLOT_KIND_ON, True),
                "mode": _slot(K.SLOT_KIND_MODE, "cool", options=["cool", "dry"]),
                "fan_speed": _slot(K.SLOT_KIND_FAN_SPEED, "auto"),
                "target_temperature": _slot(K.SLOT_KIND_TARGET_TEMPERATURE, 22.0),
            }),
            TypedDevice("tuer", "Balkontür", "wohnzimmer", device_class=C.DEVICE_CLASS_CONTACT,
                        subtype=pb.DEVICE_SUBTYPE_DOOR, slots={"open": _slot(K.SLOT_KIND_OPEN, False, writable=False)}),
        ])
        self.slot_writes: list = []
        self.state_writes: list = []
        self.controller = DeviceController(
            self.registry,
            send_set_slot=lambda device_id, slot_id, value: self.slot_writes.append(
                (device_id, slot_id, slot_value_from_pb(value))) or True,
            send_set_state=lambda state_id, value: self.state_writes.append((state_id, value)) or True,
        )
        self.iobroker.set_slot_control(self.registry, self.controller)


@pytest.fixture
def setup():
    return Setup()


@pytest.fixture
def channel(setup):
    user_manager = MagicMock(get_user_by_linked_account=MagicMock(return_value=MagicMock(id=1, trust_level=8)))
    servicer = HannahServicer(
        user_manager=user_manager, satellite_manager=MagicMock(), handle_text=MagicMock(), handle_voice=MagicMock(),
        announce=MagicMock(), notificate=MagicMock(), get_satellites=MagicMock(), get_car_state=MagicMock(),
        get_devices=setup.iobroker.get_devices_snapshot,
        control_device=lambda device_id, state, value, trust: setup.iobroker.control_direct(
            device_id, state, value, trust_level=trust),
        get_typed_devices=setup.registry.devices,
        get_rooms=lambda: [{"room_id": "wohnzimmer", "display_name": "Wohnzimmer"}],
    )
    port = _free_port()
    server = GrpcServer({"host": "127.0.0.1", "port": port}, servicer)
    server.start()
    ch = grpc.insecure_channel(f"127.0.0.1:{port}")
    yield ch
    ch.close()
    server.stop()


def _control(channel, device_id, state, value):
    return pb1_grpc.HannahServiceStub(channel).ControlDevice(
        pb1.ControlDeviceRequest(device_id=device_id, state=state, value=value, source_service="telegram",
                                 source_user_id="42"),
        timeout=5, metadata=(("x-compat-version", "2"),),
    )


class TestGetDevices:
    def test_a_v1_client_sees_the_devices_of_both_adapter_generations(self, channel):
        response = pb1_grpc.HannahServiceStub(channel).GetDevices(Empty(), timeout=5)

        [room] = response.rooms
        assert room.name == "Wohnzimmer"
        assert {d.name for d in room.devices} == {"Decke", "Klimaanlage", "Balkontür"}

    def test_a_light_keeps_its_v1_state_keys_and_values(self, channel):
        room = pb1_grpc.HannahServiceStub(channel).GetDevices(Empty(), timeout=5).rooms[0]
        light = next(d for d in room.devices if d.name == "Decke")

        assert light.category == "light"
        assert sorted(light.states) == ["level", "on"]
        assert dict(light.current) == {"on": "true", "level": "60"}
        assert light.state_types["on"] == pb1.StateType.BOOLEAN

    def test_a_climate_device_is_a_v1_climate_with_enum_states(self, channel):
        room = pb1_grpc.HannahServiceStub(channel).GetDevices(Empty(), timeout=5).rooms[0]
        klima = next(d for d in room.devices if d.name == "Klimaanlage")

        assert klima.category == "climate"
        assert set(klima.states) == {"on", "mode", "fanSpeed", "expected"}
        assert klima.state_types["mode"] == pb1.StateType.ENUM
        assert set(klima.state_enum_values["mode"].values) == {"cool", "dry"}
        assert klima.current["mode"] == "cool"

    def test_a_door_is_a_door(self, channel):
        room = pb1_grpc.HannahServiceStub(channel).GetDevices(Empty(), timeout=5).rooms[0]

        assert next(d for d in room.devices if d.name == "Balkontür").category == "door"


class TestControlDevice:
    def test_a_v1_client_controls_a_device_of_a_v2_adapter_by_its_state_key(self, setup, channel):
        assert _control(channel, "klima", "fanSpeed", "high").ok is True
        assert _control(channel, "klima", "mode", "dry").ok is True
        assert _control(channel, "klima", "expected", "21.5").ok is True

        assert setup.slot_writes == [("klima", "fan_speed", "high"), ("klima", "mode", "dry"),
                                     ("klima", "target_temperature", 21.5)]

    def test_a_v1_client_controls_a_device_of_a_v1_adapter_through_its_state_id(self, setup, channel):
        assert _control(channel, BASE, "level", "30").ok is True

        assert [(sid, value) for sid, value in setup.state_writes] == [(f"{BASE}.level", 30)]

    def test_an_unknown_state_key_is_refused(self, setup, channel):
        response = _control(channel, "klima", "gibt-es-nicht", "1")

        assert response.ok is False and setup.slot_writes == []

    def test_a_read_only_slot_is_refused(self, setup, channel):
        assert _control(channel, "tuer", "open", "true").ok is False

    def test_a_value_of_the_wrong_kind_is_refused(self, setup, channel):
        assert _control(channel, "klima", "expected", "warm").ok is False
        assert setup.slot_writes == []


class TestLegacyFallbackWithoutARegistry:
    def test_an_empty_registry_serves_the_old_device_list(self):
        iobroker = IoBrokerClient({})
        iobroker.handle_device_snapshot([_light_state("on", "on", "true", pb1.StateType.BOOLEAN)])
        servicer = HannahServicer(
            user_manager=MagicMock(), satellite_manager=MagicMock(), handle_text=MagicMock(), handle_voice=MagicMock(),
            announce=MagicMock(), notificate=MagicMock(), get_satellites=MagicMock(), get_car_state=MagicMock(),
            get_devices=iobroker.get_devices_snapshot, get_typed_devices=DeviceRegistry().devices,
        )

        [room] = servicer.devices_response_v1().rooms

        assert room.devices[0].category == "light"
