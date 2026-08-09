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
