"""
N−1-Servicer für `hannah.v1` (hannah-proto#19, #384).

Core arbeitet intern ausschließlich mit `hannah.v2` (N). Komponenten, die noch auf
`hannah.v1` (N−1) stehen, rufen `/hannah.v1.HannahService/...` auf. Dieses Modul
bedient diese Pfade mit denselben (v2-)Handlern und übersetzt an der Grenze:

- Methoden, deren Nachrichten sich nicht unterscheiden, gehen durch
  `hannah_grpc.translate` (Descriptor-Kopie nach Feldnamen: Requests strikt, ein
  Feld, das v2 nicht kennt, ist ein Fehler; Responses lenient). Es wird nirgends
  angenommen, dass die Generationen bytegleich wären.
- `GetDevices`, `ControlDevice` und `AgentConnect` sind der Teil, in dem sich die
  Generationen unterscheiden (State-basiert vs. typisierte Geräte). Für v1-Clients
  und v1-Adapter bleiben sie nativ: Core führt die Geräte eines v1-Adapters
  weiterhin über den State-basierten Pfad (`IoBrokerClient.handle_device_snapshot`),
  ohne Umweg über ein typisiertes Modell — das Verhalten für bestehende Nutzer
  ändert sich nicht. Typisierte Geräte konsumiert Core erst mit #383.

Der N−1-Servicer ist dauerhaft bis `hannah.v3`, kein Provisorium. Ändert sich eine
Methode so, dass es kein gleichförmiges Gegenstück mehr gibt, wird sie nicht bedient
und geloggt (`tests/test_grpc_v1.py` schlägt in dem Fall an).
"""
import logging
import queue

import grpc
from google.protobuf.descriptor import MethodDescriptor
from google.protobuf.message_factory import GetMessageClass

from hannah_grpc.translate import TranslationError, translate
from hannah_proto.v1 import hannah_pb2 as pb1
from hannah_proto.v2 import hannah_pb2 as pb2

log = logging.getLogger(__name__)

SERVICE_NAME = "HannahService"
V1_SERVICE = pb1.DESCRIPTOR.services_by_name[SERVICE_NAME]
V2_SERVICE = pb2.DESCRIPTOR.services_by_name[SERVICE_NAME]
V1_PREFIX = f"/{V1_SERVICE.full_name}/"

# Legacy-Absolutwerte von AgentSetResident.presence_state (hannah.v1), wenn der Aufrufer
# keinen eigenen mitgibt: 0 = abwesend, 1 = zuhause, 2 = Nacht.
_PRESENCE_STATE_BY_ACTION = {
    pb2.AWAY: 0,
    pb2.HOME: 1,
    pb2.ASLEEP: 2,
    pb2.AWAKE: 1,
}


# ------------------------------------------------------------------
# GetDevices (State-basiert, nativ für v1-Clients)

def get_devices_response(rooms_raw: list) -> "pb1.GetDevicesResponse":
    """Geräteliste für Steuer-Menüs in der State-basierten Form von hannah.v1."""
    rooms = []
    for r in rooms_raw:
        devices = [
            pb1.DeviceInfo(
                id=d["id"],
                name=d["name"],
                category=d["category"],
                states=d["states"],
                current=d["current"],
                state_types=d["state_types"],
                state_enum_values={
                    k: pb1.EnumValues(values=v) for k, v in d["state_enum_values"].items()
                },
                state_writable=d["state_writable"],
            )
            for d in r["devices"]
        ]
        rooms.append(pb1.RoomInfo(key=r["key"], name=r["name"], devices=devices))
    return pb1.GetDevicesResponse(rooms=rooms)


def with_v1_state_format(response: "pb1.GetDevicesResponse") -> "pb1.GetDevicesResponse":
    """Boolean-States im Format des alten Gerätebaums (`"True"`/`"False"`, #388).

    Die Lib-Übersetzung schreibt `"true"`/`"false"`; v1-Clients (Telegram) vergleichen gegen
    `str(bool)`. hannah.v1 ist eingefroren, also gilt das alte Format weiter."""
    for room in response.rooms:
        for device in room.devices:
            for key, state_type in device.state_types.items():
                if state_type == pb1.StateType.BOOLEAN and key in device.current:
                    device.current[key] = "True" if device.current[key].lower() == "true" else "False"
    return response


