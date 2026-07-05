"""
Protocol-Version-Check-Interceptor (#60).

Jeder der 6 externen Hannah-Clients (Adapter, Telegram, Proxy, Timer Service,
WebUI, Wakeword Collector) schickt bei jedem RPC die Metadata
`x-proto-version` mit — ein statischer Wert, gelesen aus dem jeweils
eingebundenen proto-Submodule (hannah-proto). Dieser Interceptor läuft vor
JEDEM RPC (unary wie streaming, da er auf Service-Ebene ansetzt, nicht auf
den einzelnen Handler) und vergleicht den Wert gegen Hannahs eigene
PROTO_VERSION.

`enforce=False` (Default) protokolliert einen Mismatch nur — Hannah nimmt den
Call trotzdem an. Erst wenn alle Clients umgestellt sind, wird per
`grpc.enforce_protocol_version: true` (config.yaml) bzw. zur Laufzeit über
GrpcServer.set_protocol_version_enforcement() scharf geschaltet; danach wird
jeder Mismatch/jedes Fehlen der Metadata mit FAILED_PRECONDITION abgelehnt,
bevor der eigentliche Handler läuft.
"""
import logging

import grpc
from hannah_proto import PROTO_VERSION

log = logging.getLogger(__name__)

PROTO_VERSION_METADATA_KEY = "x-proto-version"


def read_proto_version() -> str:
    """hannah_proto.PROTO_VERSION as the string the x-proto-version metadata value needs to be."""
    return str(PROTO_VERSION)


class ProtocolVersionInterceptor(grpc.ServerInterceptor):
    def __init__(self, expected_version: str, enforce: bool = False):
        self._expected_version = expected_version
        self.enforce = enforce  # öffentlich: zur Laufzeit umschaltbar, siehe GrpcServer.set_protocol_version_enforcement

    def intercept_service(self, continuation, handler_call_details):
        handler = continuation(handler_call_details)
        if handler is None:
            return handler

        metadata = dict(handler_call_details.invocation_metadata or ())
        received = metadata.get(PROTO_VERSION_METADATA_KEY)

        if received == self._expected_version:
            return handler

        message = (
            f"Proto-Version-Mismatch auf {handler_call_details.method!r}: "
            f"erwartet {self._expected_version!r}, erhalten {received!r}"
        )
        if not self.enforce:
            log.warning(f"[grpc/version] {message} — nur geloggt (enforce=False)")
            return handler

        log.warning(f"[grpc/version] {message} — RPC abgelehnt")
        return _make_abort_handler(handler, message)


def _make_abort_handler(handler: "grpc.RpcMethodHandler", message: str) -> "grpc.RpcMethodHandler":
    code = grpc.StatusCode.FAILED_PRECONDITION

    if handler.request_streaming and handler.response_streaming:
        def behavior(request_iterator, context):
            context.abort(code, message)
        return grpc.stream_stream_rpc_method_handler(
            behavior,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )
    if handler.request_streaming and not handler.response_streaming:
        def behavior(request_iterator, context):
            context.abort(code, message)
        return grpc.stream_unary_rpc_method_handler(
            behavior,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )
    if not handler.request_streaming and handler.response_streaming:
        def behavior(request, context):
            context.abort(code, message)
        return grpc.unary_stream_rpc_method_handler(
            behavior,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )

    def behavior(request, context):
        context.abort(code, message)
    return grpc.unary_unary_rpc_method_handler(
        behavior,
        request_deserializer=handler.request_deserializer,
        response_serializer=handler.response_serializer,
    )
