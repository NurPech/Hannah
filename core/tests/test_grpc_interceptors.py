import logging
from unittest.mock import MagicMock

import grpc
from hannah_proto import PROTO_VERSION as _PROTO_VERSION

from hannah.grpc_interceptors import (
    PROTO_VERSION_METADATA_KEY,
    OutdatedComponentInterceptor,
    ProtocolVersionInterceptor,
    read_proto_version,
)

LEGACY_PREFIX = "/hannah.v1.HannahService/"

EXPECTED_VERSION = str(_PROTO_VERSION)
UNKNOWN_METHOD = "/hannah.HannahService/SubmitText"  # the unversioned package is gone since hannah.v2


def _handler_call_details(method="/hannah.v2.HannahService/SubmitText", version=EXPECTED_VERSION):
    metadata = ((PROTO_VERSION_METADATA_KEY, version),) if version is not None else ()
    return MagicMock(method=method, invocation_metadata=metadata)


def _unary_handler():
    return grpc.unary_unary_rpc_method_handler(lambda request, context: "ok")


def _version_records(caplog):
    return [r for r in caplog.records if "[grpc/version]" in r.getMessage()]


def test_read_proto_version_matches_package():
    assert read_proto_version() == EXPECTED_VERSION


def test_matching_version_passes_through_without_log(caplog):
    interceptor = ProtocolVersionInterceptor(EXPECTED_VERSION)
    handler = _unary_handler()

    with caplog.at_level(logging.DEBUG, logger="hannah.grpc_interceptors"):
        result = interceptor.intercept_service(MagicMock(return_value=handler), _handler_call_details())

    assert result is handler
    assert not _version_records(caplog)


def test_mismatch_is_never_rejected_only_logged(caplog):
    # #359: x-proto-version is diagnostic only — the versioned path decides compatibility.
    interceptor = ProtocolVersionInterceptor(EXPECTED_VERSION)
    handler = _unary_handler()

    with caplog.at_level(logging.INFO, logger="hannah.grpc_interceptors"):
        result = interceptor.intercept_service(MagicMock(return_value=handler), _handler_call_details(version="999"))

    assert result is handler
    records = _version_records(caplog)
    assert len(records) == 1
    assert "999" in records[0].getMessage()


def test_missing_metadata_is_never_rejected():
    interceptor = ProtocolVersionInterceptor(EXPECTED_VERSION)
    handler = _unary_handler()

    result = interceptor.intercept_service(MagicMock(return_value=handler), _handler_call_details(version=None))

    assert result is handler


def test_unknown_method_logs_path_and_header_and_stays_unimplemented(caplog):
    # A component older than N−1 calls a path Core no longer serves: handler is None,
    # gRPC answers UNIMPLEMENTED — but path and x-proto-version must be logged first.
    interceptor = ProtocolVersionInterceptor(EXPECTED_VERSION)

    with caplog.at_level(logging.INFO, logger="hannah.grpc_interceptors"):
        result = interceptor.intercept_service(
            MagicMock(return_value=None), _handler_call_details(method=UNKNOWN_METHOD, version="1")
        )

    assert result is None
    records = _version_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert UNKNOWN_METHOD in records[0].getMessage()
    assert "'1'" in records[0].getMessage()


def test_same_path_and_header_logged_only_once(caplog):
    interceptor = ProtocolVersionInterceptor(EXPECTED_VERSION)
    continuation = MagicMock(return_value=None)

    with caplog.at_level(logging.INFO, logger="hannah.grpc_interceptors"):
        for _ in range(5):
            interceptor.intercept_service(continuation, _handler_call_details(method=UNKNOWN_METHOD, version="1"))
        # a different header on the same path is a new finding
        interceptor.intercept_service(continuation, _handler_call_details(method=UNKNOWN_METHOD, version="2"))

    assert len(_version_records(caplog)) == 2


class TestOutdatedComponentInterceptor:
    """#358 — jeder Call verrät gratis über den Pfad, ob N oder N−1 gerufen wurde;
    hannah-grpc-lib hängt x-proto-version an jeden Call, unabhängig von irgendeiner
    komponentenspezifischen Registrierungsnachricht."""

    def test_legacy_path_notifies_with_bare_method_name_and_proto_version(self):
        notifier = MagicMock()
        interceptor = OutdatedComponentInterceptor(notifier, legacy_prefix=LEGACY_PREFIX)
        handler = _unary_handler()

        result = interceptor.intercept_service(
            MagicMock(return_value=handler),
            _handler_call_details(method=f"{LEGACY_PREFIX}ChannelConnect", version="3.2.0"),
        )

        assert result is handler
        notifier.notify_legacy_call.assert_called_once_with("ChannelConnect", "3.2.0")
        notifier.notify_current_call.assert_not_called()

    def test_current_path_reports_recovery_not_a_new_notice(self):
        notifier = MagicMock()
        interceptor = OutdatedComponentInterceptor(notifier, legacy_prefix=LEGACY_PREFIX)
        handler = _unary_handler()

        interceptor.intercept_service(
            MagicMock(return_value=handler),
            _handler_call_details(method="/hannah.v2.HannahService/ChannelConnect", version=EXPECTED_VERSION),
        )

        notifier.notify_current_call.assert_called_once_with("ChannelConnect")
        notifier.notify_legacy_call.assert_not_called()

    def test_missing_proto_version_header_passed_as_empty_string(self):
        notifier = MagicMock()
        interceptor = OutdatedComponentInterceptor(notifier, legacy_prefix=LEGACY_PREFIX)
        handler = _unary_handler()

        interceptor.intercept_service(
            MagicMock(return_value=handler),
            _handler_call_details(method=f"{LEGACY_PREFIX}AgentConnect", version=None),
        )

        notifier.notify_legacy_call.assert_called_once_with("AgentConnect", "")

    def test_unimplemented_method_is_not_reported(self):
        """handler is None (Methode existiert nicht mehr) — kein Fall für #358, das
        deckt ProtocolVersionInterceptor bereits als eigene Diagnose ab."""
        notifier = MagicMock()
        interceptor = OutdatedComponentInterceptor(notifier, legacy_prefix=LEGACY_PREFIX)

        result = interceptor.intercept_service(
            MagicMock(return_value=None),
            _handler_call_details(method=f"{LEGACY_PREFIX}SomeAncientMethod", version="1.0.0"),
        )

        assert result is None
        notifier.notify_legacy_call.assert_not_called()
        notifier.notify_current_call.assert_not_called()
