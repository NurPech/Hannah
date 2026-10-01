"""
#384: Core runs internally on hannah.v2 and serves the frozen hannah.v1 (N−1) with the
same handlers through hannah.grpc_v1 — translated per message for what hasn't changed,
native for the State-based device messages (GetDevices, ControlDevice, AgentConnect).
"""
import logging
import socket
import threading
import time
from unittest.mock import MagicMock

import grpc
import pytest
from google.protobuf.descriptor import Descriptor, FieldDescriptor
from google.protobuf.empty_pb2 import Empty
from hannah_proto.v1 import hannah_pb2 as pb1
from hannah_proto.v1 import hannah_pb2_grpc as pb1_grpc
from hannah_proto.v2 import hannah_pb2 as pb
from hannah_proto.v2 import hannah_pb2_grpc as pb_grpc

from hannah import grpc_v1
from hannah.grpc_server import GrpcServer, HannahServicer
from hannah.iobroker import IoBrokerClient


# ------------------------------------------------------------------
# Descriptor check — a v1 request reaches the v2 handlers through the lib's generic
# by-name copy, strictly: a field v1 sets that v2 doesn't have fails the call. Catch
# that here, for every non-native method, instead of at runtime. If this fails, the
# named method needs an explicit translation (or a native v1 handler).

def _assert_fields_translatable(v1: Descriptor, v2: Descriptor, path: str, seen: set) -> None:
    if (v1.full_name, v2.full_name) in seen:
        return
    seen = seen | {(v1.full_name, v2.full_name)}
    for field in v1.fields:
        other = v2.fields_by_name.get(field.name)
        where = f"{path}.{field.name}"
        assert other is not None, f"{where} has no counterpart in {v2.full_name}"
        assert (other.type, other.is_repeated) == (field.type, field.is_repeated), f"{where} type/cardinality differ"
        if field.type == FieldDescriptor.TYPE_MESSAGE:
            _assert_fields_translatable(field.message_type, other.message_type, where, seen)
        elif field.type == FieldDescriptor.TYPE_ENUM:
            missing = {v.name for v in field.enum_type.values} - {v.name for v in other.enum_type.values}
            assert not missing, f"{where} enum values {sorted(missing)} missing in {other.enum_type.full_name}"


@pytest.mark.parametrize(
    "method", [m for m in grpc_v1.V1_SERVICE.methods if m.name not in grpc_v1._NATIVE], ids=lambda m: m.name,
)
def test_translated_method_requests_are_translatable(method):
    current = grpc_v1.V2_SERVICE.methods_by_name.get(method.name)
    assert current is not None, f"{method.name} missing in hannah.v2"
    assert (current.client_streaming, current.server_streaming) == (method.client_streaming, method.server_streaming)
    _assert_fields_translatable(method.input_type, current.input_type, method.input_type.name, set())


def test_every_v1_method_gets_a_handler():
    handlers = grpc_v1.build_v1_method_handlers(pb_grpc.HannahServiceServicer())
    assert set(handlers) == {m.name for m in grpc_v1.V1_SERVICE.methods}


# ------------------------------------------------------------------
# command_to_v1: Core's v2 commands as a hannah.v1 adapter gets them

class TestCommandToV1:
    def test_set_resident_carries_the_callers_presence_state_and_action(self):
        cmd = pb.AgentCommand(set_resident=pb.AgentSetResident(resident_id="leonie", type=pb.ROOMIE, action=pb.HOME))

        v1 = grpc_v1.command_to_v1(cmd, presence_state=7)

        assert isinstance(v1, pb1.AgentCommand)
        assert (v1.set_resident.resident_id, v1.set_resident.presence_state) == ("leonie", 7)
        assert v1.set_resident.type == pb1.ROOMIE
        assert v1.set_resident.action == pb1.HOME

    @pytest.mark.parametrize("action,expected", [(pb.AWAY, 0), (pb.HOME, 1), (pb.ASLEEP, 2), (pb.AWAKE, 1)])
    def test_set_resident_derives_presence_state_from_action_when_not_given(self, action, expected):
        cmd = pb.AgentCommand(set_resident=pb.AgentSetResident(resident_id="leonie", action=action))

        assert grpc_v1.command_to_v1(cmd).set_resident.presence_state == expected

    def test_unchanged_commands_are_translated_by_name(self):
        cmd = pb.AgentCommand(set_state=pb.AgentSetState(state_id="a.b.on", value="true"))

        v1 = grpc_v1.command_to_v1(cmd)

        assert (v1.set_state.state_id, v1.set_state.value) == ("a.b.on", "true")

    def test_set_slot_has_no_counterpart_for_a_v1_adapter(self):
        cmd = pb.AgentCommand(set_slot=pb.SetSlot(device_id="d", slot_id="on", value=pb.SlotValue(boolean=True)))

        assert grpc_v1.command_to_v1(cmd) is None

    def test_empty_command_is_dropped(self):
        assert grpc_v1.command_to_v1(pb.AgentCommand()) is None


