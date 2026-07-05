import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Iterable, Optional
from hannah_proto.hannah_pb2 import AgentDevice as AgentDevice
from hannah_proto.hannah_pb2 import AgentStateValue as AgentStateValue

if TYPE_CHECKING:
    from .nlu import Intent

log = logging.getLogger(__name__)


_UMLAUT_MAP = {"ae": "ä", "oe": "ö", "ue": "ü", "Ae": "Ä", "Oe": "Ö", "Ue": "Ü"}

_CATEGORY_LABELS: dict[str, str] = {
    "light":   "Lichter",
    "socket":  "Steckdosen",
    "climate": "Klimageräte",
    "blind":   "Rollläden",
    "sensor":  "Sensoren",
}

def _category_label(cat: Optional[str]) -> str:
    return _CATEGORY_LABELS.get(cat, cat) if cat else "Geräte"

def _iaq_label(value: float) -> str:
    """Übersetzt den BSEC2-IAQ-Index (0–500) in eine Klartext-Bewertung."""
    if value <= 50:
        return "gut"
    if value <= 100:
        return "okay"
    if value <= 150:
        return "leicht belastet"
    return "schlecht"

def _normalize_umlauts(s: str) -> str:
    """Ersetzt ae/oe/ue durch Umlaute: Buero → Büro, Sued → Süd."""
    return re.sub(r"[AaOoUu]e", lambda m: _UMLAUT_MAP.get(m.group(), m.group()), s)

def _camel_to_words(s: str) -> str:
    """
    Konvertiert Geräte-/Raumnamen in einen NLU-Suchbegriff:
      DeckeSeite      → decke seite
      Zimmer_Sued     → zimmer süd
      BueroRene       → büro rene
      Deckenlampe_Spot1 → deckenlampe spot 1
    """
    # Unterstriche → Leerzeichen
    s = s.replace("_", " ")
    # CamelCase aufbrechen
    s = re.sub(r"([A-Z])", r" \1", s)
    # Zahl-Suffix mit Leerzeichen trennen (Spot1 → Spot 1)
    s = re.sub(r"([a-zA-Z])(\d)", r"\1 \2", s)
    # Mehrfache Leerzeichen normalisieren
    s = re.sub(r" +", " ", s).strip()
    s = s.lower()
    # ae/oe/ue → Umlaute
    s = _normalize_umlauts(s)
    return s


@dataclass
class Device:
    id: str                # javascript.0.virtualDevice.Licht.EG.Wohnzimmer.DeckeSeite
    name: str              # DeckeSeite (Originalname)
    key: str               # decke seite (normalisiert für NLU-Matching)
    room: str              # room_id: enum ID segment, z.B. "wohnzimmer" oder "living_room"
    room_display_name: str # Anzeigename für Ansagen, z.B. "Wohnzimmer"
    floor: str             # EG
    category: str          # Licht
    states: dict = field(default_factory=dict)         # canon-key → state_id
    current: dict = field(default_factory=dict)        # canon-key → aktueller Wert (Cache)


