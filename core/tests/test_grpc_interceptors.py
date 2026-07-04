import os
from unittest.mock import MagicMock

import grpc

from hannah.grpc_interceptors import (
    PROTO_VERSION_METADATA_KEY,
    ProtocolVersionInterceptor,
    read_proto_version,
)

EXPECTED_VERSION = "1"


def _handler_call_details(method="/hannah.HannahService/SubmitText", version=EXPECTED_VERSION):
    metadata = ((PROTO_VERSION_METADATA_KEY, version),) if version is not None else ()
    return MagicMock(method=method, invocation_metadata=metadata)


def _unary_handler():
    return grpc.unary_unary_rpc_method_handler(lambda request, context: "ok")


def _stream_stream_handler():
    return grpc.stream_stream_rpc_method_handler(lambda request_iterator, context: iter(["ok"]))


def test_read_proto_version_matches_committed_file():
    path = os.path.join(os.path.dirname(__file__), "..", "hannah", "proto", "PROTO_VERSION")
    with open(path, "r", encoding="utf-8") as f:
        expected = f.read().strip()
    assert read_proto_version() == expected


def test_matching_version_passes_through_unchanged():
    interceptor = ProtocolVersionInterceptor(EXPECTED_VERSION, enforce=True)
    handler = _unary_handler()
    continuation = MagicMock(return_value=handler)

    result = interceptor.intercept_service(continuation, _handler_call_details(version=EXPECTED_VERSION))

    assert result is handler


def test_mismatch_enforce_false_only_logs_and_passes_through():
    interceptor = ProtocolVersionInterceptor(EXPECTED_VERSION, enforce=False)
    handler = _unary_handler()
    continuation = MagicMock(return_value=handler)

    result = interceptor.intercept_service(continuation, _handler_call_details(version="999"))

    assert result is handler


def test_missing_metadata_enforce_false_only_logs_and_passes_through():
    interceptor = ProtocolVersionInterceptor(EXPECTED_VERSION, enforce=False)
    handler = _unary_handler()
    continuation = MagicMock(return_value=handler)

    result = interceptor.intercept_service(continuation, _handler_call_details(version=None))

    assert result is handler


def test_mismatch_enforce_true_aborts_unary_call():
    interceptor = ProtocolVersionInterceptor(EXPECTED_VERSION, enforce=True)
    continuation = MagicMock(return_value=_unary_handler())

    result = interceptor.intercept_service(continuation, _handler_call_details(version="999"))

    assert result.request_streaming is False
    assert result.response_streaming is False

    context = MagicMock()
    result.unary_unary(MagicMock(), context)
    context.abort.assert_called_once()
    code, message = context.abort.call_args[0]
    assert code == grpc.StatusCode.FAILED_PRECONDITION
    assert "999" in message
    assert EXPECTED_VERSION in message


def test_missing_metadata_enforce_true_aborts():
    interceptor = ProtocolVersionInterceptor(EXPECTED_VERSION, enforce=True)
    continuation = MagicMock(return_value=_unary_handler())

    result = interceptor.intercept_service(continuation, _handler_call_details(version=None))

    context = MagicMock()
    result.unary_unary(MagicMock(), context)
    context.abort.assert_called_once()
    code, message = context.abort.call_args[0]
    assert code == grpc.StatusCode.FAILED_PRECONDITION
    assert "None" in message


def test_mismatch_enforce_true_preserves_streaming_shape():
    interceptor = ProtocolVersionInterceptor(EXPECTED_VERSION, enforce=True)
    continuation = MagicMock(return_value=_stream_stream_handler())

    result = interceptor.intercept_service(continuation, _handler_call_details(version="999"))

    assert result.request_streaming is True
    assert result.response_streaming is True

    context = MagicMock()
    result.stream_stream(iter([MagicMock()]), context)
    context.abort.assert_called_once()
    assert context.abort.call_args[0][0] == grpc.StatusCode.FAILED_PRECONDITION


def test_enforce_can_be_toggled_at_runtime():
    interceptor = ProtocolVersionInterceptor(EXPECTED_VERSION, enforce=False)
    handler = _unary_handler()
    continuation = MagicMock(return_value=handler)

    # enforce=False -> passes through despite mismatch
    assert interceptor.intercept_service(continuation, _handler_call_details(version="999")) is handler

    # flip to enforce=True -> same mismatch now gets rejected
    interceptor.enforce = True
    result = interceptor.intercept_service(continuation, _handler_call_details(version="999"))
    assert result is not handler
