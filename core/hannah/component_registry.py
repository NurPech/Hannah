"""
Registry der aktuell mit Hannah verbundenen Komponenten (#339, #335).

Eine Komponente ist über (kind, name) eindeutig, z. B. ("channel", "telegram"). Pro
Schlüssel gibt es höchstens einen Eintrag — eine neue Anmeldung verdrängt die alte,
damit sich eine Komponente nach einem Netzwerk-Hänger nicht selbst aussperrt, solange
Hannah den alten Stream noch nicht als tot erkannt hat.

Die Registry ist generisch: Was ein Handle ist (z. B. die Stream-Subscription eines
Channel-Adapters), weiß sie nicht. Das Beenden eines verdrängten Handles ist Sache
des Aufrufers.

Beobachter (subscribe) werden bei jedem Ein- und Austragen benachrichtigt. Snapshot
und Anmeldung des Beobachters passieren atomar, damit zwischen beidem keine Änderung
verloren geht oder doppelt ankommt.
"""
import logging
import threading
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

KIND_CHANNEL = "channel"
KIND_LOG_COLLECTOR = "log_collector"

EVENT_REGISTERED = "registered"
EVENT_UNREGISTERED = "unregistered"

# (event, kind, name, handle)
Listener = Callable[[str, str, str, Any], None]


class ComponentRegistry:
    def __init__(self):
        self._entries: dict[tuple[str, str], Any] = {}
        self._listeners: list[Listener] = []
        self._lock = threading.Lock()

    def register(self, kind: str, name: str, handle: Any) -> Optional[Any]:
        """Trägt handle unter (kind, name) ein. Gibt den verdrängten Handle zurück,
        falls unter dem Schlüssel schon ein anderer eingetragen war."""
        with self._lock:
            old = self._entries.get((kind, name))
            self._entries[(kind, name)] = handle
            self._notify(EVENT_REGISTERED, kind, name, handle)
        return old if old is not handle else None

    def unregister(self, kind: str, name: str, handle: Any) -> bool:
        """Trägt (kind, name) aus — aber nur, wenn dort noch genau dieser Handle steht.
        Ein bereits verdrängter Handle entfernt so nicht versehentlich seinen Nachfolger."""
        with self._lock:
            if self._entries.get((kind, name)) is not handle:
                return False
            del self._entries[(kind, name)]
            self._notify(EVENT_UNREGISTERED, kind, name, handle)
            return True

    def get(self, kind: str, name: str) -> Optional[Any]:
        with self._lock:
            return self._entries.get((kind, name))

    def entries(self, kind: str) -> list[Any]:
        """Alle aktuell eingetragenen Handles einer Art."""
        with self._lock:
            return [h for (k, _), h in self._entries.items() if k == kind]

    def subscribe(self, listener: Listener) -> list[tuple[str, str, Any]]:
        """Meldet listener an und gibt den aktuellen Stand als [(kind, name, handle)] zurück.

        listener wird unter dem Registry-Lock aufgerufen: Er darf nicht blockieren und
        nicht zurück in die Registry rufen (z. B. nur in eine Queue schreiben)."""
        with self._lock:
            self._listeners.append(listener)
            return [(k, n, h) for (k, n), h in self._entries.items()]

    def unsubscribe(self, listener: Listener) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def _notify(self, event: str, kind: str, name: str, handle: Any) -> None:
        # A failing listener must never break the registration itself
        for listener in list(self._listeners):
            try:
                listener(event, kind, name, handle)
            except Exception:
                log.exception(f"[registry] Listener fehlgeschlagen ({event} {kind}/{name})")
