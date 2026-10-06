"""
Syslog-Ziel der Satelliten per MQTT verteilen (#417).

Die Satelliten schicken ihre Logs per UDP-Syslog und holen sich das Ziel aus dem retained
Topic `hannah/syslog` (JSON {"host": ..., "port": ...}), statt es im Web-UI einzutragen. Core
kennt den Log-Collector aus der eigenen Component-Registry (er meldet sich per
LogCollectorConnect an, seit hannah-proto 5.4 mit dem Port seines Syslog-Empfängers) und
veröffentlicht dessen Adresse. Fehlt der Collector, gilt der Fallback aus den Settings
(Kategorie "syslog": fallback_host, fallback_port), zum Beispiel direkt der Alloy-Stack, damit
die Logs trotzdem in Loki landen. Ist auch der nicht gesetzt, geht {"host": "", "port": 0}
raus, die Satelliten senden dann nichts. Bewusst kein leeres Payload: Das löscht die retained
Message, was verbundene Satelliten nie erfahren.

Ein Collector, der geht, wird erst nach DEBOUNCE_S durch den Fallback ersetzt: Ein kurzer
Reconnect (Netz-Hänger, Neustart des Collectors) soll nicht alle Satelliten zweimal umschalten.
Dasselbe beim Start von Core, wo der Collector sich gleich meldet. Ein (wieder) angemeldeter
Collector gilt sofort.
"""
import json
import logging
import threading
from typing import Any, Callable, Optional

from hannah.component_registry import (
    ComponentRegistry, EVENT_REGISTERED, EVENT_UNREGISTERED, KIND_LOG_COLLECTOR,
)

log = logging.getLogger(__name__)

TOPIC = "hannah/syslog"
DEBOUNCE_S = 10.0

# (host, port)
Target = tuple[str, int]


def payload(target: Optional[Target]) -> str:
    host, port = target if target else ("", 0)
    return json.dumps({"host": host, "port": port})


class SyslogTarget:
    """Hält `hannah/syslog` auf dem aktuellen Ziel. `publish` bekommt den JSON-Text."""

    def __init__(
        self,
        registry: ComponentRegistry,
        get_settings: Callable[[], dict],
        publish: Callable[[str], None],
        debounce_s: float = DEBOUNCE_S,
    ):
        self._registry = registry
        self._get_settings = get_settings
        self._publish = publish
        self._debounce_s = debounce_s

        self._lock = threading.Lock()
        self._collectors: dict[str, Target] = {}  # instance -> (host, syslog_port), only those with a receiver
        self._timer: Optional[threading.Timer] = None
        self._last: Optional[str] = None  # what was published last
        self._from_collector = False      # whether that was a collector's address

    def start(self) -> None:
        """Meldet sich an der Registry an, vor dem Start des gRPC-Servers aufrufen (wie
        log_shipping.follow_registry), damit sich kein Collector unbemerkt an- oder abmeldet."""
        for kind, name, handle in self._registry.subscribe(self._on_registry):
            if kind == KIND_LOG_COLLECTOR:
                self._track(name, handle)
        self._evaluate()

    def stop(self) -> None:
        with self._lock:
            self._cancel_timer()
        self._registry.unsubscribe(self._on_registry)

    def refresh(self) -> None:
        """Nach einer Änderung der Fallback-Settings."""
        self._evaluate()

    # ------------------------------------------------------------------

    def _on_registry(self, event: str, kind: str, name: str, handle: Any) -> None:
        # Läuft unter dem Registry-Lock: nur eigenen Zustand ändern und MQTT anstoßen, nicht zurück in die Registry
        if kind != KIND_LOG_COLLECTOR:
            return
        if event == EVENT_REGISTERED:
            self._track(name, handle)
        elif event == EVENT_UNREGISTERED:
            with self._lock:
                self._collectors.pop(name, None)
        self._evaluate()

    def _track(self, name: str, handle: Any) -> None:
        with self._lock:
            syslog_port = getattr(handle, "syslog_port", 0)
            if syslog_port > 0:
                self._collectors[name] = (handle.host, syslog_port)
            else:
                self._collectors.pop(name, None)  # re-registered without a receiver

    def _collector_target(self) -> Optional[Target]:
        # Mehrere Collector sind unüblich — deterministisch einen wählen.
        return self._collectors[min(self._collectors)] if self._collectors else None

    def _fallback_target(self) -> Optional[Target]:
        try:
            settings = self._get_settings()
            host = str(settings.get("fallback_host") or "").strip()
            port = int(settings.get("fallback_port") or 0)
        except Exception:
            log.exception("[syslog] Fallback-Settings nicht lesbar")
            return None
        return (host, port) if host and 0 < port < 65536 else None

    def _evaluate(self) -> None:
        with self._lock:
            collector = self._collector_target()
            if collector is not None:
                self._cancel_timer()
                self._send(collector, from_collector=True)
                return
            if self._from_collector or self._last is None:
                # Der Collector ist weg oder Core gerade gestartet: erst abwarten, ob er wiederkommt
                if self._timer is None:
                    self._timer = threading.Timer(self._debounce_s, self._fallback_due)
                    self._timer.daemon = True
                    self._timer.start()
                return
            # Schon auf dem Fallback: Änderungen daran gelten sofort
            self._send(self._fallback_target(), from_collector=False)

    def _fallback_due(self) -> None:
        with self._lock:
            self._timer = None
            collector = self._collector_target()
            if collector is not None:
                self._send(collector, from_collector=True)
            else:
                self._send(self._fallback_target(), from_collector=False)

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _send(self, target: Optional[Target], from_collector: bool) -> None:
        text = payload(target)
        self._from_collector = from_collector
        if text == self._last:
            return
        self._last = text
        try:
            self._publish(text)
        except Exception:
            log.exception("[syslog] Syslog-Ziel konnte nicht veröffentlicht werden")
            self._last = None  # beim nächsten Anlass noch einmal versuchen