# ------------------------------------------------------------------
# End-to-end: one real GrpcServer, clients on both generations.

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _EchoServicer(pb_grpc.HannahServiceServicer):
    """Handlers only ever see and return v2 objects, regardless of the caller's generation."""

    def SubmitText(self, request, context):
        assert isinstance(request, pb.SubmitTextRequest)
        return pb.SubmitTextResponse(answer=f"echo:{request.text}", intent_name="Echo")

    def SubscribeEvents(self, request, context):
        assert isinstance(request, pb.EventFilter)
        for event_type in request.event_types:
            yield pb.HannahEvent(event_type=event_type)

    def AutomationConnect(self, request_iterator, context):
        for msg in request_iterator:
            assert isinstance(msg, pb.AutomationMessage)
            yield pb.AutomationCommand(snapshot=pb.AutomationSnapshot(user_ids=[len(msg.register.automation)]))


@pytest.fixture(scope="module")
def channel():
    port = _free_port()
    server = GrpcServer({"host": "127.0.0.1", "port": port}, _EchoServicer())
    server.start()
    ch = grpc.insecure_channel(f"127.0.0.1:{port}")
    yield ch
    ch.close()
    server.stop()


@pytest.fixture(params=["hannah.v1", "hannah.v2"])
def client(request, channel):
    if request.param == "hannah.v1":
        return pb1, pb1_grpc.HannahServiceStub(channel)
    return pb, pb_grpc.HannahServiceStub(channel)


def test_unary_call_works_for_both_generations(client):
    mod, stub = client
    response = stub.SubmitText(mod.SubmitTextRequest(text="hallo"), timeout=5)

    assert isinstance(response, mod.SubmitTextResponse)
    assert response.answer == "echo:hallo"
    assert response.intent_name == "Echo"


def test_server_streaming_works_for_both_generations(client):
    mod, stub = client
    events = list(stub.SubscribeEvents(mod.EventFilter(event_types=["car.parked", "resident.arrived"]), timeout=5))

    assert [e.event_type for e in events] == ["car.parked", "resident.arrived"]
    assert all(isinstance(e, mod.HannahEvent) for e in events)


def test_bidi_streaming_works_for_both_generations(client):
    mod, stub = client
    requests = [
        mod.AutomationMessage(register=mod.AutomationRegister(automation="x" * n)) for n in (1, 2, 3)
    ]
    commands = list(stub.AutomationConnect(iter(requests), timeout=5))

    assert [list(c.snapshot.user_ids) for c in commands] == [[1], [2], [3]]
    assert all(isinstance(c, mod.AutomationCommand) for c in commands)


def test_unversioned_package_is_unimplemented_and_logged(channel, caplog):
    call = channel.unary_unary(
        "/hannah.HannahService/SubmitText",
        request_serializer=lambda b: b,
        response_deserializer=lambda b: b,
    )
    with caplog.at_level(logging.WARNING, logger="hannah.grpc_interceptors"):
        with pytest.raises(grpc.RpcError) as exc:
            call(b"", timeout=5, metadata=(("x-proto-version", "9"),))

    assert exc.value.code() == grpc.StatusCode.UNIMPLEMENTED
    [record] = [r for r in caplog.records if "[grpc/version]" in r.getMessage()]
    assert "/hannah.HannahService/SubmitText" in record.getMessage()
    assert "'9'" in record.getMessage()


