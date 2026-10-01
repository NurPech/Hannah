import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Iterable, Optional

from hannah_proto.v2 import hannah_pb2 as pb

from . import responses
from .typed_devices import slot_value_from_pb, slot_value_to_pb

if TYPE_CHECKING:
    from .nlu import Intent

log = logging.getLogger(__name__)


# Legacy (#387): nur noch für den alten Gerätebaum. Die typisierte Registry braucht sie nicht.
# Suffix→canon-Tabelle für handle_state_update() (Live-Updates): Fallback für Adapter,
# die in AgentStateUpdate kein canonical_key mitschicken (Feld erst seit hannah-proto
# 4.8; ältere Adapter senden es nicht). Neuere Adapter liefern den Key direkt, dann wird
# diese Tabelle nicht befragt. Vorher aus Settings/DB editierbar (siehe
# deploy/migrate_config_settings.py), jetzt nur noch Code-Konstante, da der Adapter die
# Rolle selbst aus common.role auflöst und kein Nutzer-Mapping mehr nötig ist.
DEFAULT_IOBROKER_STATE_NAMES: dict = {
    "on": "on", "level": "level", "color": "color", "colorTemp": "colorTemp",
    "current": "current", "expected": "expected", "illuminance": "illuminance",
    "open": "open", "iaq": "iaq", "co2_equiv": "co2_equiv", "voc_equiv": "voc_equiv",
    "power": "power",
}

# Trust-Level pro State (#366): unbekannte Sprecher/unverknüpfte Accounts zählen als Gast.
GUEST_TRUST_LEVEL = 0
TRUST_DENIED_TEXT = "Das darfst du leider nicht steuern."


class TrustLevelDenied(Exception):
    """Der anfragende User hat für diesen State ein zu niedriges Trust-Level (#366)."""

_UMLAUT_MAP = {"ae": "ä", "oe": "ö", "ue": "ü", "Ae": "Ä", "Oe": "Ö", "Ue": "Ü"}

