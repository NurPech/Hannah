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

import grpc
from hannah_proto import PROTO_VERSION

log = logging.getLogger(__name__)

PROTO_VERSION_METADATA_KEY = "x-proto-version"

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


class OutdatedComponentInterceptor(grpc.ServerInterceptor):
    """Meldet jeden RPC über den eingefrorenen N−1-Pfad an einen OutdatedComponentNotifier
    (#358) — unabhängig von Registrierungsnachrichten einzelner Komponenten, da
    hannah-grpc-lib jedem Call bereits x-proto-version mitgibt und der Pfad selbst schon
    verrät, ob N oder N−1 gerufen wurde. Läuft dieselbe Methode über den aktuellen Pfad,
    gilt ein zuvor gesetzter Hinweis als erledigt (Entwarnung)."""

    def __init__(self, notifier, legacy_prefix: str):
        self._notifier = notifier
        self._legacy_prefix = legacy_prefix

    def intercept_service(self, continuation, handler_call_details):
        handler = continuation(handler_call_details)
        if handler is None:
            return handler

        method = handler_call_details.method
        bare_name = method.rsplit("/", 1)[-1]
        if method.startswith(self._legacy_prefix):
            metadata = dict(handler_call_details.invocation_metadata or ())
            proto_version = metadata.get(PROTO_VERSION_METADATA_KEY, "") or ""
            self._notifier.notify_legacy_call(bare_name, proto_version)
        else:
            self._notifier.notify_current_call(bare_name)
        return handler
