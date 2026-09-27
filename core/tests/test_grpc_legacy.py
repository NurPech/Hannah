"""
#359: Core runs internally on hannah.v1 and serves the frozen, unversioned
hannah package (N−1) with the same handlers through hannah.grpc_legacy.
"""
import logging
import socket
from unittest.mock import MagicMock

import grpc
import pytest
from google.protobuf.descriptor import Descriptor, FieldDescriptor
from hannah_proto import hannah_pb2 as legacy_pb
from hannah_proto import hannah_pb2_grpc as legacy_pb_grpc
from hannah_proto.v1 import hannah_pb2 as pb
from hannah_proto.v1 import hannah_pb2_grpc as pb_grpc

from hannah.grpc_legacy import CURRENT_SERVICE, LEGACY_SERVICE, build_legacy_method_handlers
from hannah.grpc_server import GrpcServer


# ------------------------------------------------------------------
# Wire compatibility — the generic byte-level bridge only holds while every
# message a legacy method uses is a wire-compatible subset of its v1 counterpart:
# every legacy field exists in v1 with the same number, type and cardinality.
# v1 may carry additional fields (additive, e.g. #366's required_trust_level /
# ControlDeviceRequest.source_*): legacy bytes parse into v1 with those unset,
# and legacy clients skip them as unknown. If this fails, the named method needs
# a real translator in grpc_legacy.py.

def _assert_wire_subset(legacy: Descriptor, current: Descriptor, path: str, seen: set) -> None:
    if (legacy.full_name, current.full_name) in seen:
        return
    seen = seen | {(legacy.full_name, current.full_name)}
    for field in legacy.fields:
        other = current.fields_by_number.get(field.number)
        where = f"{path}.{field.name} (#{field.number})"
        assert other is not None, f"{where} missing in hannah.v1"
        assert (other.type, other.is_repeated) == (field.type, field.is_repeated), f"{where} type/cardinality differ"
        if field.type in (FieldDescriptor.TYPE_MESSAGE, FieldDescriptor.TYPE_GROUP):
            _assert_wire_subset(field.message_type, other.message_type, where, seen)
        elif field.type == FieldDescriptor.TYPE_ENUM:
            missing = {v.number for v in field.enum_type.values} - {v.number for v in other.enum_type.values}
            assert not missing, f"{where} enum values {sorted(missing)} missing in hannah.v1"


@pytest.mark.parametrize("method", list(LEGACY_SERVICE.methods), ids=lambda m: m.name)
def test_legacy_method_is_wire_compatible_with_v1(method):
    current = CURRENT_SERVICE.methods_by_name.get(method.name)
    assert current is not None, f"{method.name} missing in hannah.v1"
    assert (current.client_streaming, current.server_streaming) == (method.client_streaming, method.server_streaming)
    _assert_wire_subset(method.input_type, current.input_type, method.input_type.name, set())
    _assert_wire_subset(method.output_type, current.output_type, method.output_type.name, set())


def test_every_legacy_method_gets_a_handler():
    handlers = build_legacy_method_handlers(pb_grpc.HannahServiceServicer())
    assert set(handlers) == {m.name for m in LEGACY_SERVICE.methods}


# ------------------------------------------------------------------
# End-to-end: one real GrpcServer, clients on both packages.

class _EchoServicer(pb_grpc.HannahServiceServicer):
    """Handlers only ever see and return v1 objects, regardless of the caller's package."""

    def SubmitText(self, request, context):
        assert isinstance(request, pb.SubmitTextRequest)
        return pb.SubmitTextResponse(answer=f"echo:{request.text}", intent_name="Echo")

    def SubscribeEvents(self, request, context):
        assert isinstance(request, pb.EventFilter)
        for event_type in request.event_types:
            yield pb.HannahEvent(event_type=event_type)

    def AgentConnect(self, request_iterator, context):
        for msg in request_iterator:
            assert isinstance(msg, pb.AgentMessage)
            yield pb.AgentCommand(set_state=pb.AgentSetState(
                state_id=msg.state_update.state_id, value=msg.state_update.value,
            ))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def channel():
    port = _free_port()
    server = GrpcServer({"host": "127.0.0.1", "port": port}, _EchoServicer())
    server.start()
    ch = grpc.insecure_channel(f"127.0.0.1:{port}")
    yield ch
    ch.close()
    server.stop()


@pytest.fixture(params=["hannah", "hannah.v1"])
def client(request, channel):
    if request.param == "hannah":
        return legacy_pb, legacy_pb_grpc.HannahServiceStub(channel)
    return pb, pb_grpc.HannahServiceStub(channel)


def test_unary_call_works_for_both_packages(client):
    mod, stub = client
    response = stub.SubmitText(mod.SubmitTextRequest(text="hallo"), timeout=5)

    assert isinstance(response, mod.SubmitTextResponse)
    assert response.answer == "echo:hallo"
    assert response.intent_name == "Echo"


def test_server_streaming_works_for_both_packages(client):
    mod, stub = client
    events = list(stub.SubscribeEvents(mod.EventFilter(event_types=["car.parked", "resident.arrived"]), timeout=5))

    assert [e.event_type for e in events] == ["car.parked", "resident.arrived"]
    assert all(isinstance(e, mod.HannahEvent) for e in events)


def test_bidi_streaming_works_for_both_packages(client):
    mod, stub = client
    requests = [
        mod.AgentMessage(state_update=mod.AgentStateUpdate(state_id=f"light.{i}", value=str(i)))
        for i in range(3)
    ]
    commands = list(stub.AgentConnect(iter(requests), timeout=5))

    assert [(c.set_state.state_id, c.set_state.value) for c in commands] == [
        ("light.0", "0"), ("light.1", "1"), ("light.2", "2"),
    ]


def test_path_older_than_n_minus_1_is_unimplemented(channel):
    call = channel.unary_unary(
        "/hannah.v0.HannahService/SubmitText",
        request_serializer=lambda b: b,
        response_deserializer=lambda b: b,
    )
    with pytest.raises(grpc.RpcError) as exc:
        call(b"", timeout=5, metadata=(("x-proto-version", "1"),))
    assert exc.value.code() == grpc.StatusCode.UNIMPLEMENTED


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
    server = GrpcServer({}, MagicMock())

    assert server._compat_interceptor.enforce


def test_compat_version_enforcement_can_be_disabled_in_config():
    server = GrpcServer({"enforce_compat_version": False}, MagicMock())

    assert not server._compat_interceptor.enforce


def test_compat_version_enforcement_covers_both_packages():
    server = GrpcServer({}, MagicMock())
    server.set_compat_version_enforcement(True)

    required = server._compat_interceptor._required
    assert {f"/{CURRENT_SERVICE.full_name}/{m.name}" for m in CURRENT_SERVICE.methods} <= required.keys()
    assert {f"/{LEGACY_SERVICE.full_name}/{m.name}" for m in LEGACY_SERVICE.methods} <= required.keys()
    assert server._compat_interceptor.enforce
