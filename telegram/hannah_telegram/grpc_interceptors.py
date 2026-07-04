"""
Protocol-Version-Client-Interceptor (#60).

Telegram ist einer von 6 externen Hannah-Clients (siehe Hannah-Core-Seite in
core/hannah/grpc_interceptors.py). Dieser Interceptor hängt bei jedem
ausgehenden RPC (unary und streaming) die Metadata `x-proto-version` an —
statisch aus der lokal mitkopierten PROTO_VERSION-Datei gelesen, einmalig
beim Channel-Aufbau konfiguriert statt pro Call-Site.
"""
import collections
import os

import grpc
import grpc.aio

PROTO_VERSION_METADATA_KEY = "x-proto-version"


def read_proto_version() -> str:
    """Liest die lokale PROTO_VERSION-Datei (mitkopiert von proto/, siehe gen_proto.sh)."""
    path = os.path.join(os.path.dirname(__file__), "proto", "PROTO_VERSION")
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


class _ClientCallDetails(
    collections.namedtuple(
        "_ClientCallDetails",
        ("method", "timeout", "metadata", "credentials", "wait_for_ready"),
    ),
    grpc.aio.ClientCallDetails,
):
    pass


def _add_version_metadata(client_call_details, version: str) -> _ClientCallDetails:
    metadata = list(client_call_details.metadata or [])
    metadata.append((PROTO_VERSION_METADATA_KEY, version))
    return _ClientCallDetails(
        client_call_details.method,
        client_call_details.timeout,
        metadata,
        client_call_details.credentials,
        client_call_details.wait_for_ready,
    )


class ProtocolVersionClientInterceptor(
    grpc.aio.UnaryUnaryClientInterceptor,
    grpc.aio.UnaryStreamClientInterceptor,
    grpc.aio.StreamUnaryClientInterceptor,
    grpc.aio.StreamStreamClientInterceptor,
):
    def __init__(self, version: str):
        self._version = version

    async def intercept_unary_unary(self, continuation, client_call_details, request):
        return await continuation(_add_version_metadata(client_call_details, self._version), request)

    async def intercept_unary_stream(self, continuation, client_call_details, request):
        return await continuation(_add_version_metadata(client_call_details, self._version), request)

    async def intercept_stream_unary(self, continuation, client_call_details, request_iterator):
        return await continuation(_add_version_metadata(client_call_details, self._version), request_iterator)

    async def intercept_stream_stream(self, continuation, client_call_details, request_iterator):
        return await continuation(_add_version_metadata(client_call_details, self._version), request_iterator)
