"""The client works with hannah.v2 types and, against a Core too old for hannah.v2, talks
hannah.v1 through the translating stub of hannah-grpc-lib. Runs against real in-process
gRPC servers. The fallback itself lives in hannah_grpc.client; these tests cover
HannahClient end to end on top of it."""
import asyncio
import logging

import grpc.aio
import pytest
from hannah_proto.v1 import hannah_pb2 as v1_pb2
from hannah_proto.v1 import hannah_pb2_grpc as v1_pb2_grpc
from hannah_proto.v2 import hannah_pb2
from hannah_proto.v2 import hannah_pb2_grpc

from hannah_telegram.grpc_client import HannahClient

FALLBACK_LOGGER = "hannah_grpc.client"


def _make_servicer(pb, pb_grpc, tag: str, hits: list, submit_text: bool = True):
    """A fake Core for one generation: records `tag` for every call it serves."""

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

        async def GetDevices(self, request, context):
            hits.append((tag, "GetDevices"))
            if tag == "v2":
                lamp = pb.DeviceInfo(id="x.lamp", name="Lampe", device_class=pb.DEVICE_CLASS_LIGHT, available=True)
                lamp.slots.add(slot_id="on", kind=pb.SLOT_KIND_ON, writable=True).value.boolean = True
                lamp.slots.add(slot_id="brightness", kind=pb.SLOT_KIND_BRIGHTNESS, writable=True).value.number = 60
            else:
                lamp = pb.DeviceInfo(id="x.lamp", name="Lampe", category="light", states=["on", "level"])
                lamp.current["on"] = "True"
                lamp.current["level"] = "60"
                lamp.state_types["on"] = pb.StateType.BOOLEAN
                lamp.state_types["level"] = pb.StateType.NUMERIC
                lamp.state_writable["on"] = True
                lamp.state_writable["level"] = True
            return pb.GetDevicesResponse(rooms=[pb.RoomInfo(key="wz", name="Wohnzimmer", devices=[lamp])])

        async def ControlDevice(self, request, context):
            if tag == "v2":
                hits.append((tag, "ControlDevice", request.device_id, request.slot_id, request.value.number))
            else:
                hits.append((tag, "ControlDevice", request.device_id, request.state, request.value))
            return pb.StatusResponse(ok=True, message="OK")

    if submit_text:
        async def SubmitText(self, request, context):
            hits.append((tag, "SubmitText"))
            return pb.SubmitTextResponse(answer=f"{tag}:{request.text}")
        Servicer.SubmitText = SubmitText
    return Servicer()


