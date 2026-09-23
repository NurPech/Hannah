from unittest.mock import MagicMock

import pytest

from hannah_telegram.grpc_client import HannahClient
from hannah_telegram.grpc_interceptors import PROTO_VERSION_METADATA_KEY, read_proto_version


async def test_subscribe_events_sends_proto_version_metadata_explicitly():
    """Regression: grpc.aio's UnaryStreamClientInterceptor doesn't reliably apply
    metadata mutations for streaming calls (unlike unary-unary) — SubscribeEvents
    needs x-proto-version and x-compat-version passed explicitly instead of
    relying on the interceptors (#60, #217)."""
    client = HannahClient("localhost", 50051)
    client._stub = MagicMock()
    client._stub.SubscribeEvents.side_effect = RuntimeError("stop after first call")

    with pytest.raises(RuntimeError):
        await client.subscribe_events([], on_event=lambda _e: None)

    client._stub.SubscribeEvents.assert_called_once()
    _, kwargs = client._stub.SubscribeEvents.call_args
    assert kwargs["metadata"][0] == (PROTO_VERSION_METADATA_KEY, read_proto_version())
    assert kwargs["metadata"][1][0] == "x-compat-version"


# ---------------------------------------------------------------------------
# ChannelConnect + link tokens (#334)
# ---------------------------------------------------------------------------

import asyncio
from types import SimpleNamespace

from hannah_proto import hannah_pb2


class _FakeChannelCall:
    """Stands in for a grpc.aio stream-stream call: records writes, yields `commands`,
    then raises CancelledError so channel_connect() returns instead of reconnecting."""

    def __init__(self, commands):
        self.written = []
        self._commands = commands

    async def write(self, msg):
        self.written.append(msg)

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for cmd in self._commands:
            yield cmd
        raise asyncio.CancelledError


async def test_channel_connect_registers_and_dispatches_redeem_result():
    client = HannahClient("localhost", 50051)
    fut = asyncio.get_running_loop().create_future()
    client._pending_redeems["r1"] = fut
    call = _FakeChannelCall([
        hannah_pb2.ChannelCommand(registered=hannah_pb2.ChannelRegistered()),
        hannah_pb2.ChannelCommand(redeem_result=hannah_pb2.RedeemLinkTokenResult(
            request_id="r1", result=hannah_pb2.REDEEM_OK,
        )),
    ])
    client._stub = MagicMock()
    client._stub.ChannelConnect.return_value = call

    register = hannah_pb2.ChannelRegister(service="telegram", link_url_template="https://t.me/Bot?start={token}")
    await client.channel_connect(register)

    assert call.written == [hannah_pb2.ChannelMessage(register=register)]
    assert fut.result().result == hannah_pb2.REDEEM_OK
    _, kwargs = client._stub.ChannelConnect.call_args
    assert kwargs["metadata"][0] == (PROTO_VERSION_METADATA_KEY, read_proto_version())
    assert kwargs["metadata"][1][0] == "x-compat-version"
    assert client._channel_call is None  # cleared once the stream is gone


async def test_redeem_link_token_resolves_matching_answer():
    client = HannahClient("localhost", 50051)

    async def write(msg):
        rid = msg.redeem.request_id
        assert msg.redeem.token == "tok" and msg.redeem.account_id == "123"
        client._pending_redeems[rid].set_result(
            hannah_pb2.RedeemLinkTokenResult(request_id=rid, result=hannah_pb2.REDEEM_OK)
        )

    client._channel_call = SimpleNamespace(write=write)
    result = await client.redeem_link_token("tok", "123")

    assert result.result == hannah_pb2.REDEEM_OK
    assert client._pending_redeems == {}


async def test_redeem_link_token_without_stream_returns_none():
    client = HannahClient("localhost", 50051)
    assert await client.redeem_link_token("tok", "123") is None


async def test_redeem_link_token_times_out():
    client = HannahClient("localhost", 50051)

    async def write(_msg):
        pass

    client._channel_call = SimpleNamespace(write=write)
    assert await client.redeem_link_token("tok", "123", timeout=0.01) is None
    assert client._pending_redeems == {}