# ------------------------------------------------------------------
# GetDevices / ControlDevice: State-based and native for hannah.v1, typed for hannah.v2.

def _light(iobroker_suffix: str, canonical: str, value: str, state_type, writable: bool = True):
    base = "javascript.0.virtualDevice.Licht.EG.Wohnzimmer.Decke"
    return pb1.AgentDevice(
        state_id=f"{base}.{iobroker_suffix}", device_id=base, canonical_key=canonical,
        room="wohnzimmer", device="Decke", device_type="light",
        value=pb1.AgentStateValue(value=value, ack=True), room_names={"de": "Wohnzimmer"},
        state_type=state_type, writable=writable,
    )


@pytest.fixture
def device_server():
    iobroker = IoBrokerClient({"host": "localhost", "port": 8093})
    iobroker.handle_device_snapshot([
        _light("on", "on", "true", pb1.StateType.BOOLEAN),
        _light("level", "level", "60", pb1.StateType.NUMERIC),
    ])
    control = MagicMock(return_value=True)
    user_manager = MagicMock(get_user_by_linked_account=MagicMock(return_value=MagicMock(id=1, trust_level=8)))
    servicer = HannahServicer(
        user_manager=user_manager, satellite_manager=MagicMock(), handle_text=MagicMock(),
        handle_voice=MagicMock(), announce=MagicMock(), notificate=MagicMock(),
        get_satellites=MagicMock(), get_car_state=MagicMock(),
        get_devices=iobroker.get_devices_snapshot, control_device=control,
    )
    port = _free_port()
    server = GrpcServer({"host": "127.0.0.1", "port": port}, servicer)
    server.start()
    ch = grpc.insecure_channel(f"127.0.0.1:{port}")
    yield ch, control
    ch.close()
    server.stop()


class TestDevices:
    def test_v1_client_gets_the_state_based_list_unchanged(self, device_server):
        ch, _ = device_server
        response = pb1_grpc.HannahServiceStub(ch).GetDevices(Empty(), timeout=5)

        [room] = response.rooms
        [device] = room.devices
        assert (room.key, device.name, device.category) == ("wohnzimmer", "Decke", "light")
        assert sorted(device.states) == ["level", "on"]
        assert dict(device.current) == {"on": "True", "level": "60"}
        assert device.state_types["on"] == pb1.StateType.BOOLEAN
        assert dict(device.state_writable) == {"on": True, "level": True}

    def test_v2_client_gets_class_and_slots(self, device_server):
        ch, _ = device_server
        response = pb_grpc.HannahServiceStub(ch).GetDevices(Empty(), timeout=5)

        [room] = response.rooms
        [device] = room.devices
        assert device.device_class == pb.DEVICE_CLASS_LIGHT
        slots = {s.slot_id: s for s in device.slots}
        assert slots["on"].kind == pb.SLOT_KIND_ON and slots["on"].value.boolean is True
        assert slots["level"].kind == pb.SLOT_KIND_BRIGHTNESS and slots["level"].value.number == 60

    def test_v1_control_passes_state_and_value_through(self, device_server):
        ch, control = device_server
        response = pb1_grpc.HannahServiceStub(ch).ControlDevice(
            pb1.ControlDeviceRequest(device_id="d", state="level", value="40",
                                     source_service="telegram", source_user_id="42"),
            timeout=5, metadata=(("x-compat-version", "2"),),
        )

        assert response.ok is True
        control.assert_called_once_with("d", "level", "40", 8)

    def test_v1_path_is_gated_by_the_v1_compat_version(self, device_server):
        # ControlDeviceRequest is at compat_version 2 in hannah.v1: a client without the
        # header counts as 1 and is rejected, exactly like before hannah.v2.
        ch, control = device_server
        with pytest.raises(grpc.RpcError) as exc:
            pb1_grpc.HannahServiceStub(ch).ControlDevice(
                pb1.ControlDeviceRequest(device_id="d", state="on", value="true"), timeout=5,
            )

        assert exc.value.code() == grpc.StatusCode.FAILED_PRECONDITION
        control.assert_not_called()

    def test_v2_control_resolves_the_slot_to_the_state_key(self, device_server):
        ch, control = device_server
        response = pb_grpc.HannahServiceStub(ch).ControlDevice(
            pb.ControlDeviceRequest(device_id="d", slot_id="brightness", value=pb.SlotValue(number=40),
                                    source_service="telegram", source_user_id="42"),
            timeout=5,
        )

        assert response.ok is True
        control.assert_called_once_with("d", "level", "40", 8)


