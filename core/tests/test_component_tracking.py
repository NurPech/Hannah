"""Component tracking through the OutdatedComponentInterceptor (#398), against a real
in-process gRPC server: calls, streams, the Heartbeat RPC."""
import logging
import threading
import time
from concurrent import futures
from unittest.mock import MagicMock

import grpc
import pytest
from hannah_proto.v2 import hannah_pb2 as pb
from hannah_proto.v2 import hannah_pb2_grpc as pb_grpc

from hannah import grpc_v1
from hannah.component_registry import ComponentRegistry, ComponentTracker, KIND_COMPONENT
from hannah.grpc_interceptors import OutdatedComponentInterceptor
from hannah.grpc_server import DEFAULT_MAX_WORKERS, KEEPALIVE_OPTIONS, GrpcServer, HannahServicer


class _Core(pb_grpc.HannahServiceServicer):
    """Heartbeat as Core serves it, a server stream that stays open until released."""

    def __init__(self):
        self.release = threading.Event()
        self.stream_running = threading.Event()

    def Heartbeat(self, request, context):
        return HannahServicer.Heartbeat(None, request, context)

    def SubscribeEvents(self, request, context):
        self.stream_running.set()
        while not self.release.wait(0.05) and context.is_active():
            pass
        yield pb.HannahEvent(event_type="done")


