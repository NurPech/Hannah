"""
Hannah Car Tracker

Abonniert VW-Connect MQTT-Topics via MQTTHandler und cached den Auto-Status.
Feuert einen on_parked-Callback wenn das Auto von fahrend → geparkt wechselt.

Wird von main.py per set_car_handler() in den MQTTHandler eingehängt —
genau wie WeatherCache und ResidentsClient.
"""
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

log = logging.getLogger(__name__)

_DOOR_LABELS: dict[str, str] = {
    "bonnet":      "Motorraum",
    "frontLeft":   "Fahrerseite",
    "frontRight":  "Beifahrerseite",
    "rearLeft":    "Tür hinten links",
    "rearRight":   "Tür hinten rechts",
    "trunk":       "Kofferraum",
}
_WINDOW_LABELS: dict[str, str] = {
    "frontLeft":  "Fenster Fahrerseite",
    "frontRight": "Fenster Beifahrerseite",
    "rearLeft":   "Fenster hinten links",
    "rearRight":  "Fenster hinten rechts",
}


@dataclass
class CarState:
    # position
    latitude:      Optional[float] = None
    longitude:     Optional[float] = None
    address:       str = ""
    is_moving:     Optional[bool] = None
    position_date: Optional[int] = None   # Unix-Timestamp (Sekunden)

    # status
    odometer:        Optional[int] = None
    total_range:     Optional[int] = None
    is_car_locked:   Optional[bool] = None
    door_lock_status: str = ""              # "locked" / "unlocked"
    overall_status:  str = ""

    # Türen / Fenster: Name → True (geschlossen) / False (offen)
    doors:   dict[str, bool] = field(default_factory=dict)
    windows: dict[str, bool] = field(default_factory=dict)

    # Metadaten (aus ioBroker via MQTT)
    display_name: str = ""   # z.B. "Leonies Golf"
    plate:        str = ""   # Nummernschild, z.B. "BKS-XX 123"
    vin:          str = ""   # Fahrzeug-Identifikationsnummer (optional)

    # Besitzer: erster Eintrag aus owner_roomies (für gRPC-Compat)
    owner_roomie: str = ""

    @property
    def available(self) -> bool:
        """True sobald mindestens ein Wert empfangen wurde."""
        return self.latitude is not None or bool(self.address) or self.odometer is not None

    def _security_problems(self) -> list[str]:
        problems: list[str] = []
        if self.door_lock_status == "unlocked" or self.is_car_locked is False:
            problems.append("Auto ist nicht abgeschlossen")
        for name, closed in self.doors.items():
            if not closed:
                problems.append(_DOOR_LABELS.get(name, name) + " geöffnet")
        for name, closed in self.windows.items():
            if not closed:
                problems.append(_WINDOW_LABELS.get(name, name) + " geöffnet")
        return problems

    # ------------------------------------------------------------------
    # Sprachausgabe (kurz, für TTS-Antwort)

    def build_voice_answer(self, scope: str = "all", label: str = "") -> str:
        """
        Kurze Antwort für Sprach-Interface.
        label: optionaler Identifikator (z.B. Kennzeichen oder Displayname) wenn
               mehrere Autos im Haushalt vorhanden sind.
        """
        if not self.available:
            ref = f"dein {label}" if label else "das Auto"
            return f"Ich habe noch keine Daten für {ref} empfangen."

        if scope == "location":
            return self._voice_location(label)
        if scope == "security":
            return self._voice_security(label)
        if scope == "range":
            ref = f"dein {label}" if label else "das Auto"
            if self.total_range is not None:
                return f"Die Restreichweite von {ref} beträgt {self.total_range} Kilometer."
            return f"Ich kenne die Reichweite von {ref} gerade nicht."
        if scope == "odometer":
            ref = f"dein {label}" if label else "das Auto"
            if self.odometer is not None:
                return f"{ref.capitalize()} hat {self.odometer} Kilometer."
            return f"Ich kenne den Kilometerstand von {ref} gerade nicht."

        # "all" — Ort + Sicherheitsstatus
        return self._voice_location(label) + " " + self._voice_security(label)

    def _voice_location(self, label: str = "") -> str:
        ref = f"dein {label}" if label else "das Auto"
        if self.is_moving:
            return f"{ref.capitalize()} ist gerade unterwegs, zuletzt an: {self.address}."
        if self.address:
            return f"{ref.capitalize()} steht an: {self.address}."
        return f"Ich weiß nicht wo {ref} steht."

    def _voice_security(self, label: str = "") -> str:
        ref = f"dein {label}" if label else "das Auto"
        problems = self._security_problems()
        if not problems:
            return f"Alle Türen und Fenster von {ref} sind geschlossen und es ist abgeschlossen."
        return "Achtung: " + ", ".join(problems) + "."

    # ------------------------------------------------------------------
    # Telegram-Nachricht (ausführlich)

    def build_message(self, home_address: str = "") -> str:
        """Ausführliche Nachricht für Telegram."""
        parts: list[str] = []

        if self.is_moving:
            parts.append(f"🚗 Auto fährt, letzte bekannte Adresse:\n{self.address}")
        elif home_address and self.address and self.address.startswith(home_address[:20]):
            parts.append(f"🏠 Das Auto steht zu Hause.\n{self.address}")
        elif self.address:
            parts.append(f"📍 Das Auto steht an:\n{self.address}")

        if self.latitude is not None and self.longitude is not None:
            parts.append(f"🗺 https://maps.google.com/?q={self.latitude},{self.longitude}")

        problems = self._security_problems()
        if problems:
            parts.append("⚠️ Sicherheitsproblem:\n" + "\n".join(f"- {p}" for p in problems))
        else:
            parts.append("✅ Alle Fenster geschlossen, Auto abgeschlossen.")

        if self.odometer is not None:
            parts.append(f"🔢 Kilometerstand: {self.odometer} km")
        if self.total_range is not None:
            parts.append(f"⛽ Restreichweite: {self.total_range} km")
        if self.position_date:
            ts = self.position_date / 1000 if self.position_date > 1e10 else self.position_date
            dt = datetime.fromtimestamp(ts)
            parts.append(f"🕐 Letztes Update: {dt.strftime('%d.%m.%y %H:%M:%S')}")

        return "\n\n".join(parts)