# ------------------------------------------------------------------
# AgentConnect: a hannah.v1 adapter stays on the State-based path, a hannah.v2 adapter
# speaks typed devices.

UNKNOWN_20 = bytes([0xA0, 0x01, 0x07])


@pytest.fixture
def agent_server():
    callbacks = {
        "on_agent_state": MagicMock(), "on_agent_state_initial": MagicMock(), "on_agent_device_snapshot": MagicMock(),
        "on_agent_resident": MagicMock(), "on_agent_room_snapshot": MagicMock(),
    }
    servicer = HannahServicer(
        user_manager=MagicMock(), satellite_manager=MagicMock(), handle_text=MagicMock(),
        handle_voice=MagicMock(), announce=MagicMock(), notificate=MagicMock(),
        get_satellites=MagicMock(), get_car_state=MagicMock(), **callbacks,
    )
    port = _free_port()
    server = GrpcServer({"host": "127.0.0.1", "port": port}, servicer)
    server.start()
    ch = grpc.insecure_channel(f"127.0.0.1:{port}")
    yield ch, servicer, callbacks
    ch.close()
    server.stop()


def _open_stream(stub, *messages):
    """A stream that stays open after `messages` until release() — so Core can push commands."""
    release = threading.Event()

    def requests():
        yield from messages
        release.wait(10)

    return stub.AgentConnect(requests(), timeout=15), release