@pytest.fixture
def setup():
    registry = ComponentRegistry()
    tracker = ComponentTracker(registry)
    interceptor = OutdatedComponentInterceptor(
        MagicMock(), legacy_prefix=grpc_v1.V1_PREFIX, tracker=tracker, stream_limit=10)
    core = _Core()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=16), interceptors=[interceptor])
    pb_grpc.add_HannahServiceServicer_to_server(core, server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    yield registry, tracker, interceptor, core, pb_grpc.HannahServiceStub(channel)
    core.release.set()
    channel.close()
    server.stop(None)


def _identity(component="proxy", version="1.2.3", instance_id="abc123"):
    return (("x-component", component), ("x-component-version", version), ("x-component-id", instance_id))


def _consume(call):
    try:
        list(call)
    except grpc.RpcError:
        pass  # cancelled by the test or the channel closing


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_a_heartbeat_enters_the_component_into_the_registry(setup):
    registry, tracker, _interceptor, _core, stub = setup

    stub.Heartbeat(pb.HeartbeatRequest(), metadata=_identity("timer", "0.4.0", "t1"))

    entry = registry.get(KIND_COMPONENT, "timer/t1")
    assert (entry.component, entry.version, entry.legacy) == ("timer", "0.4.0", False)


def test_a_call_without_identity_is_not_tracked(setup):
    registry, _tracker, _interceptor, _core, stub = setup

    stub.Heartbeat(pb.HeartbeatRequest())

    assert [n for k, n, _ in registry.items() if k == KIND_COMPONENT] == []


def test_a_stream_is_counted_while_it_runs_and_not_afterwards(setup):
    _registry, tracker, interceptor, core, stub = setup

    call = stub.SubscribeEvents(pb.EventFilter(), metadata=_identity("telegram", "1.0.0", "t1"))
    consumer = threading.Thread(target=_consume, args=(call,), daemon=True)
    consumer.start()
    assert core.stream_running.wait(5)

    assert tracker.snapshot()[0]["open_streams"] == 1
    assert interceptor.open_streams == 1

    core.release.set()
    consumer.join(5)

    assert _wait_for(lambda: interceptor.open_streams == 0)
    assert tracker.snapshot()[0]["open_streams"] == 0


def test_a_cancelled_stream_is_counted_down_too(setup):
    _registry, tracker, interceptor, core, stub = setup
    call = stub.SubscribeEvents(pb.EventFilter(), metadata=_identity("telegram", "1.0.0", "t1"))
    consumer = threading.Thread(target=_consume, args=(call,), daemon=True)
    consumer.start()
    assert core.stream_running.wait(5)

    call.cancel()

    assert _wait_for(lambda: interceptor.open_streams == 0)
    assert tracker.snapshot()[0]["open_streams"] == 0


def test_streams_without_identity_count_towards_the_pool_but_not_for_a_component(setup):
    registry, _tracker, interceptor, core, stub = setup
    call = stub.SubscribeEvents(pb.EventFilter())
    consumer = threading.Thread(target=_consume, args=(call,), daemon=True)
    consumer.start()
    assert core.stream_running.wait(5)

    assert interceptor.open_streams == 1
    assert [n for k, n, _ in registry.items() if k == KIND_COMPONENT] == []


def test_the_legacy_path_is_flagged():
    registry = ComponentRegistry()
    tracker = ComponentTracker(registry)
    interceptor = OutdatedComponentInterceptor(None, legacy_prefix=grpc_v1.V1_PREFIX, tracker=tracker)
    handler = grpc.unary_unary_rpc_method_handler(lambda request, context: "ok")
    details = MagicMock(method=f"{grpc_v1.V1_PREFIX}SubmitText", invocation_metadata=_identity("proxy", "0.9.0", "p1"))

    assert interceptor.intercept_service(MagicMock(return_value=handler), details) is handler

    assert registry.get(KIND_COMPONENT, "proxy/p1").legacy is True


def test_a_unary_handler_is_passed_on_unchanged():
    interceptor = OutdatedComponentInterceptor(None, legacy_prefix="/x/", tracker=ComponentTracker(ComponentRegistry()))
    handler = grpc.unary_unary_rpc_method_handler(lambda request, context: "ok")

    result = interceptor.intercept_service(
        MagicMock(return_value=handler),
        MagicMock(method="/hannah.v2.HannahService/Heartbeat", invocation_metadata=_identity()),
    )

    assert result is handler


def test_a_warning_comes_at_eighty_percent_of_the_pool_once_until_it_has_calmed_down(caplog):
    interceptor = OutdatedComponentInterceptor(None, stream_limit=10)

    with caplog.at_level(logging.WARNING, logger="hannah.grpc_interceptors"):
        for _ in range(7):
            interceptor._stream_opened(None)
        assert caplog.records == []

        for _ in range(3):
            interceptor._stream_opened(None)  # the 8th crosses 80 %, 9th and 10th stay quiet
        assert len([r for r in caplog.records if "Streams offen" in r.getMessage()]) == 1

        for _ in range(5):
            interceptor._stream_closed(None)  # down to 5, below 60 %
        for _ in range(3):
            interceptor._stream_opened(None)  # back to 8

    assert len([r for r in caplog.records if "Streams offen" in r.getMessage()]) == 2


def test_the_heartbeat_handler_answers_with_an_empty_response():
    assert HannahServicer.Heartbeat(None, pb.HeartbeatRequest(), MagicMock()) == pb.HeartbeatResponse()


class TestServerSettings:
    def test_the_pool_is_larger_than_before_and_configurable(self):
        servicer = MagicMock()

        assert GrpcServer({}, servicer)._max_workers == DEFAULT_MAX_WORKERS >= 64
        assert GrpcServer({"max_workers": 96}, servicer)._max_workers == 96

    def test_keepalive_finds_dead_peers_well_inside_the_presence_window(self):
        options = dict(KEEPALIVE_OPTIONS)

        assert options["grpc.keepalive_time_ms"] + options["grpc.keepalive_timeout_ms"] < 90_000
        assert options["grpc.keepalive_permit_without_calls"] == 1

    def test_the_interceptor_knows_the_pool_size_and_the_tracker_of_the_servicer(self):
        servicer = MagicMock()

        server = GrpcServer({"max_workers": 40}, servicer)

        assert server._outdated_interceptor._stream_limit == 40
        assert server._outdated_interceptor._tracker is servicer.components
