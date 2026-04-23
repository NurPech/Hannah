import logging
import threading
from typing import Callable, Optional

log = logging.getLogger(__name__)


def _parse(raw: str):
    s = raw.strip()
    if s.lower() == "true":
        return True
    if s.lower() == "false":
        return False
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


class ResidentsClient:
    """
    Verbindet Hannah mit dem ioBroker Residents-Adapter.

    Lesen  : residents/0/roomie/#       (ioBroker → Hannah, via MQTT-Subscription)
    Schreiben: hannah/set/residents/0/roomie/{name}/{key}  (Hannah → ioBroker)

    Hannah pflegt ihren eigenen Status (hannah_roomie) beim Start/Stop.
    Auf Änderungen des user_roomie (z.B. "leonie") wird mit Callbacks reagiert.
    """

    def __init__(self, cfg: dict, publish_fn: Callable[[str, str], None]):
        self._publish = publish_fn
        self._lock = threading.Lock()
        self._cache: dict[str, dict[str, object]] = {}

        self.topic_prefix_read  = cfg.get("topic_prefix_read",  "residents/0/roomie")
        self.topic_prefix_write = cfg.get("topic_prefix_write", "hannah/set/residents/0/roomie")

        self.hannah_name = cfg.get("hannah_roomie", "hannah")

        # user_roomies akzeptiert Liste oder einzelnen String (Rückwärtskompatibilität)
        raw = cfg.get("user_roomies", cfg.get("user_roomie", []))
        self.user_names: set[str] = {raw} if isinstance(raw, str) else set(raw)

        # Datenpunkt-Suffix unterhalb von /{name}/ — z.B. "mood/state"
        self._state_key = cfg.get("state_key", "mood/state")

        # Welche state-Werte bedeuten "zuhause" / "weg"?
        # Residents-Adapter: 0=Abwesend, 1=zu Hause, 2=Nacht
        self._state_home = cfg.get("state_home", 1)
        self._state_away = cfg.get("state_away", 0)

        # Callbacks: fn(roomie_name: str)
        self._on_arrival:   Optional[Callable[[str], None]] = None
        self._on_departure: Optional[Callable[[str], None]] = None

    # ------------------------------------------------------------------
    # Callbacks registrieren

    def on_arrival(self, fn: Callable[[str], None]):
        """Wird aufgerufen wenn ein user_roomie von weg → zuhause wechselt."""
        self._on_arrival = fn

    def on_departure(self, fn: Callable[[str], None]):
        """Wird aufgerufen wenn ein user_roomie von zuhause → weg wechselt."""
        self._on_departure = fn

    # ------------------------------------------------------------------
    # MQTT-Update empfangen (von mqtt_handler weitergeleitet)

    def update(self, topic: str, raw_value: str):
        """Verarbeitet ein eingehendes residents-Topic."""
        suffix = topic[len(self.topic_prefix_read):].strip("/")
        parts = suffix.split("/", 1)
        if len(parts) != 2:
            return
        roomie, key = parts
        value = _parse(raw_value)

        with self._lock:
            old = self._cache.get(roomie, {}).get(key)
            self._cache.setdefault(roomie, {})[key] = value

        log.debug(f"Residents: {roomie}/{key} = {value!r}")

        # Auf Zustandsänderungen der konfigurierten Nutzer reagieren
        if key == self._state_key and roomie in self.user_names and old != value:
            was_home = (old == self._state_home)
            is_home  = (value == self._state_home)
            if is_home and not was_home and old is not None:
                log.info(f"Residents: {roomie} ist heimgekommen.")
                if self._on_arrival:
                    threading.Thread(
                        target=self._on_arrival, args=(roomie,), daemon=True
                    ).start()
            elif not is_home and was_home:
                log.info(f"Residents: {roomie} hat das Haus verlassen.")
                if self._on_departure:
                    threading.Thread(
                        target=self._on_departure, args=(roomie,), daemon=True
                    ).start()

    # ------------------------------------------------------------------
    # State setzen (Hannah → ioBroker)

    def set_presence(self, roomie: str, state_value: object):
        """Setzt den Anwesenheits-Status eines Roomies via MQTT."""
        topic = f"{self.topic_prefix_write}/{roomie}/{self._state_key}"
        self._publish(topic, str(state_value))
        log.info(f"Residents: {roomie}/{self._state_key} → {state_value!r}")

    def set_user_home(self, roomie: str):
        self.set_presence(roomie, self._state_home)

    def set_user_away(self, roomie: str):
        self.set_presence(roomie, self._state_away)

    def announce_online(self):
        """Setzt Hannahs eigenen Status auf 'home' (beim Start)."""
        self.set_presence(self.hannah_name, self._state_home)

    def announce_offline(self):
        """Setzt Hannahs eigenen Status auf 'away' (beim Stop)."""
        self.set_presence(self.hannah_name, self._state_away)

    # ------------------------------------------------------------------
    # Cache lesen

    def get(self, roomie: str, key: str = "state") -> Optional[object]:
        with self._lock:
            return self._cache.get(roomie, {}).get(key)

    def is_home(self, roomie: Optional[str] = None) -> bool:
        if roomie:
            return self.get(roomie) == self._state_home
        return any(self.get(r) == self._state_home for r in self.user_names)