class CarTracker:
    """
    Empfängt Auto-Status-Updates aus MQTT und cached den aktuellen Zustand.
    on_parked wird aufgerufen wenn das Auto von fahrend → geparkt wechselt.

    Integration: mqtt_handler.set_car_handler(car_tracker.topic_prefix, car_tracker.update)
    """

    def __init__(self, cfg: dict):
        self.topic_prefix = cfg.get(
            "topic_prefix", "javascript/0/virtualDevice/Auto/Leonie/Auto1"
        )
        self._home_address = cfg.get("home_address", "")
        # owner_roomies: Liste von Roomie-IDs mit Zugriff auf dieses Auto
        # Backward-compat: alter Key "owner_roomie" (str) wird als Liste übernommen
        raw = cfg.get("owner_roomies", cfg.get("owner_roomie", ""))
        self.owner_roomies: list[str] = raw if isinstance(raw, list) else ([raw] if raw else [])
        self._state = CarState(owner_roomie=self.owner_roomies[0] if self.owner_roomies else "")
        self._prev_moving: Optional[bool] = None
        self._lock = threading.Lock()
        self._on_parked: Optional[Callable[["CarState"], None]] = None

    def on_parked(self, fn: Callable[["CarState"], None]):
        """Registriert einen Callback der beim Einparken aufgerufen wird."""
        self._on_parked = fn

    @property
    def home_address(self) -> str:
        return self._home_address

    @property
    def state(self) -> CarState:
        with self._lock:
            return self._state

    @property
    def home_address(self) -> str:
        return self._home_address

    # ------------------------------------------------------------------
    # MQTT-Update (von mqtt_handler weitergeleitet)

    def update(self, topic: str, raw_value: str):
        """Verarbeitet ein eingehendes Auto-Topic."""
        relative = topic[len(self.topic_prefix):].lstrip("/")
        with self._lock:
            self._apply(relative, raw_value.strip())

    def _apply(self, key: str, value: str):
        s = self._state
        match key:
            case "position/latitude":
                s.latitude = _float(value)
            case "position/longitude":
                s.longitude = _float(value)
            case "position/addressDisplayName":
                s.address = value
            case "position/isMoving":
                new_moving = _bool(value)
                if self._prev_moving is True and new_moving is False:
                    log.info("Auto eingeparkt — on_parked-Callback wird aufgerufen")
                    s.is_moving = new_moving  # Snapshot mit aktuellem Wert
                    snapshot = CarState(**{
                        k: (dict(v) if isinstance(v, dict) else v)
                        for k, v in s.__dict__.items()
                    })
                    if self._on_parked:
                        threading.Thread(
                            target=self._on_parked,
                            args=(snapshot,),
                            daemon=True,
                        ).start()
                self._prev_moving = new_moving
                s.is_moving = new_moving
            case "position/date":
                s.position_date = _int(value)
            case "status/odometer":
                s.odometer = _int(value)
            case "status/totalRange":
                s.total_range = _int(value)
            case "status/isCarLocked":
                s.is_car_locked = _bool(value)
            case "status/doorLockStatus":
                s.door_lock_status = value
            case "status/overallStatus":
                s.overall_status = value
            case _ if key.startswith("status/doors/closed/"):
                door = key.removeprefix("status/doors/closed/")
                s.doors[door] = _bool(value) is not False
            case _ if key.startswith("status/windows/closed/"):
                win = key.removeprefix("status/windows/closed/")
                s.windows[win] = _bool(value) is not False
            case "info/displayName":
                s.display_name = value
            case "info/plate":
                s.plate = value
            case "info/vin":
                s.vin = value
            case _:
                log.debug(f"CarTracker: unbekannter Key '{key}' = {value!r}")