# ------------------------------------------------------------------
# AgentConnect: Session eines v1-Adapters

def command_to_v1(command: "pb2.AgentCommand", presence_state=None):
    """Ein Core-Befehl (hannah.v2) als hannah.v1-AgentCommand für einen v1-Adapter.

    None, wenn es für v1 kein Gegenstück gibt (SetSlot: ein v1-Adapter kennt keine
    typisierten Geräte, er steuert über SetState).
    presence_state: Legacy-Absolutwert für AgentSetResident, v2 kennt nur noch `action`.
    """
    which = command.WhichOneof("command")
    if which is None:
        return None
    if which == "set_slot":
        log.warning("[grpc/v1] SetSlot gibt es für hannah.v1-Adapter nicht, Befehl verworfen")
        return None
    try:
        inner = translate(getattr(command, which), "v1", strict=False)
    except TranslationError as exc:
        log.error(f"[grpc/v1] Befehl {which!r} nicht nach hannah.v1 übersetzbar: {exc}")
        return None
    if which == "set_resident":
        inner.presence_state = (
            presence_state if presence_state is not None
            else _PRESENCE_STATE_BY_ACTION.get(command.set_resident.action, 0)
        )
    return pb1.AgentCommand(**{which: inner})


class V1AgentSession:
    """Ein verbundener hannah.v1-Adapter. Gleiche Schnittstelle wie die v2-Session in
    grpc_server.py: Core legt v2-Befehle mit `put()` ab, `get()` liefert sie als v1."""

    generation = "v1"

    def __init__(self):
        self._queue: queue.Queue = queue.Queue()

    def put(self, command, presence_state=None) -> None:
        v1_command = command_to_v1(command, presence_state)
        if v1_command is not None:
            self._queue.put(v1_command)

    def get(self, timeout: float):
        return self._queue.get(timeout=timeout)

    def close(self) -> None:
        self._queue.put(None)

    def decode(self, msg):
        """`(Payload-Name, Payload)` einer eingehenden AgentMessage, Payload als hannah.v2.

        Die State-basierten Geräte-Nachrichten bleiben nativ v1 und heißen `legacy_*`:
        ein v1-Snapshot ist kein typisierter Snapshot, ein v1-State-Update trägt
        canonical_key. Alles andere wird nach v2 übersetzt (strikt). `(None, None)`,
        wenn der Payload unbekannt oder nicht übersetzbar ist."""
        which = msg.WhichOneof("payload")
        if which is None:
            return None, None
        if which == "send_snapshot":
            return "legacy_snapshot", msg.send_snapshot
        if which == "state_update":
            return "legacy_state_update", msg.state_update
        try:
            return which, translate(getattr(msg, which), "v2", strict=True)
        except TranslationError as exc:
            log.error(f"[grpc/v1] AgentMessage.{which} nicht nach hannah.v2 übersetzbar: {exc}")
            return None, None


# ------------------------------------------------------------------
# Servicer

def _native_get_devices(servicer, _request, _context):
    return servicer.devices_response_v1()


def _native_control_device(servicer, request, _context):
    ok, message = servicer.control_device_state(
        request.device_id, request.state, request.value,
        request.source_service, request.source_user_id,
    )
    return pb1.StatusResponse(ok=ok, message=message)


def _native_agent_connect(servicer, request_iterator, context):
    yield from servicer.run_agent_stream(request_iterator, context, V1AgentSession())


# Methoden, die für v1 nativ laufen, statt durch die Übersetzung zu gehen
_NATIVE = {
    "GetDevices": _native_get_devices,
    "ControlDevice": _native_control_device,
    "AgentConnect": _native_agent_connect,
}


def _shape(method: MethodDescriptor) -> tuple[bool, bool]:
    return method.client_streaming, method.server_streaming


