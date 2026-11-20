import logging
import re
import threading
import time
from typing import TYPE_CHECKING, Callable, Iterable, Optional

from hannah_proto.v2 import hannah_pb2 as pb

from . import responses
from .typed_devices import slot_value_from_pb, slot_value_to_pb

if TYPE_CHECKING:
    from .nlu import Intent

log = logging.getLogger(__name__)


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


class IoBrokerClient:
    """
    Die Schnittstelle von Core zu den ioBroker-States über den Adapter: schreibt States, bestätigt
    Befehle (Pending-Confirmations und synchrone Waiter), hält den Cache der States ohne Raum
    (Wetter, Auto, ...) und führt Sprachbefehle über die typisierte Registry aus
    (hannah.typed_devices, hannah.device_control). Geräte hält sie nicht selbst: den State-basierten
    Gerätebaum gibt es seit #522 nicht mehr.
    """

    def __init__(self, cfg: dict):
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

        # Synchroner Confirm-Waiter für execute(wait_confirm=True) (Text-Kanäle, #373) —
        # bewusst getrennt von _pending/_feedback_cb (Satelliten-Pfad, asynchron): beide
        # beobachten denselben state_id-Bestätigungsweg in handle_state_update(), aber mit
        # unterschiedlichem Konsummuster (ein Aufrufer wartet synchron auf genau seine
        # eigenen state_ids, der andere batcht callback-artig pro Satellit).
        self._confirm_waiters: dict[str, dict] = {}
        self._confirm_lock = threading.Lock()

        # Beantwortet Geräte-Abfragen aus der typisierten Registry (#387, hannah.device_answers).
        self._answerer: Optional[Callable[["Intent"], Optional[str]]] = None

        # Steuern über die typisierte Registry und den DeviceController (#387, Schritt 4)
        self._registry = None
        self._controller = None
        self._room_names: Callable[[], dict] = lambda: {}

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
        Verarbeitet die Liste der hannah.v1-AgentDevice-Objekte (State-basierter Pfad der v1-Adapter).
        Hier bleiben nur die States ohne Raum (Wetter, Auto, ...): sie sind der State-Cache für
        get_state_raw(). Die Geräte mit Raum gehören in die typisierte Registry
        (legacy_devices.load_snapshot, main.py), nicht hierher (#522).
        """
        log.info(f"gRPC Snapshot: {len(devices)} Geräte erhalten, verarbeiten...")
        state_cache = {}
        for device in devices:
            if device.room:
                continue
            try:
                state_cache[device.state_id] = self._parse_payload(device.value.value)
            except Exception as e:
                log.warning(f"Fehler beim Verarbeiten von State {device.state_id}: {e}", exc_info=True)
        self._state_cache = state_cache
        if self._state_cache:
            log.info(f"State-Cache: {len(self._state_cache)} rohe States ohne Raum (Wetter, Auto, …)")

    # ------------------------------------------------------------------
    # Intent ausführen

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
        trust_level: Trust-Level des anfragenden Users (#366), geprüft pro Slot im DeviceController.
          Pflicht, damit kein Aufrufer die Prüfung versehentlich auslässt.
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
          bestätigt haben (nur relevant mit wait_confirm=True) und der Geräte, die sich als nicht
          erreichbar melden (#523, dort kam der Befehl gar nicht erst raus).
        unsupported: wird um je einen fertigen Satz für die Geräte ergänzt, denen die gefragte
          Fähigkeit fehlt (State nicht vorhanden) oder die sie nur lesen können (nicht
          schreibbar) oder die sich als nicht erreichbar melden (#523) — ehrliche Antwort
          statt stillem Ignorieren (#387). Die Aufrufer verwenden sie nur, wenn gar nichts
          gesetzt wurde (Rückgabe 0).
        """
        if intent.name == "Unknown":
            log.debug("execute: Intent 'Unknown', nichts zu tun.")
            return 0

        if not intent.room and not intent.device_id:
            log.warning("execute: Kein Raum erkannt.")
            return 0

        if self._controller is None:
            log.warning("execute: Kein Slot-Controller gesetzt, nichts zu tun.")
            return 0
        return self._execute_slots(
            intent, satellite_device, trust_level=trust_level, denied=denied, requester_name=requester_name,
            wait_confirm=wait_confirm, offline=offline, unsupported=unsupported,
        )

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
        DeviceController.set_slot (Trust pro Slot). Bestätigungen laufen über Pending und Waiter, nur
        der Schlüssel unterscheidet sich: die state_id bei einem v1-Gerät, `device#slot` bei
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
                result = self._controller.set_slot(dev.device_id, slot.slot_id, value, trust_level=trust_level)
                sent = result.sent
                if result.unreachable:
                    # #523: das Gerät meldet sich als nicht erreichbar, der Befehl ging nicht raus. Wurde
                    # gar nichts gesetzt, sagt der Aufrufer den Satz aus `unsupported`; bei einem Sammelbefehl,
                    # der andere Geräte erreicht hat, nennt er das Gerät in der Offline-Antwort.
                    log.info(f"  {dev.name}: nicht erreichbar, übersprungen.")
                    if unsupported is not None:
                        unsupported.append(f"{label} ist gerade nicht erreichbar.")
                    if offline is not None:
                        offline.append(label)
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
        if self._answerer is None:
            log.warning("answer_query: Kein Antwortgeber gesetzt, keine Antwort.")
            return None
        return self._answerer(intent)

    def handle_state_update(self, state_id: str, raw: str):
        """
        Callback für eingehende State-Updates aus ioBroker.
        Parst den Rohwert und prüft Pending-Confirmations. States ohne Raum landen im _state_cache.

        Bestätigungen hängen an der state_id, nicht am Gerätebaum (#387): sie greifen auch dann,
        wenn das Gerät nur in der typisierten Registry steht. Den Wert der Registry pflegt
        main.py (legacy_devices.apply_state_update).
        """
        if state_id in self._state_cache:
            self._state_cache[state_id] = self._parse_payload(raw)
            log.debug(f"State-Cache: {state_id} = {self._state_cache[state_id]!r}")
            return

        self._confirm(state_id, self._parse_payload(raw))

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
        device_id  : ID des Geräts in der Registry, z.B. "javascript.0.virtualDevice.Licht.EG.Wohnzimmer.DeckeSeite"
        state_key  : kanonischer Key, z.B. "on", "level", "color"
        raw_value  : String-serialisierter Wert, z.B. "true", "50", "#FF0000"
        trust_level: Trust-Level des anfragenden Users (#366), geprüft pro Slot im DeviceController
        Wirft TrustLevelDenied, wenn das Trust-Level nicht reicht.

        Mit registriertem Slot-Controller (#387) läuft das über die typisierte Registry: der
        State-Key wird auf den Slot des Geräts abgebildet und über DeviceController.set_slot
        geschrieben, so auch bei Geräten eines hannah.v2-Adapters.
        """
        if self._controller is None:
            log.warning("control_direct: Kein Slot-Controller gesetzt, nichts zu tun.")
            return False
        return self._control_direct_slots(device_id, state_key, raw_value, trust_level)

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