async def _start_core(v2: bool, v1: bool, hits: list, v2_submit_text: bool = True):
    server = grpc.aio.server()
    if v2:
        hannah_pb2_grpc.add_HannahServiceServicer_to_server(
            _make_servicer(hannah_pb2, hannah_pb2_grpc, "v2", hits, submit_text=v2_submit_text), server)
    if v1:
        v1_pb2_grpc.add_HannahServiceServicer_to_server(
            _make_servicer(v1_pb2, v1_pb2_grpc, "v1", hits), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    return server, port


def _fallback_warnings(caplog):
    return [r for r in caplog.records if "falling back" in r.getMessage()]


@pytest.mark.parametrize("v2, v1, expected", [
    (True, False, "v2"),       # current Core
    (False, True, "v1"),       # Core too old for hannah.v2
    (True, True, "v2"),        # today's Core: serves both, v2 wins
])
async def test_calls_go_to_the_right_path(caplog, v2, v1, expected):
    hits = []
    server, port = await _start_core(v2, v1, hits)
    client = HannahClient("127.0.0.1", port)
    await client.connect()
    try:
        with caplog.at_level(logging.WARNING, logger=FALLBACK_LOGGER):
            answer = await client.submit_text("hallo", "42")
            assert answer == f"{expected}:hallo"

            # Server streaming: hannah.v2 types regardless of the path
            stub = await client._get_stub()
            stream = stub.SubscribeEvents(hannah_pb2.EventFilter(event_types=["a", "b"]),
                                          metadata=client._stream_metadata("SubscribeEvents"))
            events = [e async for e in stream]
            assert [e.event_type for e in events] == ["a", "b"]
            assert all(isinstance(e, hannah_pb2.HannahEvent) for e in events)

            # Bidi streaming: the native stub of the active generation, not translated
            task = asyncio.create_task(client.channel_connect(hannah_pb2.ChannelRegister(service="telegram")))
            for _ in range(100):
                if any(h[1] == "ChannelConnect" for h in hits):
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert {tag for tag, *_ in hits} == {expected}
        assert len(_fallback_warnings(caplog)) == (1 if expected == "v1" else 0)
    finally:
        await client.close()
        await server.stop(None)


@pytest.mark.parametrize("v2, v1, expected", [(True, False, "v2"), (False, True, "v1")])
async def test_devices_come_as_typed_slots_on_both_paths(v2, v1, expected):
    hits = []
    server, port = await _start_core(v2, v1, hits)
    client = HannahClient("127.0.0.1", port)
    await client.connect()
    try:
        [room] = (await client.get_devices()).rooms
        [lamp] = room.devices

        assert room.name == "Wohnzimmer"
        assert lamp.device_class == hannah_pb2.DEVICE_CLASS_LIGHT
        slots = {s.slot_id: s for s in lamp.slots}
        assert slots["on"].kind == hannah_pb2.SLOT_KIND_ON and slots["on"].value.boolean is True
        assert slots["on"].writable
        assert slots["brightness" if expected == "v2" else "level"].value.number == 60
    finally:
        await client.close()
        await server.stop(None)


async def test_control_device_sets_a_slot_on_hannah_v2():
    hits = []
    server, port = await _start_core(True, False, hits)
    client = HannahClient("127.0.0.1", port)
    await client.connect()
    try:
        ok, msg = await client.control_device("x.lamp", "brightness", hannah_pb2.SlotValue(number=40), "42")

        assert (ok, msg) == (True, "OK")
        assert ("v2", "ControlDevice", "x.lamp", "brightness", 40.0) in hits
    finally:
        await client.close()
        await server.stop(None)


async def test_control_device_is_translated_to_a_state_on_a_hannah_v1_core():
    hits = []
    server, port = await _start_core(False, True, hits)
    client = HannahClient("127.0.0.1", port)
    await client.connect()
    try:
        # the slot ID of a device that came from hannah.v1 is its v1 state key
        ok, _ = await client.control_device("x.lamp", "level", hannah_pb2.SlotValue(number=40), "42")

        assert ok
        assert ("v1", "ControlDevice", "x.lamp", "level", "40") in hits
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
                assert await client.submit_text("x", "1") == "v1:x"
        assert len(_fallback_warnings(caplog)) == 1
        assert hits.count(("v1", "SubmitText")) == 3

        # A reconnect (reset) probes again and warns again
        client._stubs.reset()
        with caplog.at_level(logging.WARNING, logger=FALLBACK_LOGGER):
            await client.submit_text("x", "1")
        assert len(_fallback_warnings(caplog)) == 2
    finally:
        await client.close()
        await server.stop(None)


async def test_single_unknown_method_does_not_switch_to_the_previous_generation(caplog):
    # Core serves v2, but not this one method (e.g. it's newer than Core): the call fails,
    # the connection stays on v2, and nothing claims Core is outdated.
    hits = []
    server, port = await _start_core(True, True, hits, v2_submit_text=False)
    client = HannahClient("127.0.0.1", port)
    await client.connect()
    try:
        with caplog.at_level(logging.WARNING, logger=FALLBACK_LOGGER):
            answer = await client.submit_text("x", "1")
        assert answer.startswith("Hannah antwortet gerade nicht")
        assert not client._stubs.previous
        assert ("v1", "SubmitText") not in hits
        assert not _fallback_warnings(caplog)
    finally:
        await client.close()
        await server.stop(None)
