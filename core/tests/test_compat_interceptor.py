from unittest.mock import MagicMock

import grpc
from hannah_proto import hannah_pb2 as pb
from hannah_proto.interceptor.compat_interceptor import (
    COMPAT_VERSION_METADATA_KEY,
    DEFAULT_COMPAT_VERSION,
    CompatVersionInterceptor,
    build_required_versions,
    get_message_compat_version,
)

HANNAH_SERVICE = pb.DESCRIPTOR.services_by_name["HannahService"]


def _handler_call_details(method="/hannah.HannahService/SubmitText", version=None):
    metadata = ((COMPAT_VERSION_METADATA_KEY, version),) if version is not None else ()
    return MagicMock(method=method, invocation_metadata=metadata)


def _unary_handler():
    return grpc.unary_unary_rpc_method_handler(lambda request, context: "ok")


def _stream_stream_handler():
    return grpc.stream_stream_rpc_method_handler(lambda request_iterator, context: iter(["ok"]))


def test_message_without_compat_version_option_defaults_to_1():
    assert get_message_compat_version(pb.SubmitTextRequest.DESCRIPTOR) == DEFAULT_COMPAT_VERSION


def test_build_required_versions_covers_every_method_with_full_method_path():
    versions = build_required_versions(HANNAH_SERVICE)

    assert "/hannah.HannahService/SubmitText" in versions
    # No message currently sets compat_version explicitly (per options.proto's
    # "don't backfill" convention) — every method should require the default.
    assert all(v == DEFAULT_COMPAT_VERSION for v in versions.values())


def test_sufficient_client_version_passes_through():
    interceptor = CompatVersionInterceptor(HANNAH_SERVICE, enforce=True)
    interceptor._required["/hannah.HannahService/SubmitText"] = 2
    handler = _unary_handler()
    continuation = MagicMock(return_value=handler)

    result = interceptor.intercept_service(continuation, _handler_call_details(version="2"))

    assert result is handler


def test_missing_metadata_treated_as_version_1_and_enforce_false_only_logs():
    interceptor = CompatVersionInterceptor(HANNAH_SERVICE, enforce=False)
    interceptor._required["/hannah.HannahService/SubmitText"] = 2
    handler = _unary_handler()
    continuation = MagicMock(return_value=handler)

    result = interceptor.intercept_service(continuation, _handler_call_details(version=None))

    assert result is handler


def test_insufficient_version_enforce_true_aborts_unary_call():
    interceptor = CompatVersionInterceptor(HANNAH_SERVICE, enforce=True)
    interceptor._required["/hannah.HannahService/SubmitText"] = 2
    continuation = MagicMock(return_value=_unary_handler())

    result = interceptor.intercept_service(continuation, _handler_call_details(version="1"))

    context = MagicMock()
    result.unary_unary(MagicMock(), context)
    context.abort.assert_called_once()
    code, message = context.abort.call_args[0]
    assert code == grpc.StatusCode.FAILED_PRECONDITION
    assert "requires 2" in message
    assert "declared 1" in message


def test_insufficient_version_preserves_streaming_shape():
    interceptor = CompatVersionInterceptor(HANNAH_SERVICE, enforce=True)
    interceptor._required["/hannah.HannahService/AgentConnect"] = 2
    continuation = MagicMock(return_value=_stream_stream_handler())

    result = interceptor.intercept_service(
        continuation, _handler_call_details(method="/hannah.HannahService/AgentConnect", version="1")
    )

    assert result.request_streaming is True
    assert result.response_streaming is True

    context = MagicMock()
    result.stream_stream(iter([MagicMock()]), context)
    context.abort.assert_called_once()
    assert context.abort.call_args[0][0] == grpc.StatusCode.FAILED_PRECONDITION


def test_unrelated_method_unaffected_by_a_different_methods_required_version():
    """The whole point of compat_version: a breaking change scoped to one
    method's messages must not reject calls to an unrelated method."""
    interceptor = CompatVersionInterceptor(HANNAH_SERVICE, enforce=True)
    interceptor._required["/hannah.HannahService/SubmitText"] = 2
    handler = _unary_handler()
    continuation = MagicMock(return_value=handler)

    # GetSatellites wasn't touched by whatever bumped SubmitText's version —
    # an old client (no x-compat-version header, implicit version 1) must
    # still be let through.
    result = interceptor.intercept_service(
        continuation, _handler_call_details(method="/hannah.HannahService/GetSatellites", version=None)
    )

    assert result is handler


def test_enforce_can_be_toggled_at_runtime():
    interceptor = CompatVersionInterceptor(HANNAH_SERVICE, enforce=False)
    interceptor._required["/hannah.HannahService/SubmitText"] = 2
    handler = _unary_handler()
    continuation = MagicMock(return_value=handler)

    # enforce=False -> passes through despite insufficient version
    assert interceptor.intercept_service(continuation, _handler_call_details(version="1")) is handler

    # flip to enforce=True -> same call now gets rejected
    interceptor.enforce = True
    result = interceptor.intercept_service(continuation, _handler_call_details(version="1"))
    assert result is not handler
