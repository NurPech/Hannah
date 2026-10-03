"""
Protocol-Version-Diagnose-Interceptor (#60, #359).

Die externen Hannah-Clients schicken bei jedem RPC die Metadata
`x-proto-version` mit. Bis #359 wurde sie exakt gegen Hannahs eigene
PROTO_VERSION geprüft und bei `enforce_protocol_version: true` abgelehnt.
Seit Core `hannah.v2` (N) und `hannah.v1` (N−1) parallel bedient
(hannah-proto#11, #384), regelt der versionierte Methodenpfad die
Kompatibilität — dieser Interceptor lehnt nichts mehr ab, sondern loggt nur
noch zur Diagnose.

Wichtigster Fall: eine Komponente, die älter als N−1 ist. Ihr Pfad existiert
nicht mehr (`handler is None`), gRPC antwortet danach selbst mit
UNIMPLEMENTED. Vorher werden Pfad und Header geloggt, damit sichtbar ist, wer
da anklopft. Jede Kombination (Pfad, Header) wird nur einmal geloggt, sonst
flutet ein Client mit Retry-Schleife das Log.
"""
import logging
import threading
from dataclasses import dataclass

import grpc
from hannah_proto import PROTO_VERSION

log = logging.getLogger(__name__)

PROTO_VERSION_METADATA_KEY = "x-proto-version"
COMPONENT_METADATA_KEY = "x-component"
COMPONENT_VERSION_METADATA_KEY = "x-component-version"
COMPONENT_ID_METADATA_KEY = "x-component-id"

_MAX_IDENTITY_LENGTH = 64

# Obergrenze für die Menge bereits geloggter (Pfad, Header)-Kombinationen —
# beliebige Pfade/Header kommen von außen, die Menge darf nicht unbegrenzt wachsen.
_MAX_LOGGED_KEYS = 1024


def read_proto_version() -> str:
    """hannah_proto.PROTO_VERSION as the string the x-proto-version metadata value is compared to."""
    return str(PROTO_VERSION)


class ProtocolVersionInterceptor(grpc.ServerInterceptor):
    def __init__(self, expected_version: str):
        self._expected_version = expected_version
        self._logged: set[tuple[str, str | None]] = set()
        self._lock = threading.Lock()

    def intercept_service(self, continuation, handler_call_details):
        handler = continuation(handler_call_details)
        method = handler_call_details.method
        metadata = dict(handler_call_details.invocation_metadata or ())
        received = metadata.get(PROTO_VERSION_METADATA_KEY)

        if handler is None:
            if self._first_time(method, received):
                log.warning(
                    f"[grpc/version] Unbekannte Methode {method!r} (x-proto-version={received!r}) — "
                    f"vermutlich eine Komponente älter als N−1, gRPC antwortet mit UNIMPLEMENTED"
                )
            return handler

        if received != self._expected_version and self._first_time(method, received):
            log.info(
                f"[grpc/version] {method!r}: x-proto-version={received!r} "
                f"(Core: {self._expected_version!r}) — nur zur Diagnose, kein Ablehnungsgrund"
            )
        return handler

    def _first_time(self, method: str, received: str | None) -> bool:
        key = (method, received)
        with self._lock:
            if key in self._logged:
                return False
            if len(self._logged) >= _MAX_LOGGED_KEYS:
                self._logged.clear()
            self._logged.add(key)
            return True


@dataclass(frozen=True)
class CallerIdentity:
    """Wer ruft: Name, Version und Instanz-ID, die hannah-grpc-lib (ab Python 0.9.0) bei jedem
    Call mitschickt (#396). Rein informativ, keine Authentifizierung."""
    component: str
    version: str = ""
    instance_id: str = ""


def read_caller(metadata: dict) -> CallerIdentity | None:
    """Die Identität aus den Metadaten eines Calls, None ohne x-component (Clients ohne die
    neue Lib). Die Werte kommen von außen: Länge begrenzt, damit sie weder Log noch Mailbox
    oder DB aufblähen."""
    def value(key: str) -> str:
        return (metadata.get(key) or "").strip()[:_MAX_IDENTITY_LENGTH]

    component = value(COMPONENT_METADATA_KEY)
    if not component:
        return None
    return CallerIdentity(component, value(COMPONENT_VERSION_METADATA_KEY), value(COMPONENT_ID_METADATA_KEY))


