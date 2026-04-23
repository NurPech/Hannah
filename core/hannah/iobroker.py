import logging
import re
import threading
import time
import requests
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from .nlu import Intent

log = logging.getLogger(__name__)


_UMLAUT_MAP = {"ae": "ä", "oe": "ö", "ue": "ü", "Ae": "Ä", "Oe": "Ö", "Ue": "Ü"}

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
    id: str          # javascript.0.virtualDevice.Licht.EG.Wohnzimmer.DeckeSeite
    name: str        # DeckeSeite (Originalname)
    key: str         # decke seite (normalisiert für NLU-Matching)
    room: str        # Wohnzimmer
    floor: str       # EG
    category: str    # Licht
    states: dict = field(default_factory=dict)         # canon-key → state_id
    current: dict = field(default_factory=dict)        # canon-key → aktueller Wert (Cache)


class IoBrokerClient:
    """
    Lädt Geräte aus javascript.0.virtualDevice.<Kategorie>.<Etage>.<Raum>.<Gerätename>
    und steuert deren States per REST API v1 (PATCH → ack=false).
    """

    def __init__(self, cfg: dict):
        host = cfg.get("host", "localhost")
        port = cfg.get("port", 8093)
        self._base = f"http://{host}:{port}"
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

        # Wird von main.py gesetzt: fn(topic, payload_str) → publiziert via MQTT
        self._publish: Optional[Callable[[str, str], None]] = None

        # Feedback-Callback: fn(device, success, text)
        # device = MQTT-Gerätename des Satelliten, success = bool, text = Antworttext
        self._feedback_cb: Optional[Callable[[str, bool, str], None]] = None

        # Timeout für State-Bestätigung in Sekunden
        self._confirm_timeout: float = 3.0

        # Pending Confirmations: {state_id: {"expected": value, "device": str, "deadline": float, "label": str}}
        self._pending: dict[str, dict] = {}
        self._pending_lock = threading.Lock()

        # Hintergrund-Thread für Timeouts
        self._timeout_thread = threading.Thread(
            target=self._timeout_loop, daemon=True, name="iobroker-confirm"
        )
        self._timeout_thread.start()

    @property
    def state_topic_prefix(self) -> str:
        """MQTT-Topic-Prefix für State-Updates, z.B. 'javascript/0/virtualDevice'."""
        return self._prefix.replace(".", "/")

    def set_publisher(self, fn: Callable[[str, str], None]):
        """Registriert die MQTT-Publish-Funktion. Muss vor execute() aufgerufen werden."""
        self._publish = fn

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

    def load(self):
        prefix_parts = self._prefix.split(".")
        pattern = self._prefix + ".*.*.*.*.*"   # Kategorie.Etage.Raum.Gerät.State

        try:
            member_to_room = self._load_room_mapping()
            objects = self._get_objects(pattern)
        except Exception as e:
            log.error(f"ioBroker nicht erreichbar: {e}")
            return

        state_suffix_map = {v: k for k, v in self._state_names.items()}
        device_map: dict[str, Device] = {}

        for obj in objects:
            oid = obj.get("_id") or obj.get("id", "")
            if not oid:
                continue

            if not oid.startswith(self._prefix + "."):
                continue

            parts = oid.split(".")
            n = len(parts) - len(prefix_parts)
            # Unterstützte Strukturen nach dem Prefix:
            #   5: Kategorie.Etage.Raum.Gerät.State  (Normalfall)
            #   4: Kategorie.Etage.Raum.State         (kein separater Gerätename)
            if n not in (4, 5):
                continue

            state_suffix = parts[-1]
            if state_suffix not in state_suffix_map:
                continue

            device_id = ".".join(parts[:-1])
            category  = parts[len(prefix_parts)]
            floor     = parts[len(prefix_parts) + 1] if n == 5 else ""
            dev_name  = parts[len(prefix_parts) + 3] if n == 5 else parts[len(prefix_parts) + 2]

            if device_id not in device_map:
                # Kanonischen Raumnamen aus enum/rooms ermitteln, Pfad-Segment als Fallback
                room_name = self._find_room_for_device(device_id, member_to_room) \
                            or parts[len(prefix_parts) + 2]
                device_map[device_id] = Device(
                    id=device_id,
                    name=dev_name,
                    key=_camel_to_words(dev_name),
                    room=room_name,
                    floor=floor,
                    category=category,
                )

            canon = state_suffix_map[state_suffix]
            device_map[device_id].states[canon] = oid
            log.debug(f"  State geladen: {oid}")

        self.rooms = {}
        self.devices = {}
        self._devices_by_id = {}

        for device in device_map.values():
            room_key = device.room.lower()
            self.rooms[room_key] = device.room
            self._devices_by_id[device.id] = device
            if room_key not in self.devices:
                self.devices[room_key] = {}
            self.devices[room_key][device.key] = device
            log.debug(
                f"  Gerät: {device.name} (key='{device.key}') [{device.room}/{device.floor}] "
                f"States: {list(device.states.keys())}"
            )

        total = sum(len(d) for d in self.devices.values())
        log.info(f"ioBroker: {len(self.rooms)} Räume, {total} Geräte geladen.")
        self._log_device_map()
        self._warm_cache()

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
                    label = f"{dev.name} im {dev.room}"
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
        """Setzt einen ioBroker-State via MQTT (hannah/set/...). Gibt True bei Erfolg zurück."""
        if not self._publish:
            log.error("set_state: kein Publisher registriert (set_publisher() nicht aufgerufen)")
            return False

        topic = self._state_id_to_topic(state_id)
        if isinstance(value, bool):
            payload = "true" if value else "false"
        elif isinstance(value, float) and value.is_integer():
            payload = str(int(value))
        else:
            payload = str(value)

        try:
            self._publish(topic, payload)
            log.debug(f"MQTT {topic} = {payload!r}")
            return True
        except Exception as e:
            log.error(f"set_state({state_id}, {value!r}) fehlgeschlagen: {e}")
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
                return f"Ich kenne keine {category_filter}-Geräte."
            return "Ich habe keine Gerätedaten."

        # Sensor-Kategorien direkt beschreiben (haben kein on/off)
        if category_filter and category_filter in self._CATEGORY_STATES:
            lines = []
            for dev in sorted(targets, key=lambda d: d.room):
                desc = self._describe_device(dev, qs)
                if desc:
                    lines.append(desc)
            return " ".join(lines) if lines else f"Keine {category_filter}-Daten verfügbar."

        if qs == "on" or qs is None:
            # Räume mit eingeschalteten Geräten nennen (nur Raumnamen, keine Geräteliste)
            rooms_on = sorted({dev.room for dev in targets if dev.current.get("on") is True})

            if not rooms_on:
                label = f"{category_filter}-Geräte" if category_filter else "Geräte"
                return f"Keine {label} sind eingeschaltet."

            label = category_filter if category_filter else "Geräte"
            return f"Eingeschaltete {label} in: {', '.join(rooms_on)}."

        if qs == "level":
            lines = []
            for dev in sorted(targets, key=lambda d: d.room):
                val = dev.current.get("level")
                if val is not None:
                    lines.append(f"{dev.name} im {dev.room}: {int(val)} Prozent")
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
        "Temperaturen": [
            ("current",  "Grad",  None),
            ("expected", "Grad",  None),
        ],
        "Helligkeit": [
            ("illuminance", "Lux", None),
        ],
        "Fenster": [
            ("open", "", "bool_offen"),
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
                elif fmt == "bool_bewegung":
                    parts.append("Bewegung erkannt" if val else "keine Bewegung")
                elif isinstance(val, float):
                    parts.append(f"{val:.1f} {unit}".strip())
                else:
                    parts.append(f"{val} {unit}".strip())
            if parts:
                lines.append(f"{dev.name}: {', '.join(parts)}")

        if not lines:
            return None
        prefix = f"Im {room}" if len(targets) > 1 else f"{targets[0].name} im {room}"
        return prefix + ": " + ", ".join(lines) + "."

    def _describe_device(self, dev: "Device", qs: Optional[str]) -> str:
        name = dev.name
        room = dev.room

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

        # Default: on/off
        val = dev.current.get("on")
        if val is None:
            return f"Ich weiß nicht ob {name} im {room} an oder aus ist."
        return f"{name} im {room} ist {'an' if val else 'aus'}."

    def handle_state_update(self, state_id: str, raw: str):
        """
        Callback für eingehende MQTT State-Updates aus ioBroker.
        Parst den Rohwert, schreibt ihn in den Device-Cache und prüft Pending-Confirmations.
        """
        device_id = ".".join(state_id.rsplit(".", 1)[:-1])
        state_suffix = state_id.rsplit(".", 1)[-1]
        device = self._devices_by_id.get(device_id)
        if not device:
            return

        state_suffix_map = {v: k for k, v in self._state_names.items()}
        canon = state_suffix_map.get(state_suffix)
        if not canon:
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

    def list_roomies(self, roomie_prefix: str = "residents.0.roomie") -> dict[str, str]:
        """
        Gibt alle Roomie-Channels aus dem ioBroker Residents-Adapter zurück.
        Rückgabe: {roomie_id: display_name}, z.B. {"leonie": "Leonie", "hannah": "Hannah"}
        Wirft eine Exception wenn ioBroker nicht erreichbar ist.
        """
        states = self._get_states_by_filter(f"{roomie_prefix}*")
        prefix_dot = roomie_prefix + "."
        # Schritt 1: alle Roomie-IDs sammeln
        roomie_ids: set[str] = set()
        for state_id in states:
            if not state_id.startswith(prefix_dot):
                continue
            remainder = state_id[len(prefix_dot):]
            roomie_id = remainder.split(".")[0]
            if roomie_id:
                roomie_ids.add(roomie_id)

        # Schritt 2: Display-Name aus <prefix>.<id>.info.name lesen
        result: dict[str, str] = {}
        for roomie_id in roomie_ids:
            name_key = f"{prefix_dot}{roomie_id}.info.name"
            entry = states.get(name_key, {})
            val = entry.get("val")
            display = str(val) if val is not None else roomie_id
            result[roomie_id] = display

        return result

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
        return self.set_state(state_id, value)

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
        """Liest einen beliebigen ioBroker-State per REST und gibt ihn als String zurück."""
        val = self._get_state_value(state_id)
        return str(val) if val is not None else None

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

    def _state_id_to_topic(self, state_id: str) -> str:
        """
        javascript.0.virtualDevice.Licht.EG.Wohnzimmer.DeckeSeite.on
        → hannah/set/devices/Licht/EG/Wohnzimmer/DeckeSeite/on
        """
        suffix = state_id[len(self._prefix):].lstrip(".")
        return "hannah/set/devices/" + suffix.replace(".", "/")

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
        return None, None

    def _load_room_mapping(self) -> dict[str, str]:
        """
        Lädt enum/rooms und gibt {member_id: kanonischer_raumname} zurück.
        Jeder Member-Eintrag (und alle seine Eltern) wird dem Raum zugeordnet.
        """
        try:
            rooms_raw = self._get_enum("rooms")
        except Exception as e:
            log.warning(f"enum/rooms nicht ladbar, Fallback auf Pfad-Segmente: {e}")
            return {}

        mapping: dict[str, str] = {}
        for room in rooms_raw:
            name = self._extract_name(room)
            if not name:
                continue
            for member in room.get("common", {}).get("members", []):
                mapping[member] = name
                # Auch den Parent eintragen damit device_id ohne State-Suffix matcht
                # z.B. "...Schlafzimmer.on" → auch "...Schlafzimmer" → Raum
                parent = member.rsplit(".", 1)[0]
                if parent not in mapping:
                    mapping[parent] = name

        log.info(f"Raum-Mapping: {len(rooms_raw)} Räume, {len(mapping)} Members geladen.")
        return mapping

    def _find_room_for_device(self, device_id: str, member_to_room: dict[str, str]) -> Optional[str]:
        """
        Sucht den Raum für eine Device-ID indem die ID und alle Eltern-Prefixe
        gegen das Member-Mapping geprüft werden. Längster Treffer gewinnt.
        """
        parts = device_id.split(".")
        # Von spezifisch (ganzer Pfad) nach allgemein (kürzerer Prefix)
        for length in range(len(parts), 0, -1):
            candidate = ".".join(parts[:length])
            if candidate in member_to_room:
                return member_to_room[candidate]
        return None

    def _extract_name(self, obj: dict) -> Optional[str]:
        """Extrahiert den lokalisierten Namen aus einem ioBroker-Enum-Objekt."""
        name = obj.get("common", {}).get("name", "")
        if isinstance(name, dict):
            return name.get("de") or name.get("en") or next(iter(name.values()), None)
        return str(name) if name else None

    def _get_enum(self, kind: str) -> list[dict]:
        """Ruft /v1/enum/rooms oder /v1/enum/functions ab."""
        url = f"{self._base}/v1/enum/{kind}"
        resp = requests.get(url, headers={"accept": "application/json"}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            members = data.get("members")
            if isinstance(members, list) and members and isinstance(members[0], dict):
                return members
            return [v for v in data.values() if isinstance(v, dict)]
        return []

    def _get_states_by_filter(self, filter_pattern: str) -> dict[str, dict]:
        """
        Ruft /v1/states?filter=<pattern> ab und gibt ein flaches Dict zurück:
        {state_id: {"val": ..., "ack": ..., ...}}
        """
        url = f"{self._base}/v1/states"
        resp = requests.get(
            url,
            params={"filter": filter_pattern},
            headers={"accept": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            return data
        return {}

    def _get_objects(self, pattern: str) -> list[dict]:
        url = f"{self._base}/v1/objects"
        resp = requests.get(
            url,
            params={"pattern": pattern, "type": "state"},
            headers={"accept": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            if "rows" in data:
                return [row.get("value", row) for row in data["rows"]]
            return [{"_id": k, **v} for k, v in data.items() if isinstance(v, dict)]
        return []

    def _warm_cache(self):
        """
        Liest alle bekannten States einmalig via REST API und füllt den Cache.
        Nötig für nicht-retained MQTT-Topics die erst nach einem State-Wechsel eintreffen.
        """
        total = filled = 0

        for device in self._devices_by_id.values():
            for canon, state_id in device.states.items():
                total += 1
                try:
                    val = self._get_state_value(state_id)
                    if val is not None:
                        device.current[canon] = val
                        filled += 1
                        log.debug(f"Cache warm: {device.name}.{canon} = {val!r}")
                except Exception as e:
                    log.debug(f"Cache warm fehlgeschlagen für {state_id}: {e}")

        log.info(f"Cache-Vorwärmung: {filled}/{total} States geladen.")

    def _get_state_value(self, state_id: str):
        """Liest den aktuellen Wert eines States via REST API. Gibt None zurück wenn nicht verfügbar."""
        url = f"{self._base}/v1/state/{state_id}"
        resp = requests.get(url, headers={"accept": "application/json"}, timeout=5)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        # ioBroker gibt {"val": ..., "ack": ..., "ts": ...} zurück
        if isinstance(data, dict):
            raw = data.get("val")
            if raw is None:
                return None
            return self._parse_payload(str(raw))
        return None

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
