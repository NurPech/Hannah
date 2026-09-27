"""#360: the client works with hannah.v1 types and falls back to the unversioned N−1
path when Core is too old for hannah.v1. Runs against real in-process gRPC servers.
The fallback itself lives in hannah_grpc.client (#361); these tests cover HannahClient
end to end on top of it."""
import logging

import grpc.aio
import pytest
from hannah_proto import hannah_pb2 as legacy_pb2
from hannah_proto import hannah_pb2_grpc as legacy_pb2_grpc
from hannah_proto.v1 import hannah_pb2
from hannah_proto.v1 import hannah_pb2_grpc

from hannah_telegram.grpc_client import HannahClient

FALLBACK_LOGGER = "hannah_grpc.client"


def _make_servicer(pb, pb_grpc, tag: str, hits: list, submit_text: bool = True):
    """A fake Core for one package: records `tag` for every call it serves."""

    class Servicer(pb_grpc.HannahServiceServicer):
        async def GetSatellites(self, request, context):
            hits.append((tag, "GetSatellites"))
            return pb.GetSatellitesResponse()

        async def SubscribeEvents(self, request, context):
            hits.append((tag, "SubscribeEvents"))
            for event_type in request.event_types:
                yield pb.HannahEvent(event_type=event_type)

        async def ChannelConnect(self, request_iterator, context):
            hits.append((tag, "ChannelConnect"))
            async for msg in request_iterator:
                assert msg.register.service == "telegram"
                yield pb.ChannelCommand(registered=pb.ChannelRegistered())
                return

    if submit_text:
        async def SubmitText(self, request, context):
            hits.append((tag, "SubmitText"))
            return pb.SubmitTextResponse(answer=f"{tag}:{request.text}")
        Servicer.SubmitText = SubmitText
    return Servicer()


async def _start_core(v1: bool, legacy: bool, hits: list, v1_submit_text: bool = True):
    server = grpc.aio.server()
    if v1:
        hannah_pb2_grpc.add_HannahServiceServicer_to_server(
            _make_servicer(hannah_pb2, hannah_pb2_grpc, "v1", hits, submit_text=v1_submit_text), server)
    if legacy:
        legacy_pb2_grpc.add_HannahServiceServicer_to_server(
            _make_servicer(legacy_pb2, legacy_pb2_grpc, "legacy", hits), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    return server, port


def _fallback_warnings(caplog):
    return [r for r in caplog.records if "falling back" in r.getMessage()]


@pytest.mark.parametrize("v1, legacy, expected", [
    (True, False, "v1"),       # current Core that dropped N−1 (hannah.v2 era)
    (False, True, "legacy"),   # Core too old for hannah.v1
    (True, True, "v1"),        # today's Core: serves both, v1 wins
])
async def test_calls_go_to_the_right_path(caplog, v1, legacy, expected):
    hits = []
    server, port = await _start_core(v1, legacy, hits)
    client = HannahClient("127.0.0.1", port)
    await client.connect()
    try:
        with caplog.at_level(logging.WARNING, logger=FALLBACK_LOGGER):
            answer = await client.submit_text("hallo", "42")
            assert answer == f"{expected}:hallo"

            # Server streaming: v1 types regardless of the path
            stub = await client._get_stub()
            stream = stub.SubscribeEvents(hannah_pb2.EventFilter(event_types=["a", "b"]),
                                          metadata=client._stream_metadata("SubscribeEvents"))
            events = [e async for e in stream]
            assert [e.event_type for e in events] == ["a", "b"]
            assert all(isinstance(e, hannah_pb2.HannahEvent) for e in events)

            # Bidi streaming
            call = stub.ChannelConnect(metadata=client._stream_metadata("ChannelConnect"))
            await call.write(hannah_pb2.ChannelMessage(register=hannah_pb2.ChannelRegister(service="telegram")))
            cmd = await call.read()
            assert cmd.WhichOneof("command") == "registered"
            await call.done_writing()

        assert {tag for tag, _ in hits} == {expected}
        assert len(_fallback_warnings(caplog)) == (1 if expected == "legacy" else 0)
    finally:
        await client.close()
        await server.stop(None)


async def test_probe_runs_once_per_connection(caplog):
    hits = []
    server, port = await _start_core(False, True, hits)
    client = HannahClient("127.0.0.1", port)
    await client.connect()
    try:
        with caplog.at_level(logging.WARNING, logger=FALLBACK_LOGGER):
            for _ in range(3):
                assert await client.submit_text("x", "1") == "legacy:x"
        assert len(_fallback_warnings(caplog)) == 1
        assert hits.count(("legacy", "SubmitText")) == 3

        # A reconnect (reset) probes again and warns again
        client._stubs.reset()
        with caplog.at_level(logging.WARNING, logger=FALLBACK_LOGGER):
            await client.submit_text("x", "1")
        assert len(_fallback_warnings(caplog)) == 2
    finally:
        await client.close()
        await server.stop(None)


async def test_single_unknown_method_does_not_switch_to_legacy(caplog):
    # Core serves v1, but not this one method (e.g. it's newer than Core): the call fails,
    # the connection stays on v1, and nothing claims Core is outdated.
    hits = []
    server, port = await _start_core(True, True, hits, v1_submit_text=False)
    client = HannahClient("127.0.0.1", port)
    await client.connect()
    try:
        with caplog.at_level(logging.WARNING, logger=FALLBACK_LOGGER):
            answer = await client.submit_text("x", "1")
        assert answer.startswith("Hannah antwortet gerade nicht")
        assert not client._stubs.legacy
        assert ("legacy", "SubmitText") not in hits
        assert not _fallback_warnings(caplog)
    finally:
        await client.close()
        await server.stop(None)