def _wait_for(condition, what, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError(f"timeout waiting for {what}")


class TestAgentConnectV1:
    def test_snapshot_and_state_updates_take_the_state_based_path(self, agent_server):
        ch, _, callbacks = agent_server
        messages = [
            pb1.AgentMessage(send_snapshot=pb1.AgentDeviceSnapshot(devices=[_light("on", "on", "true", pb1.StateType.BOOLEAN)])),
            pb1.AgentMessage(state_update=pb1.AgentStateUpdate(
                state_id="javascript.0.virtualDevice.Licht.EG.Wohnzimmer.Decke.on", value="false", ack=True, ts=5, canonical_key="on",
            )),
        ]

        list(pb1_grpc.HannahServiceStub(ch).AgentConnect(iter(messages), timeout=5))

        [devices] = callbacks["on_agent_device_snapshot"].call_args.args
        assert isinstance(devices[0], pb1.AgentDevice) and devices[0].canonical_key == "on"
        callbacks["on_agent_state"].assert_called_once_with(
            "javascript.0.virtualDevice.Licht.EG.Wohnzimmer.Decke.on", "false", True, 5, "on",
        )

    def test_other_payloads_are_translated_to_v2(self, agent_server):
        ch, _, callbacks = agent_server
        messages = [
            pb1.AgentMessage(resident_update=pb1.AgentResident(roomie_id="leonie", type=pb1.ROOMIE, presence_state=1)),
            pb1.AgentMessage(send_rooms=pb1.AgentRoomSnapshot(rooms=[pb1.AgentRoom(room_id="bad", display_names={"de": "Bad"})])),
        ]

        list(pb1_grpc.HannahServiceStub(ch).AgentConnect(iter(messages), timeout=5))

        callbacks["on_agent_resident"].assert_called_once_with("leonie", None, 1, pb.ROOMIE, None)
        [rooms] = callbacks["on_agent_room_snapshot"].call_args.args
        assert isinstance(rooms[0], pb.AgentRoom) and dict(rooms[0].display_names) == {"de": "Bad"}

    def test_ack_reports_unknown_fields_against_the_v1_schema(self, agent_server):
        ch, _, _ = agent_server
        msg = pb1.AgentMessage(ack_id=7)
        msg.send_snapshot.devices.add().MergeFromString(pb1.AgentDevice(state_id="a").SerializeToString() + UNKNOWN_20)

        [command] = list(pb1_grpc.HannahServiceStub(ch).AgentConnect(iter([msg]), timeout=5))

        assert isinstance(command, pb1.AgentCommand)
        assert command.ack.ack_id == 7
        assert [(u.message_type, list(u.field_numbers)) for u in command.ack.unknown_fields] == [("hannah.v1.AgentDevice", [20])]

    def test_core_commands_reach_the_adapter_as_v1(self, agent_server):
        ch, servicer, _ = agent_server
        call, release = _open_stream(pb1_grpc.HannahServiceStub(ch))
        try:
            _wait_for(servicer.agent_connected, "the adapter to connect")

            servicer.agent_set_resident("leonie", 2, pb.ROOMIE, pb.ASLEEP)
            servicer.agent_set_state("a.b.on", "true")
            resident_cmd, state_cmd = next(call), next(call)
        finally:
            release.set()

        assert isinstance(resident_cmd, pb1.AgentCommand)
        assert (resident_cmd.set_resident.resident_id, resident_cmd.set_resident.presence_state) == ("leonie", 2)
        assert resident_cmd.set_resident.action == pb1.ASLEEP
        assert (state_cmd.set_state.state_id, state_cmd.set_state.value) == ("a.b.on", "true")


class TestAgentConnectV2:
    def test_typed_devices_reach_their_callbacks(self, agent_server):
        ch, servicer, _ = agent_server
        servicer._on_agent_typed_snapshot = MagicMock()
        servicer._on_agent_slot_update = MagicMock()
        servicer._on_agent_device_availability = MagicMock()
        messages = [
            pb.AgentMessage(typed_snapshot=pb.TypedDeviceSnapshot(devices=[pb.TypedDevice(device_id="d", name="Decke", room="bad")])),
            pb.AgentMessage(slot_update=pb.SlotUpdate(device_id="d", slot_id="on", value=pb.SlotValue(boolean=True), ack=True)),
            pb.AgentMessage(device_availability=pb.DeviceAvailability(device_id="d", available=False)),
        ]

        list(pb_grpc.HannahServiceStub(ch).AgentConnect(iter(messages), timeout=5))

        [devices] = servicer._on_agent_typed_snapshot.call_args.args
        assert [d.device_id for d in devices] == ["d"]
        assert servicer._on_agent_slot_update.call_args.args[0].slot_id == "on"
        assert servicer._on_agent_device_availability.call_args.args[0].available is False

    def test_a_v2_adapter_fills_the_device_registry(self, agent_server):
        from hannah.typed_devices import DeviceRegistry
        ch, servicer, _ = agent_server
        registry = DeviceRegistry()
        servicer._on_agent_typed_snapshot = registry.handle_typed_snapshot
        servicer._on_agent_slot_update = registry.handle_slot_update
        servicer._on_agent_device_availability = registry.handle_device_availability
        messages = [
            pb.AgentMessage(typed_snapshot=pb.TypedDeviceSnapshot(devices=[pb.TypedDevice(
                device_id="d", name="Decke", room="bad", device_class=pb.DEVICE_CLASS_LIGHT, available=True,
                slots=[pb.Slot(slot_id="on", kind=pb.SLOT_KIND_ON, writable=True)],
            )])),
            pb.AgentMessage(slot_update=pb.SlotUpdate(device_id="d", slot_id="on", value=pb.SlotValue(boolean=True), ack=True)),
            pb.AgentMessage(device_availability=pb.DeviceAvailability(device_id="d", available=False)),
        ]

        list(pb_grpc.HannahServiceStub(ch).AgentConnect(iter(messages), timeout=5))

        device = registry.get("d")
        assert device.device_class == pb.DEVICE_CLASS_LIGHT
        assert device.slots["on"].value is True
        assert device.available is False

    def test_typed_devices_without_a_consumer_are_acked_and_logged_once(self, agent_server, caplog):
        ch, _, _ = agent_server
        messages = [
            pb.AgentMessage(ack_id=1, typed_snapshot=pb.TypedDeviceSnapshot(devices=[pb.TypedDevice(device_id="d")])),
            pb.AgentMessage(ack_id=2, slot_update=pb.SlotUpdate(device_id="d", slot_id="on")),
        ]

        with caplog.at_level(logging.WARNING, logger="hannah.grpc_server"):
            commands = list(pb_grpc.HannahServiceStub(ch).AgentConnect(iter(messages), timeout=5))

        assert [c.ack.ack_id for c in commands] == [1, 2]
        assert len([r for r in caplog.records if "typisierte Geräte" in r.getMessage()]) == 1

    def test_state_updates_carry_no_canonical_key(self, agent_server):
        ch, _, callbacks = agent_server
        msg = pb.AgentMessage(state_update=pb.AgentStateUpdate(state_id="0_userdata.0.feeded", value="true", ack=True, ts=3))

        list(pb_grpc.HannahServiceStub(ch).AgentConnect(iter([msg]), timeout=5))

        callbacks["on_agent_state"].assert_called_once_with("0_userdata.0.feeded", "true", True, 3, "")
        callbacks["on_agent_state_initial"].assert_not_called()

    def test_initial_state_updates_only_seed_and_are_no_changes(self, agent_server):
        ch, _, callbacks = agent_server
        messages = [
            pb.AgentMessage(state_update=pb.AgentStateUpdate(state_id="0_userdata.0.feeded", value="true", ack=True, ts=3, initial=True)),
            pb.AgentMessage(state_update=pb.AgentStateUpdate(state_id="0_userdata.0.feeded", value="false", ack=True, ts=4)),
        ]

        list(pb_grpc.HannahServiceStub(ch).AgentConnect(iter(messages), timeout=5))

        callbacks["on_agent_state_initial"].assert_called_once_with("0_userdata.0.feeded", "true")
        callbacks["on_agent_state"].assert_called_once_with("0_userdata.0.feeded", "false", True, 4, "")

    def test_initial_state_update_is_acked(self, agent_server):
        ch, _, _ = agent_server
        msg = pb.AgentMessage(ack_id=7, state_update=pb.AgentStateUpdate(state_id="s", value="1", initial=True))

        commands = list(pb_grpc.HannahServiceStub(ch).AgentConnect(iter([msg]), timeout=5))

        assert [c.ack.ack_id for c in commands] == [7]

    def test_set_resident_reaches_a_v2_adapter_with_the_action_only(self, agent_server):
        ch, servicer, _ = agent_server
        call, release = _open_stream(pb_grpc.HannahServiceStub(ch))
        try:
            _wait_for(servicer.agent_connected, "the adapter to connect")
            servicer.agent_set_resident("leonie", 2, pb.ROOMIE, pb.ASLEEP)
            command = next(call)
        finally:
            release.set()

        assert isinstance(command, pb.AgentCommand)
        assert command.set_resident.action == pb.ASLEEP
        assert not hasattr(command.set_resident, "presence_state")


# ------------------------------------------------------------------
# Config

def test_enforce_protocol_version_key_is_ignored_with_deprecation_log(caplog):
    with caplog.at_level(logging.WARNING, logger="hannah.grpc_server"):
        GrpcServer({"enforce_protocol_version": True}, MagicMock())

    assert any("enforce_protocol_version" in r.getMessage() for r in caplog.records)


def test_no_deprecation_log_without_the_key(caplog):
    with caplog.at_level(logging.WARNING, logger="hannah.grpc_server"):
        GrpcServer({}, MagicMock())

    assert not any("enforce_protocol_version" in r.getMessage() for r in caplog.records)


def test_compat_version_is_enforced_by_default():
    assert GrpcServer({}, MagicMock())._compat_interceptor.enforce


def test_compat_version_enforcement_can_be_disabled_in_config():
    assert not GrpcServer({"enforce_compat_version": False}, MagicMock())._compat_interceptor.enforce


def test_compat_version_enforcement_covers_both_generations():
    server = GrpcServer({}, MagicMock())
    server.set_compat_version_enforcement(True)

    required = server._compat_interceptor._required
    assert {f"/{grpc_v1.V2_SERVICE.full_name}/{m.name}" for m in grpc_v1.V2_SERVICE.methods} <= required.keys()
    assert {f"/{grpc_v1.V1_SERVICE.full_name}/{m.name}" for m in grpc_v1.V1_SERVICE.methods} <= required.keys()
    assert server._compat_interceptor.enforce