class OutdatedComponentInterceptor(grpc.ServerInterceptor):
    """Sieht jeden RPC an Core und gibt ihn weiter:

    - an einen OutdatedComponentNotifier (#358): unabhängig von Registrierungsnachrichten
      einzelner Komponenten, da hannah-grpc-lib jedem Call bereits x-proto-version mitgibt und
      der Pfad selbst schon verrät, ob N oder N−1 gerufen wurde. Läuft dieselbe Komponente über
      den aktuellen Pfad, gilt ein zuvor gesetzter Hinweis als erledigt (Entwarnung).
    - an einen ComponentTracker (#398): Sendet der Client x-component, x-component-version und
      x-component-id (#396), steht er ab dem ersten Call in der Registry, egal ob unär, Stream
      oder Heartbeat. Die Handler von Streams werden dazu eingewickelt: Der Tracker zählt die
      offenen Streams der Instanz, im finally wird abgezogen. Eine Komponente mit mehreren
      Streams (Telegram) fliegt so nicht beim Abriss eines davon raus.

    Alle offenen Streams werden gezählt, auch die ohne Identität: Der synchrone grpc.server()
    belegt pro Stream einen Worker-Thread für die ganze Verbindungsdauer (#229). Ab 80 % des
    Pools (stream_limit) steht eine Warnung im Log, bevor neue Aufrufe unbemerkt warten.

    Beides ist optional (Tests, Core ohne Notifier)."""

    STREAM_WARN_FRACTION = 0.8
    STREAM_WARN_RESET_FRACTION = 0.6

    def __init__(self, notifier=None, legacy_prefix: str = "", tracker=None, stream_limit: int | None = None):
        self._notifier = notifier
        self._legacy_prefix = legacy_prefix
        self._tracker = tracker
        self._stream_limit = stream_limit
        self._streams_lock = threading.Lock()
        self._open_streams = 0
        self._saturated = False

    @property
    def open_streams(self) -> int:
        """Alle gerade offenen Streams (jeder belegt einen Worker-Thread)."""
        with self._streams_lock:
            return self._open_streams

    def intercept_service(self, continuation, handler_call_details):
        handler = continuation(handler_call_details)
        if handler is None:
            return handler

        method = handler_call_details.method
        bare_name = method.rsplit("/", 1)[-1]
        metadata = dict(handler_call_details.invocation_metadata or ())
        caller = read_caller(metadata)
        legacy = method.startswith(self._legacy_prefix) if self._legacy_prefix else False
        if self._notifier is not None:
            if legacy:
                proto_version = metadata.get(PROTO_VERSION_METADATA_KEY, "") or ""
                self._notifier.notify_legacy_call(bare_name, proto_version, caller)
            else:
                self._notifier.notify_current_call(bare_name, caller)
        entry = self._tracker.seen(caller, legacy) if self._tracker is not None and caller is not None else None
        return self._count_streams(handler, entry)

    def _count_streams(self, handler, entry):
        if handler.unary_unary is not None:
            return handler
        behavior = handler.unary_stream or handler.stream_unary or handler.stream_stream
        opened, closed = self._stream_opened, self._stream_closed

        if handler.unary_stream is not None:
            def wrapped(request, context):
                opened(entry)
                try:
                    yield from behavior(request, context)
                finally:
                    closed(entry)
            factory = grpc.unary_stream_rpc_method_handler
        elif handler.stream_unary is not None:
            def wrapped(request_iterator, context):
                opened(entry)
                try:
                    return behavior(request_iterator, context)
                finally:
                    closed(entry)
            factory = grpc.stream_unary_rpc_method_handler
        else:
            def wrapped(request_iterator, context):
                opened(entry)
                try:
                    yield from behavior(request_iterator, context)
                finally:
                    closed(entry)
            factory = grpc.stream_stream_rpc_method_handler
        return factory(
            wrapped,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )

    def _stream_opened(self, entry) -> None:
        if entry is not None:
            self._tracker.stream_opened(entry)
        with self._streams_lock:
            self._open_streams += 1
            count = self._open_streams
            warn = (
                self._stream_limit is not None
                and not self._saturated
                and count >= self._stream_limit * self.STREAM_WARN_FRACTION
            )
            if warn:
                self._saturated = True
        if warn:
            log.warning(
                f"[grpc] {count} Streams offen, der Worker-Pool hat {self._stream_limit} Threads — "
                f"jeder Stream belegt einen für die ganze Verbindungsdauer, neue Aufrufe können warten (#229, #398)"
            )

    def _stream_closed(self, entry) -> None:
        if entry is not None:
            self._tracker.stream_closed(entry)
        with self._streams_lock:
            self._open_streams -= 1
            if self._saturated and self._open_streams <= self._stream_limit * self.STREAM_WARN_RESET_FRACTION:
                self._saturated = False
