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

Die Art "component" (#398) hält *alle* Komponenten, die gerade mit Core sprechen, egal wie
der Connect zustande kam. Ihren Stand führt der ComponentTracker (unten) anhand der Calls
und Streams, die der OutdatedComponentInterceptor meldet. "channel" und "log_collector" sind
keine Komponenten, sondern Rollen: Eine Komponente (Telegram) kann ein Channel sein, ein
Log-Collector ist eine andere. Die Rolle hängt über die Instanz-ID (x-component-id) an der
Komponente, siehe ComponentTracker.snapshot().
"""
import logging
import threading
import time
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

KIND_CHANNEL = "channel"
KIND_LOG_COLLECTOR = "log_collector"
KIND_COMPONENT = "component"

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

    def items(self) -> list[tuple[str, str, Any]]:
        """Alle Einträge aller Arten als [(kind, name, handle)]."""
        with self._lock:
            return [(k, n, h) for (k, n), h in self._entries.items()]

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


# ------------------------------------------------------------------
# Komponenten (#398)

# Eine Komponente gilt als da, solange ein Stream offen ist oder ihr letzter Call (Heartbeat
# eingeschlossen) jünger ist als das. hannah-grpc-lib schickt alle 30 s einen Heartbeat, drei
# Intervalle gelten als weg: Wert und Intervall der Lib gehören zusammen.
PRESENCE_WINDOW_S = 90.0

DEFAULT_MAX_COMPONENTS = 256

# Wie oft höchstens nach Abgelaufenen gesucht wird (die Suche läuft bei Calls, ohne eigenen Thread).
_EXPIRY_CHECK_INTERVAL_S = 1.0


class ComponentEntry:
    """Eine laufende Instanz einer Komponente: Komponente, Version, Instanz-ID, erste und letzte
    Sichtung, ob der letzte Call über den eingefrorenen N−1-Pfad kam, und wie viele Streams
    gerade offen sind. `permanent` gilt für Core selbst, der keinen Stream zu sich hat."""

    def __init__(self, component: str, version: str, instance_id: str, now_epoch: float, now_mono: float,
                 legacy: bool = False, permanent: bool = False):
        self.component = component
        self.version = version
        self.instance_id = instance_id
        self.first_seen = now_epoch
        self.last_seen = now_epoch
        self.legacy = legacy
        self.permanent = permanent
        self.open_streams = 0
        self._last_seen_mono = now_mono

    @property
    def key(self) -> str:
        return _entry_name(self.component, self.instance_id)


def _entry_name(component: str, instance_id: str) -> str:
    return f"{component}/{instance_id}"


class ComponentTracker:
    """Führt die Art "component" der Registry: trägt jeden Anrufer mit x-component ein, zählt
    seine offenen Streams und trägt ihn aus, wenn weder ein Stream noch ein Call jünger als
    `window` da ist.

    Die Instanz-ID ist pro Prozess zufällig und ändert sich mit jedem Neustart: Sie
    unterscheidet laufende Instanzen (zwei Proxies), ist aber nie ein persistierter Schlüssel.
    Der Stand liegt nur im Speicher und baut sich nach einem Core-Neustart aus den Calls und
    Heartbeats neu auf.

    Aufgeräumt wird ohne eigenen Thread, beim Zugriff und bei jedem Call (höchstens einmal pro
    Sekunde). Ein Abgang wird deshalb erst dann gemeldet, wenn die nächste Anfrage oder der
    nächste Call kommt. Die Liste ist begrenzt: Die Werte kommen von außen, zu viele verdrängen
    die am längsten stillen Instanzen ohne offenen Stream."""

    def __init__(self, registry: ComponentRegistry, window: float = PRESENCE_WINDOW_S,
                 max_components: int = DEFAULT_MAX_COMPONENTS,
                 clock: Callable[[], float] = time.monotonic, wall_clock: Callable[[], float] = time.time):
        self._registry = registry
        self._window = window
        self._max = max_components
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._entries: dict[str, ComponentEntry] = {}
        self._next_expiry_check = 0.0

    def seen(self, caller, legacy: bool = False) -> ComponentEntry:
        """Ein Call von `caller` (hannah.grpc_interceptors.CallerIdentity): trägt die Instanz ein,
        falls neu, und merkt sich Version, Pfad und den Zeitpunkt."""
        now = self._clock()
        with self._lock:
            self._expire_locked(now, throttle=True)
            name = _entry_name(caller.component, caller.instance_id)
            entry = self._entries.get(name)
            if entry is None:
                entry = ComponentEntry(caller.component, caller.version, caller.instance_id,
                                       self._wall_clock(), now, legacy=legacy)
                self._entries[name] = entry
                self._make_room_locked(keep=entry)
                self._registry.register(KIND_COMPONENT, name, entry)
                self._log_seen(entry)
            else:
                version_changed = entry.version != caller.version
                entry.version = caller.version
                entry.legacy = legacy
                entry.last_seen = self._wall_clock()
                entry._last_seen_mono = now
                if version_changed:
                    self._log_seen(entry)
            return entry

    def stream_opened(self, entry: ComponentEntry) -> None:
        with self._lock:
            entry.open_streams += 1

    def stream_closed(self, entry: ComponentEntry) -> None:
        """Ein Stream der Instanz ist zu Ende. War es der letzte und ist der letzte Call schon
        länger her als das Fenster, ist die Komponente sofort weg."""
        now = self._clock()
        with self._lock:
            entry.open_streams = max(0, entry.open_streams - 1)
            self._expire_locked(now, throttle=False)

    def register_permanent(self, component: str, version: str, instance_id: str) -> ComponentEntry:
        """Trägt eine Komponente ein, die nie ausläuft (Core selbst: er hat keinen Stream zu sich)."""
        now = self._clock()
        with self._lock:
            entry = ComponentEntry(component, version, instance_id, self._wall_clock(), now, permanent=True)
            self._entries[entry.key] = entry
            self._registry.register(KIND_COMPONENT, entry.key, entry)
            return entry

    def get(self, component: str, instance_id: str) -> Optional[ComponentEntry]:
        with self._lock:
            self._expire_locked(self._clock(), throttle=False)
            return self._entries.get(_entry_name(component, instance_id))

    def snapshot(self) -> list[dict]:
        """Alle Instanzen, die gerade da sind, die zuletzt gesehenen zuerst. `roles` nennt die
        Rollen, die an der Instanz hängen (Art und Name, z. B. channel/telegram)."""
        with self._lock:
            self._expire_locked(self._clock(), throttle=False)
            entries = sorted(self._entries.values(), key=lambda e: e._last_seen_mono, reverse=True)
            roles: dict[tuple[str, str], list[dict]] = {}
            for kind, name, handle in self._registry.items():
                caller = getattr(handle, "caller", None)
                if kind != KIND_COMPONENT and caller is not None:
                    roles.setdefault((caller.component, caller.instance_id), []).append({"kind": kind, "name": name})
            return [
                {
                    "component": e.component, "version": e.version, "instance_id": e.instance_id,
                    "first_seen": e.first_seen, "last_seen": e.last_seen, "legacy": e.legacy,
                    "open_streams": e.open_streams, "roles": roles.get((e.component, e.instance_id), []),
                }
                for e in entries
            ]

    def _expire_locked(self, now: float, throttle: bool) -> None:
        if throttle and now < self._next_expiry_check:
            return
        self._next_expiry_check = now + _EXPIRY_CHECK_INTERVAL_S
        gone = [
            e for e in self._entries.values()
            if not e.permanent and e.open_streams == 0 and now - e._last_seen_mono > self._window
        ]
        for entry in gone:
            self._remove_locked(entry)
            log.info(
                f"[components] {entry.component} {entry.version or '(ohne Version)'} "
                f"(Instanz {entry.instance_id[:8] or '?'}) ist nicht mehr da"
            )

    def _make_room_locked(self, keep: ComponentEntry) -> None:
        while len(self._entries) > self._max:
            candidates = [e for e in self._entries.values() if e is not keep and not e.permanent and e.open_streams == 0]
            if not candidates:
                return
            self._remove_locked(min(candidates, key=lambda e: e._last_seen_mono))

    def _remove_locked(self, entry: ComponentEntry) -> None:
        if self._entries.get(entry.key) is entry:
            del self._entries[entry.key]
        self._registry.unregister(KIND_COMPONENT, entry.key, entry)

    @staticmethod
    def _log_seen(entry: ComponentEntry) -> None:
        log.info(
            f"[components] {entry.component} {entry.version or '(ohne Version)'} "
            f"(Instanz {entry.instance_id[:8] or '?'}) spricht mit Core"
            f"{' über den eingefrorenen alten Pfad' if entry.legacy else ''}"
        )
