import os
from unittest.mock import AsyncMock

import grpc.aio

from hannah_telegram.grpc_interceptors import (
    PROTO_VERSION_METADATA_KEY,
    ProtocolVersionClientInterceptor,
    read_proto_version,
)

EXPECTED_VERSION = "1"


class _FakeCallDetails:
    def __init__(self, method="/hannah.HannahService/SubmitText", metadata=None):
        self.method = method
        self.timeout = None
        self.metadata = metadata
        self.credentials = None
        self.wait_for_ready = None


def test_read_proto_version_matches_committed_file():
    path = os.path.join(os.path.dirname(__file__), "..", "hannah_telegram", "proto", "PROTO_VERSION")
    with open(path, "r", encoding="utf-8") as f:
        expected = f.read().strip()
    assert read_proto_version() == expected


async def test_intercept_unary_unary_adds_version_metadata():
    interceptor = ProtocolVersionClientInterceptor(EXPECTED_VERSION)
    continuation = AsyncMock(return_value="call-result")
    details = _FakeCallDetails(metadata=[("existing", "1")])

    result = await interceptor.intercept_unary_unary(continuation, details, "request")

    assert result == "call-result"
    continuation.assert_awaited_once()
    forwarded_details, forwarded_request = continuation.call_args[0]
    assert forwarded_request == "request"
    assert ("existing", "1") in forwarded_details.metadata
    assert (PROTO_VERSION_METADATA_KEY, EXPECTED_VERSION) in forwarded_details.metadata
    assert forwarded_details.method == details.method


async def test_intercept_unary_unary_preserves_missing_metadata():
    interceptor = ProtocolVersionClientInterceptor(EXPECTED_VERSION)
    continuation = AsyncMock(return_value="call-result")
    details = _FakeCallDetails(metadata=None)

    await interceptor.intercept_unary_unary(continuation, details, "request")

    forwarded_details, _ = continuation.call_args[0]
    assert forwarded_details.metadata == [(PROTO_VERSION_METADATA_KEY, EXPECTED_VERSION)]


async def test_intercept_unary_stream_adds_version_metadata():
    interceptor = ProtocolVersionClientInterceptor(EXPECTED_VERSION)
    continuation = AsyncMock(return_value="stream-call")
    details = _FakeCallDetails(method="/hannah.HannahService/SubscribeEvents")

    result = await interceptor.intercept_unary_stream(continuation, details, "request")

    assert result == "stream-call"
    forwarded_details, forwarded_request = continuation.call_args[0]
    assert forwarded_request == "request"
    assert (PROTO_VERSION_METADATA_KEY, EXPECTED_VERSION) in forwarded_details.metadata


async def test_intercept_stream_unary_and_stream_stream_forward_request_iterator():
    interceptor = ProtocolVersionClientInterceptor(EXPECTED_VERSION)
    continuation = AsyncMock(return_value="ok")
    details = _FakeCallDetails()
    request_iterator = iter(["a", "b"])

    await interceptor.intercept_stream_unary(continuation, details, request_iterator)
    forwarded_details, forwarded_iter = continuation.call_args[0]
    assert forwarded_iter is request_iterator
    assert (PROTO_VERSION_METADATA_KEY, EXPECTED_VERSION) in forwarded_details.metadata

    continuation.reset_mock()
    await interceptor.intercept_stream_stream(continuation, details, request_iterator)
    forwarded_details, forwarded_iter = continuation.call_args[0]
    assert forwarded_iter is request_iterator
    assert (PROTO_VERSION_METADATA_KEY, EXPECTED_VERSION) in forwarded_details.metadata