class IoBrokerClient:
    """
    Lädt Geräte aus javascript.0.virtualDevice.<Kategorie>.<Etage>.<Raum>.<Gerätename>
    und steuert deren States per REST API v1 (PATCH → ack=false).
    """

    def __init__(self, cfg: dict):
        self._prefix = cfg.get("virtual_device_prefix", "javascript.0.virtualDevice")
        # YAML parst 'on'/'off' als Boolean — Keys explizit zu str konvertieren
        raw_names = cfg.get("state_names", {
            "on":        "on",
            "level":     "level",
            "color":     "color",
            "colorTemp": "colorTemp",
        })
        self._state_names: dict[str, str] = {str(k): str(v) for k, v in raw_names.items()}

        # {room_lower: display_name}
        self.rooms: dict[str, str] = {}
        # {room_lower: {device_key: Device}}
        self.devices: dict[str, dict[str, Device]] = {}
        # {device_id: Device}
        self._devices_by_id: dict[str, Device] = {}
        # {state_id: value} — raw states without a room (weather, car, etc.)
        self._state_cache: dict[str, object] = {}

        # Set by main.py: fn(state_id, json_value) → sends SetState via gRPC adapter
        self._setter: Optional[Callable[[str, str], bool]] = None

        self._getter: Optional[Callable[[str], str]] = None

        # Feedback-Callback: fn(device, success, text)
        # device = MQTT-Gerätename des Satelliten, success = bool, text = Antworttext
        self._feedback_cb: Optional[Callable[[str, bool, str], None]] = None

        # Timeout für State-Bestätigung in Sekunden
        self._confirm_timeout: float = 3.0

        # Pending Confirmations: {state_id: {"expected": value, "device": str, "deadline": float, "label": str}}
        self._pending: dict[str, dict] = {}
        self._pending_lock = threading.Lock()

        # State-Suffixe, für die schon eine "fehlt in state_names"-Warnung geloggt wurde
        # (vermeidet Log-Spam bei wiederholten Live-Updates desselben Suffixes).
        self._warned_suffixes: set[str] = set()

        # Hintergrund-Thread für Timeouts
        self._timeout_thread = threading.Thread(
            target=self._timeout_loop, daemon=True, name="iobroker-confirm"
        )
        self._timeout_thread.start()

    def set_setter(self, fn: Callable[[str, str], bool]):
        """Register the gRPC state setter: fn(state_id, json_value) → True if adapter is connected."""
        self._setter = fn

    def set_getter(self, fn: Callable[[str], str]):
        """Register a gRPC state getter: fn(state_id) → json_value."""
        self._getter = fn

    def set_feedback_handler(self, fn: Callable[[str, bool, str], None], timeout: float = 3.0):
        """
        Registriert den Feedback-Callback.
        fn(device, success, text) wird aufgerufen wenn alle States bestätigt wurden
        oder der Timeout abläuft.
        """
        self._feedback_cb = fn
        self._confirm_timeout = timeout

    # ------------------------------------------------------------------
    # Laden

    def handle_device_snapshot(self, devices: Iterable[AgentDevice]):
        """
        Verarbeitet die gesamte Liste von gRPC AgentDevice Objekten.
        States ohne Raum (z.B. Wetter, Auto) landen im _state_cache.
        """
        new_device_map = {}
        new_state_cache = {}
        log.info(f"gRPC Snapshot: {len(devices)} Geräte erhalten, verarbeiten...")
        for device in devices:
            try:
                if not device.room:
                    new_state_cache[device.state_id] = self._parse_payload(device.value.value)
                    continue

                prefix_parts = self._prefix.split(".")
                parts = device.state_id.split(".")
                n = len(parts) - len(prefix_parts)
                if n not in (4, 5):
                    continue

                device_id = ".".join(parts[:-1])

                if device_id not in new_device_map:
                    room_display_name = dict(device.room_names).get("de") or device.room
                    new_device_map[device_id] = Device(
                        id=device_id,
                        name=device.device,
                        key=_camel_to_words(device.device),
                        room=device.room,
                        room_display_name=room_display_name,
                        floor=device.floor,
                        category=device.device_type,
                    )

                dev = new_device_map[device_id]
                state_suffix = parts[-1]
                dev.states[state_suffix] = device.state_id
                dev.current[state_suffix] = self._parse_payload(device.value.value)

                log.debug(f"Neues Gerät: {device_id} → {new_device_map[device_id]}")
            except Exception as e:
                log.warning(f"Fehler beim Verarbeiten von Gerät {device.state_id}: {e}", exc_info=True)
                continue

        self._finalize_loading(new_device_map, new_state_cache)

    def _finalize_loading(self, device_map, state_cache: dict | None = None):
        """ Hilfsmethode, um die internen Strukturen zu befüllen """
        self.rooms = {}
        self.devices = {}
        self._devices_by_id = {}
        self._state_cache = state_cache or {}

        total_states = 0
        filled_states = 0

        for device in device_map.values():
            room_key = device.room  # already the enum ID (e.g. "wohnzimmer")
            self.rooms[room_key] = device.room_display_name
            self._devices_by_id[device.id] = device

            if room_key not in self.devices:
                self.devices[room_key] = {}
            self.devices[room_key][device.key] = device

            total_states += len(device.states)
            filled_states += len(device.current)

        log.info(f"Update: {len(self.rooms)} Räume, {len(self._devices_by_id)} Geräte via gRPC geladen.")
        log.info(f"Cache: {filled_states}/{total_states} States aus gRPC-Snapshot geladen.")
        if self._state_cache:
            log.info(f"State-Cache: {len(self._state_cache)} rohe States ohne Raum (Wetter, Auto, …)")

        self._log_device_map()

    # ------------------------------------------------------------------
    # Intent ausführen

    def execute(self, intent: "Intent", satellite_device: str = "") -> int:
        """
        Löst einen Intent auf und setzt die entsprechenden States per MQTT.
        Gibt die Anzahl erfolgreich gesetzter States zurück.
        satellite_device: Name des Satelliten für TTS-Feedback (leer = kein Feedback)
        """
        if intent.name == "Unknown":
            log.debug("execute: Intent 'Unknown', nichts zu tun.")
            return 0

        if not intent.room:
            log.warning("execute: Kein Raum erkannt.")
            return 0

        targets: list[Device] = []

        if intent.device_id:
            dev = self._devices_by_id.get(intent.device_id)
            if dev:
                targets = [dev]
        else:
            all_devs = list(self.devices.get(intent.room_id or "", {}).values())
            if intent.category_filter:
                targets = [d for d in all_devs if d.category == intent.category_filter]
                log.debug(f"Kategorie-Filter '{intent.category_filter}': {len(targets)}/{len(all_devs)} Geräte")
            else:
                targets = all_devs

        if not targets:
            log.warning(
                f"execute: Keine Geräte für Raum '{intent.room}'"
                + (f" / Gerät '{intent.device}'" if intent.device else "")
                + " gefunden."
            )
            return 0

        state_key, value = self._intent_to_state_and_value(intent)
        if state_key is None:
            log.warning(f"execute: Unbekannter Intent '{intent.name}'")
            return 0

        log.info(
            f"execute: {intent.name} → {len(targets)} Gerät(e), "
            f"state='{state_key}', value={value!r}"
        )

        count = 0
        deadline = time.monotonic() + self._confirm_timeout
        for dev in targets:
            state_id = dev.states.get(state_key)
            if not state_id:
                log.debug(f"  {dev.name}: State '{state_key}' nicht vorhanden, übersprungen.")
                continue
            if self.set_state(state_id, value):
                count += 1
                # Bestätigung registrieren wenn Feedback gewünscht
                if satellite_device and self._feedback_cb:
                    label = f"{dev.name} im {dev.room_display_name}"
                    with self._pending_lock:
                        self._pending[state_id] = {
                            "expected":  value,
                            "device":    satellite_device,
                            "deadline":  deadline,
                            "label":     label,
                            "confirmed": False,
                        }

        return count

    # ------------------------------------------------------------------
    # State setzen

    def set_state(self, state_id: str, value) -> bool:
        """Send a SetState command to ioBroker via the gRPC adapter."""
        if not self._setter:
            log.error("set_state: no setter registered (call set_setter() first)")
            return False
        try:
            import json
            payload = json.dumps(value)
            result = self._setter(state_id, payload)
            log.debug(f"SetState {state_id} = {payload!r}")
            return result
        except Exception as e:
            log.error(f"set_state({state_id}, {value!r}) failed: {e}")
            return False

    def answer_query(self, intent: "Intent") -> Optional[str]:
        """
        Liest Gerätezustände aus dem Cache und gibt einen deutschen Antworttext zurück.
        Ohne Raum → globale Abfrage über alle Räume.
        Gibt None zurück wenn keine Daten verfügbar.
        """
        if intent.device_id:
            targets = [self._devices_by_id[intent.device_id]]
            room_label = intent.room
        elif intent.room:
            targets = list(self.devices.get(intent.room_id or "", {}).values())
            if intent.category_filter:
                targets = [d for d in targets if d.category == intent.category_filter]
            room_label = intent.room
        else:
            # Globale Abfrage: alle Räume
            all_devs = [d for devs in self.devices.values() for d in devs.values()]
            if intent.category_filter:
                all_devs = [d for d in all_devs if d.category == intent.category_filter]
            return self._answer_global(all_devs, intent.query_state, intent.category_filter)

        if not targets:
            return f"Ich kenne keine Geräte im {intent.room}."

        qs = intent.query_state

        # Einzelnes Gerät → detaillierte Antwort
        if len(targets) == 1:
            return self._describe_device(targets[0], qs)

        # Mehrere Geräte → Zusammenfassung
        return self._summarize(targets, qs, room_label)

    def _summarize(self, targets: list, qs: Optional[str], room_label: str) -> Optional[str]:
        """Fasst mehrere Geräte in einem Raum zusammen."""
        # Sensor-Kategorien direkt beschreiben (haben kein on/off)
        categories = {dev.category for dev in targets}
        if len(categories) == 1 and list(categories)[0] in self._CATEGORY_STATES:
            cat_answer = self._describe_category(list(categories)[0], targets, room_label)
            if cat_answer is not None:
                return cat_answer

        if qs == "on" or qs is None:
            on_devs  = [d for d in targets if d.current.get("on") is True]
            off_devs = [d for d in targets if d.current.get("on") is False]
            unknown  = [d for d in targets if "on" not in d.current]

            parts = []
            if on_devs:
                names = ", ".join(d.name for d in on_devs)
                parts.append(f"{names} {'ist' if len(on_devs) == 1 else 'sind'} an")
            if off_devs:
                names = ", ".join(d.name for d in off_devs)
                parts.append(f"{names} {'ist' if len(off_devs) == 1 else 'sind'} aus")
            if unknown:
                names = ", ".join(d.name for d in unknown)
                parts.append(f"von {names} habe ich keinen Status")

            if not parts:
                return f"Ich habe noch keine Statusdaten für {room_label}."
            return f"Im {room_label}: " + ", ".join(parts) + "."

        if qs == "level":
            lines = []
            for dev in targets:
                val = dev.current.get("level")
                if val is not None:
                    lines.append(f"{dev.name} {int(val)} Prozent")
            return (f"Helligkeit im {room_label}: " + ", ".join(lines) + ".") if lines \
                else f"Keine Helligkeitsdaten für {room_label}."

        # Kategorie-basierte Sensor-Zusammenfassung
        categories = {dev.category for dev in targets}
        if len(categories) == 1:
            cat_answer = self._describe_category(list(categories)[0], targets, room_label)
            if cat_answer is not None:
                return cat_answer

        return None

    def _answer_global(self, targets: list, qs: Optional[str], category_filter: Optional[str]) -> str:
        """Globale Abfrage über alle Räume — fasst Ergebnisse raumweise zusammen."""
        if not targets:
            if category_filter:
                return f"Ich kenne keine {_category_label(category_filter)}."
            return "Ich habe keine Gerätedaten."

        # Sensor-Kategorien direkt beschreiben (haben kein on/off)
        if category_filter and category_filter in self._CATEGORY_STATES:
            lines = []
            for dev in sorted(targets, key=lambda d: d.room):
                desc = self._describe_device(dev, qs)
                if desc:
                    lines.append(desc)
            return " ".join(lines) if lines else f"Keine {_category_label(category_filter)}-Daten verfügbar."

        if qs == "on" or qs is None:
            # Räume mit eingeschalteten Geräten nennen (nur Raumnamen, keine Geräteliste)
            rooms_on = sorted({dev.room_display_name for dev in targets if dev.current.get("on") is True})

            if not rooms_on:
                return f"Keine {_category_label(category_filter)} sind eingeschaltet."

            label = _category_label(category_filter)
            return f"Eingeschaltete {label} in: {', '.join(rooms_on)}."

        if qs == "level":
            lines = []
            for dev in sorted(targets, key=lambda d: d.room):
                val = dev.current.get("level")
                if val is not None:
                    lines.append(f"{dev.name} im {dev.room_display_name}: {int(val)} Prozent")
            return ("Helligkeit: " + ", ".join(lines) + ".") if lines \
                else "Keine Helligkeitsdaten verfügbar."

        # Sensor-Kategorien global
        categories = {dev.category for dev in targets}
        if len(categories) == 1:
            lines = []
            for dev in sorted(targets, key=lambda d: d.room):
                desc = self._describe_device(dev, qs)
                if desc:
                    lines.append(desc)
            return " ".join(lines) if lines else "Keine Sensordaten verfügbar."

        return "Bitte nenne einen Raum für diese Abfrage."

    # Kategorie → Beschreibungs-Logik für Sensoren
    # Format: kategorie → [(state_key, einheit, format_fn)]
    # format_fn: None = numerisch, "bool_offen" = offen/geschlossen, "bool_bewegung" = Bewegung/keine
    _CATEGORY_STATES: dict[str, list[tuple[str, str, Optional[str]]]] = {
        "temperature_sensor": [
            ("current", "Grad", None),
        ],
        "thermostat": [
            ("current",  "Grad", None),
            ("expected", "Grad", None),
        ],
        "window": [
            ("open", "", "bool_offen"),
        ],
        "door": [
            ("open", "", "bool_offen"),
        ],
        "blind": [
            ("level", "%", None),
        ],
        "climate": [
            ("on",       "", "bool_an"),
            ("mode",     "", None),
            ("current",  "°C", None),
            ("expected", "°C Soll", None),
            ("fanSpeed", "", None),
        ],
        "air_quality_sensor": [
            ("iaq",       "",        "iaq_label"),
            ("co2_equiv", "ppm CO₂", None),
            ("voc_equiv", "ppm VOC", None),
        ],
        "humidity_sensor": [
            ("current", "%", None),
        ],
        "illuminance_sensor": [
            ("illuminance", "lx", None),
        ],
    }

    def _describe_category(self, category: str, targets: list, room: str) -> Optional[str]:
        """Erzeugt Antworttexte für Sensor-Kategorien (Temperaturen, Fenster, Helligkeit)."""
        state_defs = self._CATEGORY_STATES.get(category)
        if not state_defs:
            return None

        lines = []
        for dev in targets:
            parts = []
            for state_key, unit, fmt in state_defs:
                val = dev.current.get(state_key)
                if val is None:
                    continue
                if fmt == "bool_offen":
                    parts.append("offen" if val else "geschlossen")
                elif fmt == "bool_an":
                    parts.append("an" if val else "aus")
                elif fmt == "bool_bewegung":
                    parts.append("Bewegung erkannt" if val else "keine Bewegung")
                elif fmt == "iaq_label":
                    parts.append(_iaq_label(float(val)))
                elif isinstance(val, float):
                    parts.append(f"{val:.1f} {unit}".strip())
                else:
                    parts.append(f"{val} {unit}".strip())
            if parts:
                if len(targets) > 1:
                    lines.append(f"{dev.name}: {', '.join(parts)}")
                else:
                    lines.append(", ".join(parts))

        if not lines:
            return None
        prefix = f"Im {room}" if len(targets) > 1 else f"{targets[0].name} im {room}"
        return prefix + ": " + ", ".join(lines) + "."

    _MODE_LABELS: dict[str, str] = {
        "cool":     "Kühlen",
        "heat":     "Heizen",
        "dry":      "Entfeuchten",
        "fan_only": "Lüfter",
        "auto":     "Auto",
    }
    _FAN_LABELS: dict[str, str] = {
        "low":    "niedrig",
        "medium": "mittel",
        "high":   "hoch",
        "auto":   "auto",
    }

    def _describe_device(self, dev: "Device", qs: Optional[str]) -> str:
        name = dev.name
        room = dev.room_display_name

        if dev.category == "climate":
            parts = []
            on = dev.current.get("on")
            if on is not None:
                parts.append("an" if on else "aus")
            mode = dev.current.get("mode")
            if mode is not None:
                parts.append(f"Modus {self._MODE_LABELS.get(str(mode), str(mode))}")
            current = dev.current.get("current")
            if current is not None:
                parts.append(f"{float(current):.1f}°C")
            expected = dev.current.get("expected")
            if expected is not None:
                parts.append(f"Soll {float(expected):.1f}°C")
            fan = dev.current.get("fanSpeed")
            if fan is not None:
                parts.append(f"Lüfter {self._FAN_LABELS.get(str(fan), str(fan))}")
            if not parts:
                return f"Ich habe keine Daten für {name} im {room}."
            return f"{name} im {room}: {', '.join(parts)}."

        # Kategorie-basierte Sensor-Beschreibung
        cat_answer = self._describe_category(dev.category, [dev], room)
        if cat_answer is not None:
            return cat_answer

        if qs == "level" or (qs is None and "level" in dev.current):
            val = dev.current.get("level")
            if val is not None:
                return f"{name} im {room} ist auf {int(val)} Prozent."
            return f"Keine Helligkeitsdaten für {name}."

        if qs == "color" or (qs is None and "color" in dev.current):
            val = dev.current.get("color")
            if val is not None:
                return f"{name} im {room} leuchtet in {val}."
            return f"Keine Farbdaten für {name}."

        # Default: on/off (+ optionaler Stromverbrauch)
        val = dev.current.get("on")
        if val is None:
            return f"Ich weiß nicht ob {name} im {room} an oder aus ist."
        status = "an" if val else "aus"
        power = dev.current.get("power")
        if power is not None:
            return f"{name} im {room} ist {status} ({power} W)."
        return f"{name} im {room} ist {status}."

    def handle_state_update(self, state_id: str, raw: str):
        """
        Callback für eingehende State-Updates aus ioBroker.
        Parst den Rohwert, schreibt ihn in den Device-Cache und prüft Pending-Confirmations.
        States ohne Raum landen im _state_cache.
        """
        if state_id in self._state_cache:
            self._state_cache[state_id] = self._parse_payload(raw)
            log.debug(f"State-Cache: {state_id} = {self._state_cache[state_id]!r}")
            return

        device_id = ".".join(state_id.rsplit(".", 1)[:-1])
        state_suffix = state_id.rsplit(".", 1)[-1]
        device = self._devices_by_id.get(device_id)
        if not device:
            return

        state_suffix_map = {v: k for k, v in self._state_names.items()}
        canon = state_suffix_map.get(state_suffix)
        if not canon:
            if state_suffix not in self._warned_suffixes:
                self._warned_suffixes.add(state_suffix)
                log.warning(
                    f"Unbekannter State-Suffix '{state_suffix}' (state_id={state_id}) — "
                    f"fehlt in config.yaml's iobroker.state_names, Live-Update wird verworfen "
                    f"(Wert friert auf dem letzten Snapshot ein)."
                )
            return

        value = self._parse_payload(raw)
        device.current[canon] = value
        log.debug(f"Cache: {device.name}.{canon} = {value!r}")

        # Pending-Confirmation prüfen
        with self._pending_lock:
            pending = self._pending.pop(state_id, None)

        if pending and self._feedback_cb:
            success = (value == pending["expected"])
            if success:
                log.info(f"Bestätigung: {pending['label']} = {value!r} ✓")
                self._fire_feedback(pending["device"], True, pending, remaining=self._count_pending(pending["device"]))
            else:
                log.warning(f"Bestätigung: {pending['label']} = {value!r}, erwartet {pending['expected']!r} ✗")
                self._fire_feedback(pending["device"], False, pending, remaining=0)

    def _count_pending(self, satellite_device: str) -> int:
        """Gibt die Anzahl noch ausstehender Confirmations für einen Satelliten zurück."""
        with self._pending_lock:
            return sum(1 for p in self._pending.values() if p["device"] == satellite_device)

    def _fire_feedback(self, satellite_device: str, success: bool, pending: dict, remaining: int):
        """Ruft den Feedback-Callback auf — aber nur wenn keine weiteren Confirmations ausstehen."""
        if remaining > 0:
            log.debug(f"Feedback zurückgestellt: noch {remaining} ausstehende States.")
            return
        if success:
            text = "ok"
        else:
            text = f"{pending['label']} konnte nicht geschaltet werden."
        self._feedback_cb(satellite_device, success, text)

    def _timeout_loop(self):
        """Prüft regelmäßig ob Pending-Confirmations abgelaufen sind."""
        while True:
            time.sleep(0.5)
            now = time.monotonic()
            timed_out = []
            with self._pending_lock:
                for state_id, pending in list(self._pending.items()):
                    if now >= pending["deadline"]:
                        timed_out.append((state_id, pending))
                        del self._pending[state_id]

            for state_id, pending in timed_out:
                log.warning(f"Timeout: keine Bestätigung für {pending['label']} ({state_id})")
                if self._feedback_cb:
                    # Noch ausstehende für diesen Satelliten nach Timeout auch entfernen
                    remaining = self._count_pending(pending["device"])
                    if remaining == 0:
                        self._feedback_cb(
                            pending["device"],
                            False,
                            f"{pending['label']} antwortet nicht — möglicherweise offline.",
                        )

    def control_direct(self, device_id: str, state_key: str, raw_value: str) -> bool:
        """
        Setzt einen Device-State direkt ohne NLU-Umweg (für gRPC-Menü-Steuerung).
        device_id  : Device.id, z.B. "javascript.0.virtualDevice.Licht.EG.Wohnzimmer.DeckeSeite"
        state_key  : kanonischer Key, z.B. "on", "level", "color"
        raw_value  : String-serialisierter Wert, z.B. "true", "50", "#FF0000"
        """
        device = self._devices_by_id.get(device_id)
        if not device:
            log.warning(f"control_direct: Gerät {device_id!r} nicht gefunden")
            return False
        state_id = device.states.get(state_key)
        if not state_id:
            log.warning(f"control_direct: State {state_key!r} für {device.name!r} nicht vorhanden")
            return False
        value = self._parse_payload(raw_value)
        if self.set_state(state_id, value):
            # Update cache immediately — the gRPC roundtrip is async, so GetDevices
            # called right after ControlDevice would otherwise return the stale state.
            device.current[state_key] = value
            return True
        return False

    def get_devices_snapshot(self) -> list[dict]:
        """
        Gibt alle Räume + Geräte als serialisierbares Dict zurück (für gRPC GetDevices).
        Format: [{key, name, devices: [{id, name, category, states, current}]}]
        """
        result = []
        for room_key in sorted(self.rooms):
            room_devices = []
            for dev in sorted(self.devices[room_key].values(), key=lambda d: d.name):
                room_devices.append({
                    "id":       dev.id,
                    "name":     dev.name,
                    "category": dev.category,
                    "states":   list(dev.states.keys()),
                    "current":  {k: str(v) for k, v in dev.current.items()},
                })
            result.append({
                "key":     room_key,
                "name":    self.rooms[room_key],
                "devices": room_devices,
            })
        return result

    def get_state(self, device_id: str, canon: str):
        """Gibt den gecachten Wert eines Device-States zurück, oder None."""
        device = self._devices_by_id.get(device_id)
        if device:
            return device.current.get(canon)
        return None

    def get_state_raw(self, state_id: str) -> str | None:
        """Liest einen ioBroker-State aus dem lokalen Cache. Gibt None zurück wenn nicht bekannt."""
        if state_id in self._state_cache:
            val = self._state_cache[state_id]
            return str(val) if val is not None else None

        device_id = ".".join(state_id.rsplit(".", 1)[:-1])
        state_suffix = state_id.rsplit(".", 1)[-1]
        device = self._devices_by_id.get(device_id)
        if device:
            val = device.current.get(state_suffix)
            return str(val) if val is not None else None

        log.debug(f"get_state_raw: '{state_id}' nicht im Cache — State nicht subscribed?")
        return None

    @staticmethod
    def _parse_payload(raw: str):
        """Konvertiert MQTT-Rohpayload in einen Python-Typ."""
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

    # ------------------------------------------------------------------
    # Intern

    def _intent_to_state_and_value(self, intent: "Intent") -> tuple[Optional[str], any]:
        if intent.name == "TurnOn":
            return "on", True
        if intent.name == "TurnOff":
            return "on", False
        if intent.name == "SetLevel":
            return "level", intent.value
        if intent.name == "SetColor":
            return "color", intent.value
        if intent.name == "SetTemperature":
            return "expected", intent.value
        if intent.name == "SetMode":
            return "mode", intent.value
        if intent.name == "SetFanSpeed":
            return "fanSpeed", intent.value
        return None, None

    def _log_device_map(self):
        log.info("─" * 60)
        log.info("Bekannte Geräte:")
        for room_key in sorted(self.rooms):
            log.info(f"  [{self.rooms[room_key]}]")
            for dev in sorted(self.devices[room_key].values(), key=lambda d: d.name):
                states = ", ".join(dev.states.keys()) or "—"
                log.info(f"    · {dev.name} ({dev.floor}) [{dev.category}] — States: {states}")
                log.debug(f"      ID: {dev.id}")
        log.info("─" * 60)