class CarManager:
    """
    Verwaltet mehrere CarTracker-Instanzen (ein Haushalt, mehrere Autos).
    Filtert nach owner_roomies wenn ein Sprecher bekannt ist.
    """

    def __init__(self, trackers: list[CarTracker]):
        self._trackers = trackers

    def __iter__(self):
        return iter(self._trackers)

    def _cars_for(self, roomie_id: str) -> list[CarTracker]:
        """Gibt alle Tracker zurück, deren owner_roomies die roomie_id enthält."""
        if not roomie_id:
            return self._trackers
        return [t for t in self._trackers if roomie_id in t.owner_roomies] or self._trackers

    def _label(self, tracker: CarTracker) -> str:
        """Kurzer Identifikator für Sprachantworten: Displayname, sonst Kennzeichen."""
        s = tracker.state
        return s.display_name or s.plate or ""

    def answer_for_roomie(self, scope: str, roomie_id: str = "") -> str:
        cars = self._cars_for(roomie_id)
        if not cars:
            return "Ich kenne kein Auto für dich."

        if len(cars) == 1:
            label = self._label(cars[0]) if len(self._trackers) > 1 else ""
            return cars[0].state.build_voice_answer(scope=scope, label=label)

        # Mehrere Autos → jedes mit Label benennen
        parts = [t.state.build_voice_answer(scope=scope, label=self._label(t)) for t in cars]
        return " ".join(parts)

    @property
    def first_tracker(self) -> "CarTracker | None":
        return self._trackers[0] if self._trackers else None

    @property
    def first_state(self):
        """Gibt den State des ersten Trackers zurück (für gRPC-Compat)."""
        return self._trackers[0].state if self._trackers else None


# ── Helpers ────────────────────────────────────────────────────────────────

def _bool(v: str) -> Optional[bool]:
    if v.lower() in ("true", "1", "yes"):
        return True
    if v.lower() in ("false", "0", "no"):
        return False
    return None


def _float(v: str) -> Optional[float]:
    try:
        return float(v)
    except ValueError:
        return None


def _int(v: str) -> Optional[int]:
    try:
        return int(float(v))
    except ValueError:
        return None