def _abort_on_translation_error(context, exc: TranslationError):
    log.error(f"[grpc/v1] Request nicht nach hannah.v2 übersetzbar: {exc}")
    context.abort(grpc.StatusCode.INVALID_ARGUMENT, f"not translatable to hannah.v2: {exc}")


def _translated_handler(servicer, v1: MethodDescriptor) -> grpc.RpcMethodHandler:
    behavior = getattr(servicer, v1.name)
    client_streaming, server_streaming = _shape(v1)

    def requests_to_v2(request_iterator, context):
        for message in request_iterator:
            try:
                yield translate(message, "v2", strict=True)
            except TranslationError as exc:
                _abort_on_translation_error(context, exc)

    def request_to_v2(request, context):
        try:
            return translate(request, "v2", strict=True)
        except TranslationError as exc:
            _abort_on_translation_error(context, exc)

    def to_v1(response):
        return translate(response, "v1", strict=False)

    if client_streaming and server_streaming:
        def call(request_iterator, context):
            for response in behavior(requests_to_v2(request_iterator, context), context):
                yield to_v1(response)
        factory = grpc.stream_stream_rpc_method_handler
    elif client_streaming:
        def call(request_iterator, context):
            return to_v1(behavior(requests_to_v2(request_iterator, context), context))
        factory = grpc.stream_unary_rpc_method_handler
    elif server_streaming:
        def call(request, context):
            for response in behavior(request_to_v2(request, context), context):
                yield to_v1(response)
        factory = grpc.unary_stream_rpc_method_handler
    else:
        def call(request, context):
            return to_v1(behavior(request_to_v2(request, context), context))
        factory = grpc.unary_unary_rpc_method_handler

    return factory(
        call,
        request_deserializer=GetMessageClass(v1.input_type).FromString,
        response_serializer=GetMessageClass(v1.output_type).SerializeToString,
    )


def _native_handler(servicer, v1: MethodDescriptor, native) -> grpc.RpcMethodHandler:
    client_streaming, server_streaming = _shape(v1)
    if client_streaming and server_streaming:
        factory = grpc.stream_stream_rpc_method_handler
    elif client_streaming:
        factory = grpc.stream_unary_rpc_method_handler
    elif server_streaming:
        factory = grpc.unary_stream_rpc_method_handler
    else:
        factory = grpc.unary_unary_rpc_method_handler
    return factory(
        lambda request, context: native(servicer, request, context),
        request_deserializer=GetMessageClass(v1.input_type).FromString,
        response_serializer=GetMessageClass(v1.output_type).SerializeToString,
    )


def build_v1_method_handlers(servicer) -> dict[str, grpc.RpcMethodHandler]:
    """Methodenname -> Handler für jede Methode des N−1-Service, die es in v2 gleichförmig gibt."""
    handlers = {}
    for v1 in V1_SERVICE.methods:
        v2 = V2_SERVICE.methods_by_name.get(v1.name)
        if v2 is None or _shape(v2) != _shape(v1):
            # Nach der N−1-Regel sollte das nie passieren. Falls doch: nicht raten,
            # die Methode bleibt UNIMPLEMENTED.
            log.error(
                f"[grpc/v1] {v1.full_name} hat kein gleichförmiges Gegenstück in "
                f"{V2_SERVICE.full_name} — wird für N−1-Clients nicht bedient"
            )
            continue
        native = _NATIVE.get(v1.name)
        handlers[v1.name] = (
            _native_handler(servicer, v1, native) if native else _translated_handler(servicer, v1)
        )
    return handlers


def add_v1_servicer_to_server(servicer, server: grpc.Server) -> None:
    """Registriert die Handler zusätzlich unter `/hannah.v1.HannahService/...` (N−1)."""
    handlers = build_v1_method_handlers(servicer)
    server.add_generic_rpc_handlers(
        (grpc.method_handlers_generic_handler(V1_SERVICE.full_name, handlers),)
    )
    server.add_registered_method_handlers(V1_SERVICE.full_name, handlers)