_CATEGORY_LABELS: dict[str, str] = {
    "light":               "Lichter",
    "socket":              "Steckdosen",
    "climate":             "Klimageräte",
    "blind":               "Rollläden",
    "sensor":              "Sensoren",
    "window":              "Fenster",
    "door":                "Türen",
    "thermostat":          "Heizungen",
    "temperature_sensor":  "Temperatursensoren",
    "humidity_sensor":     "Feuchtigkeitssensoren",
    "illuminance_sensor":  "Helligkeitssensoren",
    "air_quality_sensor":  "Luftqualitätssensoren",
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
    state_types: dict = field(default_factory=dict)    # canon-key → StateType (Enum-Int des hannah.v1-Adapters, siehe hannah_proto.v1.shared_pb2)
    enum_values: dict = field(default_factory=dict)    # canon-key → {rohwert: label}, nur bei ENUM/COLOR
    state_writable: dict = field(default_factory=dict) # canon-key → bool, aus ioBroker common.write
    inverted: bool = False  # category 'blind': Aktor nutzt 0%=auf/100%=zu statt Hannahs Konvention (#270)
    required_trust: dict = field(default_factory=dict) # canon-key → Mindest-Trust-Level zum Setzen (#366), fehlt = keine Einschränkung


class IoBrokerClient:
    """
    Lädt Geräte aus javascript.0.virtualDevice.<Kategorie>.<Etage>.<Raum>.<Gerätename>
    und steuert deren States per REST API v1 (PATCH → ack=false).

    Seit #387 liest und schreibt nichts Sichtbares mehr den State-basierten Gerätebaum
    (`devices`, `Device`): NLU, Abfragen, Steuern, Tool-Agent und GetDevices laufen über die
    typisierte Registry (hannah.typed_devices). Der Baum wird aus dem v1-Snapshot weiter gebaut,
    solange Legacy-Wege ihn brauchen, und ist zur Abkündigung vorgemerkt.
    """

    def __init__(self, cfg: dict):
        # YAML parst 'on'/'off' als Boolean — Keys explizit zu str konvertieren
        raw_names = cfg.get("state_names", DEFAULT_IOBROKER_STATE_NAMES)
        self._state_names: dict[str, str] = {str(k): str(v) for k, v in raw_names.items()}
        # roher ioBroker-Suffix → kanonischer Key, für Snapshot UND Live-Update (#256)
        self._suffix_to_canon: dict[str, str] = {v: k for k, v in self._state_names.items()}

        # {room_lower: display_name}
        self.rooms: dict[str, str] = {}
        # {room_lower: {device_key: Device}}
        self.devices: dict[str, dict[str, Device]] = {}
        # {device_id: Device}
        self._devices_by_id: dict[str, Device] = {}
        # {state_id: value} — raw states without a room (weather, car, etc.)
        self._state_cache: dict[str, object] = {}
        # {state_id: Mindest-Trust-Level} — für Schreibpfade, die nur die state_id kennen (Tool-Agent, #366)
        self._required_trust_by_state: dict[str, int] = {}

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

        # Synchroner Confirm-Waiter für execute(wait_confirm=True) (Text-Kanäle, #373) —
        # bewusst getrennt von _pending/_feedback_cb (Satelliten-Pfad, asynchron): beide
        # beobachten denselben state_id-Bestätigungsweg in handle_state_update(), aber mit
        # unterschiedlichem Konsummuster (ein Aufrufer wartet synchron auf genau seine
        # eigenen state_ids, der andere batcht callback-artig pro Satellit).
        self._confirm_waiters: dict[str, dict] = {}
        self._confirm_lock = threading.Lock()

        # State-Suffixe, für die schon eine "fehlt in state_names"-Warnung geloggt wurde
        # (vermeidet Log-Spam bei wiederholten Live-Updates desselben Suffixes).
        self._warned_suffixes: set[str] = set()

        # Beantwortet Geräte-Abfragen aus der typisierten Registry (#387, hannah.device_answers);
        # ohne sie läuft die Antwort wie bisher über den State-basierten Gerätebaum.
        self._answerer: Optional[Callable[["Intent"], Optional[str]]] = None

        # Steuern über die typisierte Registry und den DeviceController (#387, Schritt 4); ohne
        # sie schreibt execute() wie bisher über den State-basierten Gerätebaum.
        self._registry = None
        self._controller = None
        self._room_names: Callable[[], dict] = lambda: self.rooms

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

    def set_slot_control(self, registry, controller, room_names: Optional[Callable[[], dict]] = None):
        """Register the typed registry and the slot controller (hannah.device_control): execute()
        resolves its targets in the registry and writes through `controller.set_slot`.
        room_names: () → {room_id: display name}, for the labels in the answers."""
        self._registry = registry
        self._controller = controller
        if room_names is not None:
            self._room_names = room_names

    def set_answerer(self, fn: Callable[["Intent"], Optional[str]]):
        """Register the query answerer: fn(intent) → answer text (hannah.device_answers)."""
        self._answerer = fn

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

    def handle_device_snapshot(self, devices: Iterable):
        """
        Verarbeitet die gesamte Liste von hannah.v1-AgentDevice-Objekten (State-basierter
        Pfad der v1-Adapter; typisierte Geräte aus hannah.v2 kommen mit #383 dazu).
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

                parts = device.state_id.split(".")
                # device_id kommt vom Adapter (#257), ab Adapter 1.1.0 (hannah-proto
                # 3.8.0). Seit #359 lehnt Core ältere Clients nicht mehr per
                # x-proto-version ab — ein Adapter <1.1.0 käme hier also wieder an;
                # seine Geräte ohne device_id werden übersprungen.
                if not device.device_id:
                    continue
                device_id = device.device_id

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
                # Ein Sibling-State ohne erkennbare Kategorie (z.B. ein Power-Meter-State
                # ohne passende Role/Funktion) darf die Kategorie eines bereits erkannten
                # Geschwister-States (z.B. der "on"-State eines Steckdosen-Geräts) nicht
                # überschreiben — erster nicht-leerer Wert gewinnt (#133).
                if not dev.category and device.device_type:
                    dev.category = device.device_type
                if device.inverted:
                    dev.inverted = True
                # canonical_key kommt vom Adapter (#257), aus common.role aufgelöst —
                # garantiert vorhanden, siehe device_id-Kommentar oben. Rohsuffix nur noch
                # als Sicherheitsnetz für eine unaufgelöste Rolle (z.B. Homematic-eigene
                # WORKING/DIRECTION) — bleibt dann weiterhin nutzbar über das Geräte-Menü
                # (control_direct/GetDevices).
                canon = device.canonical_key or parts[-1]
                dev.states[canon] = device.state_id
                dev.current[canon] = self._parse_payload(device.value.value)
                dev.state_types[canon] = device.state_type
                dev.state_writable[canon] = device.writable
                if device.enum_values.values:
                    dev.enum_values[canon] = dict(device.enum_values.values)
                # optional im Proto: nicht gesetzt = keine Einschränkung, bewusst verschieden von 0 (#366)
                if device.HasField("required_trust_level"):
                    dev.required_trust[canon] = device.required_trust_level

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
        self._required_trust_by_state = {
            device.states[canon]: level
            for device in device_map.values()
            for canon, level in device.required_trust.items()
            if canon in device.states
        }

        total_states = 0
        filled_states = 0

        for device in device_map.values():
            room_key = device.room  # already the enum ID (e.g. "wohnzimmer")
            self.rooms[room_key] = device.room_display_name
            self._devices_by_id[device.id] = device

            if room_key not in self.devices:
                self.devices[room_key] = {}
            # Nach device.id (eindeutig) statt device.key (abgeleiteter Anzeigename)
            # geschlüsselt (#275) — zwei Geräte im selben Raum können denselben Anzeigenamen
            # haben (z.B. Fenster und Licht, beide von iobroker.hannah auf "Bad" aufgelöst,
            # wenn beide ohne expliziten common.name auf den letzten ID-Pfadteil
            # zurückfallen) und würden sich sonst stillschweigend gegenseitig überschreiben.
            # device.key bleibt der NLU-Suchbegriff, nur nicht mehr der Dict-Key hier.
            self.devices[room_key][device.id] = device

            total_states += len(device.states)
            filled_states += len(device.current)

        log.info(f"Update: {len(self.rooms)} Räume, {len(self._devices_by_id)} Geräte via gRPC geladen.")
        log.info(f"Cache: {filled_states}/{total_states} States aus gRPC-Snapshot geladen.")
        if self._state_cache:
            log.info(f"State-Cache: {len(self._state_cache)} rohe States ohne Raum (Wetter, Auto, …)")

        self._log_device_map()

    # ------------------------------------------------------------------
    # Intent ausführen

    def may_set(self, state_id: str, trust_level: Optional[int]) -> bool:
        """
        Zentrale Trust-Level-Prüfung (#366) für alle Schreibpfade mit User-Kontext.
        trust_level None = Aufrufer ohne User, der bewusst ungeprüft bleibt (ioBroker-
        textCommand); unbekannte User übergeben GUEST_TRUST_LEVEL, nicht None.
        Nur Setzen wird geprüft, Lesen nie.
        """
        if trust_level is None:
            return True
        required = self._required_trust_by_state.get(state_id)
        return required is None or trust_level >= required

    def execute(
        self,
        intent: "Intent",
        satellite_device: str = "",
        *,
        trust_level: Optional[int],
        denied: Optional[list[str]] = None,
        requester_name: str = "",
        wait_confirm: bool = False,
        offline: Optional[list[str]] = None,
        unsupported: Optional[list[str]] = None,
    ) -> int:
        """
        Löst einen Intent auf und setzt die entsprechenden States per MQTT.
        Gibt die Anzahl erfolgreich gesetzter States zurück.
        satellite_device: Name des Satelliten für TTS-Feedback (leer = kein Feedback)
        trust_level: Trust-Level des anfragenden Users (#366), siehe may_set(). Pflicht,
          damit kein Aufrufer die Prüfung versehentlich auslässt.
        denied: wird um die Labels der Geräte ergänzt, die wegen zu niedrigem Trust-Level
          übersprungen wurden — bei Sammelbefehlen werden nur diese ausgelassen.
        requester_name: Anzeigename des aufgelösten Sprechers (VoiceID oder linked Account),
          leer wenn unbekannt/Gast — für personalisierte Antwort-Varianten (#373). Fließt beim
          Satelliten-Pfad in die Pending-Entry (für _fire_feedback), beim Text-Pfad direkt in
          den Aufrufer zurück (der baut die Antwort selbst).
        wait_confirm: synchron bis zu self._confirm_timeout auf ioBroker-Bestätigung warten
          (#373, Text-Kanäle — der Satelliten-Pfad bekommt sein Feedback weiterhin asynchron
          über satellite_device/_feedback_cb, beide Mechanismen können unabhängig voneinander
          für denselben Aufruf aktiv sein).
        offline: wird um die Labels der Geräte ergänzt, die innerhalb des Timeouts nicht
          bestätigt haben (nur relevant mit wait_confirm=True).
        unsupported: wird um je einen fertigen Satz für die Geräte ergänzt, denen die gefragte
          Fähigkeit fehlt (State nicht vorhanden) oder die sie nur lesen können (nicht
          schreibbar) — ehrliche Antwort statt stillem Ignorieren (#387). Die Aufrufer
          verwenden sie nur, wenn gar nichts gesetzt wurde (Rückgabe 0).
        """
        if intent.name == "Unknown":
            log.debug("execute: Intent 'Unknown', nichts zu tun.")
            return 0

        if not intent.room and not intent.device_id:
            log.warning("execute: Kein Raum erkannt.")
            return 0

        if self._controller is not None:
            return self._execute_slots(
                intent, satellite_device, trust_level=trust_level, denied=denied, requester_name=requester_name,
                wait_confirm=wait_confirm, offline=offline, unsupported=unsupported,
            )

        targets: list[Device] = []

        if intent.device_id:
            dev = self._devices_by_id.get(intent.device_id)
            if dev:
                targets = [dev]
        elif intent.device_key and intent.room_id and intent.room_id in self.devices:
            # Gerät war raumübergreifend mehrdeutig (gleicher Gerätename in mehreren
            # Räumen) und wurde erst per Raum-Rückfrage aufgelöst — device_id war zum
            # Parse-Zeitpunkt noch nicht bekannt, jetzt im bestätigten Raum per Key
            # nachschlagen (#268). Scan statt Dict-Lookup, weil self.devices[room] seit
            # #275 nach device_id geschlüsselt ist, nicht mehr nach dem NLU-Suchbegriff.
            dev = next((d for d in self.devices[intent.room_id].values() if d.key == intent.device_key), None)
            if dev:
                targets = [dev]
                intent.device_id = dev.id
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
            f"execute: {intent.name}, {len(targets)} Gerät(e) gefunden, "
            f"state='{state_key}', value={value!r}"
        )

        count = 0
        deadline = time.monotonic() + self._confirm_timeout
        confirm_waits: list[tuple[str, threading.Event, str]] = []
        for dev in targets:
            state_id = dev.states.get(state_key)
            if not state_id:
                log.debug(f"  {dev.name}: State '{state_key}' nicht vorhanden, übersprungen.")
                if unsupported is not None:
                    unsupported.append(self._unsupported_text(dev, intent, state_key))
                continue
            # Ein schreibgeschützter State (z.B. ein nur lesbarer Helligkeitswert) macht das Gerät
            # nicht steuerbar. Nur bei ausdrücklichem writable=False, ein fehlender Eintrag gilt
            # als unbekannt und wird wie bisher gesetzt.
            if dev.state_writable.get(state_key) is False:
                log.debug(f"  {dev.name}: State '{state_key}' ist schreibgeschützt, übersprungen.")
                if unsupported is not None:
                    unsupported.append(self._unsupported_text(dev, intent, state_key))
                continue
            # Invertierte Rolladen/Markisen (#270): nur die semantischen "öffnen"/
            # "schließen"-Grenzwerte werden pro Gerät umgerechnet, nie ein explizit
            # genannter Prozentwert (Design-Entscheidung 1) — sonst würde eine ioBroker-
            # Visualisierung einen anderen Wert zeigen als der User genannt hat.
            if not self.may_set(state_id, trust_level):
                log.info(f"  {dev.name}: Trust-Level {trust_level} < {self._required_trust_by_state[state_id]}, übersprungen.")
                if denied is not None:
                    denied.append(f"{dev.name} im {dev.room_display_name}")
                continue
            dev_value = value
            if state_key == "level" and intent.is_open_close and dev.inverted:
                dev_value = 100 - dev_value
            label = f"{dev.name} im {dev.room_display_name}"

            # Bestätigung *vor* dem eigentlichen set_state() registrieren (#373) — sonst
            # könnte eine sehr schnelle/synchrone Bestätigung ankommen, bevor überhaupt
            # jemand zuhört, und würde stillschweigend verloren gehen.
            confirm_event = None
            if wait_confirm:
                confirm_event = threading.Event()
                with self._confirm_lock:
                    self._confirm_waiters[state_id] = {"expected": dev_value, "event": confirm_event, "success": False}
            if satellite_device and self._feedback_cb:
                with self._pending_lock:
                    self._pending[state_id] = {
                        "expected":       dev_value,
                        "device":         satellite_device,
                        "deadline":       deadline,
                        "label":          label,
                        "confirmed":      False,
                        "requester_name": requester_name,
                    }

            if self.set_state(state_id, dev_value):
                count += 1
                if wait_confirm:
                    confirm_waits.append((state_id, confirm_event, label))
            else:
                # Befehl kam nie raus — nichts zu bestätigen, Registrierung zurückrollen.
                if wait_confirm:
                    with self._confirm_lock:
                        self._confirm_waiters.pop(state_id, None)
                if satellite_device and self._feedback_cb:
                    with self._pending_lock:
                        self._pending.pop(state_id, None)

        for state_id, event, label in confirm_waits:
            confirmed = event.wait(max(0.0, deadline - time.monotonic()))
            with self._confirm_lock:
                waiter = self._confirm_waiters.pop(state_id, None)
            if not confirmed or not (waiter and waiter["success"]):
                if offline is not None:
                    offline.append(label)

        return count

    @staticmethod
    def _unsupported_text(dev: "Device", intent: "Intent", state_key: str) -> str:
        """Der ehrliche Satz, wenn ein Gerät das Gefragte nicht kann (#387)."""
        label = f"{dev.name} im {dev.room_display_name}"
        return IoBrokerClient._unsupported_sentence(label, state_key, intent.is_open_close or dev.category == "blind")

    @staticmethod
    def _unsupported_sentence(label: str, state_key: str, open_close: bool) -> str:
        if state_key == "on":
            return f"{label} lässt sich nicht ein- oder ausschalten."
        if state_key == "level":
            if open_close:
                return f"{label} lässt sich nicht öffnen oder schließen."
            return f"{label} lässt sich nicht dimmen."
        if state_key == "color":
            return f"{label} lässt sich nicht umfärben."
        if state_key == "colorTemp":
            return f"{label} lässt sich nicht auf Warm- oder Kaltweiß stellen."
        if state_key == "expected":
            return f"{label} hat keine einstellbare Solltemperatur."
        if state_key == "mode":
            return f"{label} hat keinen einstellbaren Modus."
        if state_key == "fanSpeed":
            return f"{label} hat keine einstellbare Lüfterstufe."
        return f"{label} kann das nicht."

    # ------------------------------------------------------------------
    # Steuern über die typisierte Registry (#387, Schritt 4)

    # Kelvin für "warm"/"kalt" (die Skala von SLOT_KIND_COLOR_TEMPERATURE, siehe device_model.proto)
    _WHITE_KELVIN = {"warm": 2700, "kalt": 6500}
    _LEGACY_KEY = {
        pb.SLOT_KIND_ON: "on", pb.SLOT_KIND_BRIGHTNESS: "level", pb.SLOT_KIND_POSITION: "level",
        pb.SLOT_KIND_COLOR: "color", pb.SLOT_KIND_COLOR_TEMPERATURE: "colorTemp",
        pb.SLOT_KIND_TARGET_TEMPERATURE: "expected", pb.SLOT_KIND_MODE: "mode", pb.SLOT_KIND_FAN_SPEED: "fanSpeed",
    }

    def _resolve_slot_targets(self, intent: "Intent") -> list:
        """Die Geräte der Registry, die ein Intent trifft: ein bestimmtes Gerät, ein per Key im
        bestätigten Raum nachgeschlagenes (#268) oder alle Geräte eines Raums (nach Kategorie)."""
        from .nlu_devices import categories_of, to_nlu_device   # lazy: nlu_devices importiert dieses Modul
        if intent.device_id:
            device = self._registry.get(intent.device_id)
            return [device] if device else []
        if intent.device_key and intent.room_id and self._registry.devices_in_room(intent.room_id):
            for device in self._registry.devices_in_room(intent.room_id):
                if to_nlu_device(device).key == intent.device_key:
                    intent.device_id = device.device_id
                    return [device]
            return []
        devices = self._registry.devices_in_room(intent.room_id or "")
        if intent.category_filter:
            devices = [d for d in devices if intent.category_filter in categories_of(d)]
        return devices

    def _intent_to_slot_and_value(self, intent: "Intent", device) -> tuple:
        name = intent.name
        if name == "TurnOn":
            return pb.SLOT_KIND_ON, True
        if name == "TurnOff":
            return pb.SLOT_KIND_ON, False
        if name == "SetLevel":
            is_cover = intent.is_open_close or device.device_class == pb.DEVICE_CLASS_COVER
            return (pb.SLOT_KIND_POSITION if is_cover else pb.SLOT_KIND_BRIGHTNESS), intent.value
        if name == "SetColor":
            if isinstance(intent.value, str) and intent.value.startswith("#"):
                return pb.SLOT_KIND_COLOR, intent.value
            kelvin = self._WHITE_KELVIN.get(intent.value)
            return (pb.SLOT_KIND_COLOR_TEMPERATURE, kelvin) if kelvin else (None, None)
        if name == "SetTemperature":
            return pb.SLOT_KIND_TARGET_TEMPERATURE, intent.value
        if name == "SetMode":
            return pb.SLOT_KIND_MODE, intent.value
        if name == "SetFanSpeed":
            return pb.SLOT_KIND_FAN_SPEED, intent.value
        return None, None

    def _execute_slots(
        self, intent: "Intent", satellite_device: str, *, trust_level: Optional[int], denied, requester_name: str,
        wait_confirm: bool, offline, unsupported,
    ) -> int:
        """execute() über die Registry: Ziel und Slot-Art aus dem Intent, geschrieben über
        DeviceController.set_slot (Trust pro Slot). Bestätigungen wie im State-basierten Pfad,
        nur der Schlüssel unterscheidet sich: die state_id bei einem v1-Gerät, `device#slot` bei
        einem v2-Gerät (dort bestätigt ein SlotUpdate mit ack, siehe handle_slot_ack)."""
        from .device_control import _v1_wire_value   # lazy: device_control importiert dieses Modul
        targets = self._resolve_slot_targets(intent)
        if not targets:
            log.warning(
                f"execute: Keine Geräte für Raum '{intent.room}'"
                + (f" / Gerät '{intent.device}'" if intent.device else "")
                + " gefunden."
            )
            return 0
        rooms = self._room_names()
        log.info(f"execute: {intent.name}, {len(targets)} Gerät(e) gefunden, value={intent.value!r}")

        count = 0
        deadline = time.monotonic() + self._confirm_timeout
        confirm_waits: list[tuple[str, threading.Event, str]] = []
        for dev in targets:
            kind, value = self._intent_to_slot_and_value(intent, dev)
            if kind is None:
                log.warning(f"execute: Intent '{intent.name}' mit Wert {intent.value!r} nicht auf einen Slot abbildbar")
                continue
            label = f"{dev.name} im {rooms.get(dev.room, dev.room)}"
            slot = dev.slot_of_kind(kind)
            if slot is None or not slot.writable:
                log.debug(f"  {dev.name}: Slot {pb.SlotKind.Name(kind)} fehlt oder ist schreibgeschützt, übersprungen.")
                if unsupported is not None:
                    open_close = intent.is_open_close or dev.device_class == pb.DEVICE_CLASS_COVER
                    unsupported.append(self._unsupported_sentence(label, self._LEGACY_KEY.get(kind, ""), open_close))
                continue
            # Invertierte Rolladen/Markisen (#270): nur die semantischen "öffnen"/"schließen"-Grenzwerte
            # sind im Slot kanonisch (100 = offen) und werden vom DeviceController zurückgerechnet. Ein
            # ausdrücklich genannter Prozentwert geht unverändert an den Aktor (Design-Entscheidung 1),
            # dafür hier vorab gespiegelt.
            if kind == pb.SLOT_KIND_POSITION and slot.inverted and not intent.is_open_close:
                value = 100 - value
            try:
                slot_value = slot_value_to_pb(kind, value)
            except ValueError as exc:
                log.warning(f"  {dev.name}: {exc}")
                continue
            if dev.origin == "v1":
                key, expected = slot.state_id, _v1_wire_value(slot, slot_value)
            else:
                key, expected = f"{dev.device_id}#{slot.slot_id}", slot_value_from_pb(slot_value)

            # Bestätigung *vor* dem Senden registrieren (#373), sonst geht eine sehr schnelle
            # Bestätigung verloren.
            confirm_event = None
            if wait_confirm:
                confirm_event = threading.Event()
                with self._confirm_lock:
                    self._confirm_waiters[key] = {"expected": expected, "event": confirm_event, "success": False}
            if satellite_device and self._feedback_cb:
                with self._pending_lock:
                    self._pending[key] = {
                        "expected": expected, "device": satellite_device, "deadline": deadline, "label": label,
                        "confirmed": False, "requester_name": requester_name,
                    }

            sent = False
            try:
                sent = self._controller.set_slot(dev.device_id, slot.slot_id, value, trust_level=trust_level).sent
            except TrustLevelDenied:
                log.info(f"  {dev.name}: Trust-Level {trust_level} reicht nicht, übersprungen.")
                if denied is not None:
                    denied.append(label)
            if sent:
                count += 1
                if wait_confirm:
                    confirm_waits.append((key, confirm_event, label))
            else:
                # Befehl kam nie raus — nichts zu bestätigen, Registrierung zurückrollen.
                if wait_confirm:
                    with self._confirm_lock:
                        self._confirm_waiters.pop(key, None)
                if satellite_device and self._feedback_cb:
                    with self._pending_lock:
                        self._pending.pop(key, None)

        for key, event, label in confirm_waits:
            confirmed = event.wait(max(0.0, deadline - time.monotonic()))
            with self._confirm_lock:
                waiter = self._confirm_waiters.pop(key, None)
            if not confirmed or not (waiter and waiter["success"]):
                if offline is not None:
                    offline.append(label)
        return count

    def handle_slot_ack(self, update: "pb.SlotUpdate") -> None:
        """Ein SlotUpdate mit ack=true eines v2-Adapters bestätigt einen gesendeten Befehl
        (Gegenstück zu handle_state_update bei v1-Adaptern)."""
        if update.ack:
            self._confirm(f"{update.device_id}#{update.slot_id}", slot_value_from_pb(update.value))

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
        if self._answerer is not None:
            return self._answerer(intent)
        if intent.device_id:
            targets = [self._devices_by_id[intent.device_id]]
            room_label = intent.room
        elif intent.room:
            room_devices = list(self.devices.get(intent.room_id or "", {}).values())
            targets = room_devices
            if intent.category_filter:
                targets = [d for d in targets if d.category == intent.category_filter]
            room_label = intent.room
            if not room_devices:
                return f"Ich kenne keine Geräte im {intent.room}."
            if not targets:
                # Raum ist bekannt und hat Geräte — nur keins der gefragten Kategorie (#276).
                # Andere Meldung als der "Raum komplett unbekannt"-Fall oben, sonst klingt es
                # so als würde Hannah den Raum gar nicht kennen.
                return f"Ich kenne keine {_category_label(intent.category_filter)} im {intent.room}."
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

    def _category_query_applies(self, category: str, qs: Optional[str]) -> bool:
        """Ob die Kategorie-basierte Sensor-Antwort für diese Abfrage greifen soll.

        "socket" hat anders als die übrigen _CATEGORY_STATES-Kategorien zusätzlich
        einen regulären on/off-Schaltzustand — die Watt-Antwort darf normale
        an/aus-Abfragen nicht kapern, greift nur wenn explizit nach dem
        Verbrauch gefragt wurde (qs=="power", #121)."""
        if category not in self._CATEGORY_STATES:
            return False
        if category == "socket":
            return qs == "power"
        return True

    def _summarize(self, targets: list, qs: Optional[str], room_label: str) -> Optional[str]:
        """Fasst mehrere Geräte in einem Raum zusammen."""
        # Sensor-Kategorien direkt beschreiben (haben kein on/off)
        categories = {dev.category for dev in targets}
        if len(categories) == 1 and self._category_query_applies(list(categories)[0], qs):
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
        if len(categories) == 1 and self._category_query_applies(list(categories)[0], qs):
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
        if category_filter and self._category_query_applies(category_filter, qs):
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
        "socket": [
            ("power", "Watt", None),
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
                # Invertierte Rolladen/Markisen (#270): Ansage nennt den kanonischen
                # Wert (0%=zu/100%=auf), nicht den rohen Aktorwert (Design-Entscheidung 3).
                if category == "blind" and state_key == "level" and dev.inverted:
                    val = 100 - val
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
        if self._category_query_applies(dev.category, qs):
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

    def handle_state_update(self, state_id: str, raw: str, canonical_key: Optional[str] = None):
        """
        Callback für eingehende State-Updates aus ioBroker.
        Parst den Rohwert, prüft Pending-Confirmations und pflegt den alten Gerätebaum.
        States ohne Raum landen im _state_cache.

        Bestätigungen hängen an der state_id, nicht am Gerätebaum (#387): sie greifen auch dann,
        wenn das Gerät nur in der typisierten Registry steht. Den Wert der Registry pflegt
        main.py (legacy_devices.apply_state_update).

        Der alte Gerätebaum (`devices`, `Device.current`) ist Legacy: nichts Sichtbares liest ihn
        noch, er bleibt bis zur Abkündigung erhalten. canonical_key kommt aus AgentStateUpdate
        (hannah-proto >= 4.8); fehlt er (None oder leer, ältere Adapter), wird er dort über den
        State-Suffix aufgelöst.
        """
        if state_id in self._state_cache:
            self._state_cache[state_id] = self._parse_payload(raw)
            log.debug(f"State-Cache: {state_id} = {self._state_cache[state_id]!r}")
            return

        value = self._parse_payload(raw)
        self._update_legacy_tree(state_id, value, canonical_key)
        self._confirm(state_id, value)

    def _update_legacy_tree(self, state_id: str, value, canonical_key: Optional[str]) -> None:
        device_id = ".".join(state_id.rsplit(".", 1)[:-1])
        state_suffix = state_id.rsplit(".", 1)[-1]
        device = self._devices_by_id.get(device_id)
        if not device:
            return

        canon = canonical_key or self._suffix_to_canon.get(state_suffix)
        if not canon:
            if state_suffix not in self._warned_suffixes:
                self._warned_suffixes.add(state_suffix)
                log.warning(
                    f"Unbekannter State-Suffix '{state_suffix}' (state_id={state_id}) — "
                    f"Adapter liefert kein canonical_key und der Suffix fehlt in "
                    f"DEFAULT_IOBROKER_STATE_NAMES, Live-Update wird im alten Gerätebaum verworfen."
                )
            return

        device.current[canon] = value
        log.debug(f"Cache: {device.name}.{canon} = {value!r}")

    def _confirm(self, key: str, value) -> None:
        """Prüft einen eingetroffenen Wert gegen wartende Bestätigungen: die state_id bei einem
        v1-Gerät, `device#slot` bei einem v2-Gerät."""
        state_id = key
        # Pending-Confirmation prüfen (Satelliten-Pfad, asynchron)
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

        # Confirm-Waiter prüfen (Text-Kanäle, synchron, #373) — unabhängig vom Pending-Zweig
        # oben, execute(wait_confirm=True) wartet direkt auf dieses Event.
        with self._confirm_lock:
            waiter = self._confirm_waiters.get(state_id)
            if waiter:
                waiter["success"] = (value == waiter["expected"])
                waiter["event"].set()

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
            text = responses.success("ok", pending.get("requester_name", ""))
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
                            responses.offline(f"{pending['label']} antwortet nicht — möglicherweise offline."),
                        )

    def control_direct(self, device_id: str, state_key: str, raw_value: str, *, trust_level: Optional[int]) -> bool:
        """
        Setzt einen Device-State direkt ohne NLU-Umweg (für gRPC-Menü-Steuerung).
        device_id  : Device.id, z.B. "javascript.0.virtualDevice.Licht.EG.Wohnzimmer.DeckeSeite"
        state_key  : kanonischer Key, z.B. "on", "level", "color"
        raw_value  : String-serialisierter Wert, z.B. "true", "50", "#FF0000"
        trust_level: Trust-Level des anfragenden Users (#366), siehe may_set()
        Wirft TrustLevelDenied, wenn das Trust-Level nicht reicht.

        Mit registriertem Slot-Controller (#387) läuft das über die typisierte Registry: der
        State-Key wird auf den Slot des Geräts abgebildet und über DeviceController.set_slot
        geschrieben, so auch bei Geräten eines hannah.v2-Adapters.
        """
        if self._controller is not None:
            return self._control_direct_slots(device_id, state_key, raw_value, trust_level)
        device = self._devices_by_id.get(device_id)
        if not device:
            log.warning(f"control_direct: Gerät {device_id!r} nicht gefunden")
            return False
        state_id = device.states.get(state_key)
        if not state_id:
            log.warning(f"control_direct: State {state_key!r} für {device.name!r} nicht vorhanden")
            return False
        if not self.may_set(state_id, trust_level):
            log.info(f"control_direct: Trust-Level {trust_level} reicht nicht für {state_id}")
            raise TrustLevelDenied(state_id)
        value = self._parse_payload(raw_value)
        if self.set_state(state_id, value):
            # Update cache immediately — the gRPC roundtrip is async, so GetDevices
            # called right after ControlDevice would otherwise return the stale state.
            device.current[state_key] = value
            return True
        return False

    def _control_direct_slots(self, device_id: str, state_key: str, raw_value: str, trust_level: Optional[int]) -> bool:
        """control_direct() über die Registry. Der State-Key eines hannah.v1-Clients ist der Key, den
        ihm die Übersetzung der Lib für den Slot gegeben hat (GetDevices), darüber wird der Slot
        gefunden."""
        from hannah_grpc.translate import translate   # lazy: nur dieser Weg braucht die Lib
        from .typed_devices import device_info_to_pb
        device = self._registry.get(device_id)
        if device is None:
            log.warning(f"control_direct: Gerät {device_id!r} nicht gefunden")
            return False
        keys = translate(device_info_to_pb(device), "v1", strict=False).states
        slot_id = next((slot.slot_id for slot, key in zip(device.slots.values(), keys) if key == state_key), None)
        if slot_id is None and state_key in device.slots:
            slot_id = state_key
        if slot_id is None:
            log.warning(f"control_direct: State {state_key!r} für {device.name!r} nicht vorhanden")
            return False
        try:
            result = self._controller.set_slot(device_id, slot_id, self._parse_payload(raw_value), trust_level=trust_level)
        except ValueError as exc:
            log.warning(f"control_direct: {device.name!r}/{slot_id}: {exc}")
            return False
        return result.found and result.sent

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
                    "state_types":       dict(dev.state_types),
                    "state_enum_values": {k: dict(v) for k, v in dev.enum_values.items()},
                    "state_writable":    dict(dev.state_writable),
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

        if self._registry is not None:
            target = self._registry.lookup_state(state_id)
            slot = None
            if target is not None:
                registered = self._registry.get(target[0])
                slot = registered.slots.get(target[1]) if registered else None
            if slot is not None:
                value = slot.value
                if value is None:
                    return None
                return str(int(value)) if isinstance(value, float) and value == int(value) else str(value)

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
