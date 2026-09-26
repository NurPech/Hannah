"""
N−1-Servicer für das unversionierte `hannah`-Paket (hannah-proto#11, #359).

Core arbeitet intern ausschließlich mit `hannah.v1` (N). Komponenten, die
noch auf dem eingefrorenen, unversionierten `hannah`-Paket (N−1) stehen,
rufen `/hannah.HannahService/...` statt `/hannah.v1.HannahService/...` auf.
Dieses Modul bedient diese Pfade mit denselben (v1-)Handlern.

Den Servicer einfach ein zweites Mal über `hannah_proto.hannah_pb2_grpc`
zu registrieren geht nicht: dessen Serializer lehnen die v1-Objekte ab, die
die Handler zurückgeben. Stattdessen wird pro Methode ein Handler auf Basis
des N−1-Service-Descriptors gebaut, mit den v1-Typen als (De-)Serializer.
Das funktioniert, weil beide Pakete auf der Leitung bytegleich sind — das
Paket steht nirgends in der Wire-Form, nur Feldnummern und -typen.

Das ist der dauerhafte N−1-Servicer, kein Provisorium: er bleibt, bis
`hannah.v2` kommt und das unversionierte Paket nach der N−1-Regel wegfällt.
Ändert sich eine Methode in v1 so, dass sie nicht mehr bytegleich ist,
bekommt genau diese Methode hier einen echten Übersetzer
(`tests/test_grpc_legacy.py` schlägt in dem Fall an).
"""
import logging

import grpc
from google.protobuf.descriptor import MethodDescriptor, ServiceDescriptor
from google.protobuf.message_factory import GetMessageClass

from hannah_proto import hannah_pb2 as legacy_pb
from hannah_proto.v1 import hannah_pb2 as pb

log = logging.getLogger(__name__)

SERVICE_NAME = "HannahService"
LEGACY_SERVICE: ServiceDescriptor = legacy_pb.DESCRIPTOR.services_by_name[SERVICE_NAME]
CURRENT_SERVICE: ServiceDescriptor = pb.DESCRIPTOR.services_by_name[SERVICE_NAME]


def _method_handler(servicer, legacy: MethodDescriptor, current: MethodDescriptor) -> grpc.RpcMethodHandler:
    behavior = getattr(servicer, current.name)
    request_deserializer = GetMessageClass(current.input_type).FromString
    response_serializer = GetMessageClass(current.output_type).SerializeToString

    if legacy.client_streaming and legacy.server_streaming:
        factory = grpc.stream_stream_rpc_method_handler
    elif legacy.client_streaming:
        factory = grpc.stream_unary_rpc_method_handler
    elif legacy.server_streaming:
        factory = grpc.unary_stream_rpc_method_handler
    else:
        factory = grpc.unary_unary_rpc_method_handler
    return factory(
        behavior,
        request_deserializer=request_deserializer,
        response_serializer=response_serializer,
    )


def build_legacy_method_handlers(servicer) -> dict[str, grpc.RpcMethodHandler]:
    """Methodenname -> Handler für jede Methode des N−1-Service, die es in v1 gleichförmig gibt."""
    handlers = {}
    for legacy in LEGACY_SERVICE.methods:
        current = CURRENT_SERVICE.methods_by_name.get(legacy.name)
        if current is None or (current.client_streaming, current.server_streaming) != (
            legacy.client_streaming, legacy.server_streaming,
        ):
            # Innerhalb einer Generation nur additive Änderungen — sollte nie
            # passieren. Falls doch: nicht raten, die Methode bleibt UNIMPLEMENTED.
            log.error(
                f"[grpc/legacy] {legacy.full_name} hat kein gleichförmiges Gegenstück in "
                f"{CURRENT_SERVICE.full_name} — wird für N−1-Clients nicht bedient"
            )
            continue
        handlers[legacy.name] = _method_handler(servicer, legacy, current)
    return handlers


def add_legacy_servicer_to_server(servicer, server: grpc.Server) -> None:
    """Registriert die v1-Handler zusätzlich unter `/hannah.HannahService/...` (N−1)."""
    handlers = build_legacy_method_handlers(servicer)
    server.add_generic_rpc_handlers(
        (grpc.method_handlers_generic_handler(LEGACY_SERVICE.full_name, handlers),)
    )
    server.add_registered_method_handlers(LEGACY_SERVICE.full_name, handlers)
