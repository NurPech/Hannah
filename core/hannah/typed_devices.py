"""
Typisierte Geräte-Registry (#385, Teil 1 von #383).

Core hält Geräte in den Klassen und Slots von `hannah.v2` (`device_model.proto`):
eine Klasse pro Verb-Familie (Light, Socket, Thermostat, ...), die Fähigkeiten eines
Geräts sind schlicht seine Slots. Die Registry steht **neben** dem State-basierten
`Device`-Baum aus `iobroker.py`; NLU, `execute` und die Steuerung laufen bis #386 und
#387 weiter auf diesem Baum.

Zwei Quellen füllen sie:

- ein `hannah.v2`-Adapter schickt `TypedDeviceSnapshot`, `SlotUpdate` und
  `DeviceAvailability` (`handle_typed_snapshot` & Co.),
- ein `hannah.v1`-Adapter schickt den State-basierten Snapshot, den
  `legacy_devices.py` in typisierte Geräte klassifiziert.

Werte liegen in den Skalen von `SlotKind` (Helligkeit 0-100, Farbe RGB, Farbtemperatur
Kelvin, Rollladen 100 = offen, ...). Ein v2-Adapter normalisiert selbst, bei einem
v1-Gerät übernimmt das die Legacy-Klassifikation.
"""
import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Union

from hannah_proto.v2 import hannah_pb2 as pb

log = logging.getLogger(__name__)

SlotValue = Optional[Union[bool, float, int, str]]

ORIGIN_V1 = "v1"
ORIGIN_V2 = "v2"

# Welche Art welchen Wert trägt (Skalen siehe SlotKind in device_model.proto)
BOOL_KINDS = {
    pb.SLOT_KIND_ON, pb.SLOT_KIND_OPEN, pb.SLOT_KIND_MOTION, pb.SLOT_KIND_STOP, pb.SLOT_KIND_GENERIC_BOOL,
}
TEXT_KINDS = {pb.SLOT_KIND_GENERIC_TEXT, pb.SLOT_KIND_MODE, pb.SLOT_KIND_FAN_SPEED}

_SLOT_KIND_PREFIX = "SLOT_KIND_"


def slot_id_for_kind(kind: int) -> str:
    """Empfohlene Slot-ID einer Standardart: der Kind-Name ohne Präfix, klein ("brightness")."""
    return pb.SlotKind.Name(kind)[len(_SLOT_KIND_PREFIX):].lower()


def slot_value_from_pb(value: "pb.SlotValue") -> SlotValue:
    """Python-Wert eines SlotValue, None wenn keiner gesetzt ist (Wert unbekannt)."""
    which = value.WhichOneof("value")
    return getattr(value, which) if which else None


def slot_value_to_pb(kind: int, value) -> "pb.SlotValue":
    """Ein Python-Wert als SlotValue für eine Slot-Art. ValueError, wenn der Wert nicht zur
    Art passt: Core rät nicht (ein Bool ist keine Helligkeit), der Adapter bekommt nur
    Werte, die er in der Skala der Art versteht."""
    if value is None:
        raise ValueError("kein Wert")
    if kind in BOOL_KINDS:
        if not isinstance(value, bool):
            raise ValueError(f"{pb.SlotKind.Name(kind)} erwartet true/false, nicht {value!r}")
        return pb.SlotValue(boolean=value)
    if kind in TEXT_KINDS:
        return pb.SlotValue(text=str(value))
    if kind == pb.SLOT_KIND_COLOR:
        if isinstance(value, str) and value.startswith("#"):
            try:
                value = int(value[1:], 16)
            except ValueError:
                raise ValueError(f"keine Farbe: {value!r}") from None
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFFFFFF:
            raise ValueError(f"COLOR erwartet RGB (0xRRGGBB), nicht {value!r}")
        return pb.SlotValue(rgb=value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{pb.SlotKind.Name(kind)} erwartet eine Zahl, nicht {value!r}")
    return pb.SlotValue(number=float(value))


@dataclass
class Slot:
    slot_id: str
    kind: int                                    # pb.SlotKind
    value: SlotValue = None                      # None = unbekannt
    writable: bool = False
    unit: str = ""
    label: str = ""
    required_trust_level: Optional[int] = None   # None = keine Einschränkung, bewusst verschieden von 0
    # Nur bei Geräten eines v1-Adapters: der ioBroker-State hinter dem Slot, und ob der
    # Aktor invertiert arbeitet (Rollladen 0 % = offen). Der Wert ist schon auf die
    # Skala der SlotKind normalisiert, `inverted` braucht erst das Schreiben (#386).
    state_id: str = ""
    inverted: bool = False
    # Die Werte, die das Gerät annimmt (SLOT_KIND_MODE/FAN_SPEED), leer = keine Angabe
    options: list = field(default_factory=list)


@dataclass
class TypedDevice:
    device_id: str
    name: str
    room: str                                    # room_id (Enum-ID-Segment)
    floor: str = ""
    device_class: int = pb.DEVICE_CLASS_UNSPECIFIED   # pb.DeviceClass
    slots: dict = field(default_factory=dict)    # slot_id -> Slot, in Adapter-Reihenfolge
    available: bool = True
    origin: str = ORIGIN_V2
    subtype: int = pb.DEVICE_SUBTYPE_UNSPECIFIED   # pb.DeviceSubtype, z.B. Fenster/Tür bei einem Kontakt

    def slot_of_kind(self, kind: int) -> Optional[Slot]:
        """Der erste Slot einer Art, None wenn das Gerät sie nicht hat (Capability-Prüfung)."""
        return next((s for s in self.slots.values() if s.kind == kind), None)

    def has_slot(self, kind: int) -> bool:
        return self.slot_of_kind(kind) is not None


def _slot_value_or_unset(slot: "Slot") -> "pb.SlotValue":
    if slot.value is None:
        return pb.SlotValue()
    try:
        return slot_value_to_pb(slot.kind, slot.value)
    except ValueError:
        return pb.SlotValue()


def device_info_to_pb(device: TypedDevice) -> "pb.DeviceInfo":
    """Ein Gerät für die Steuer-Menüs (`GetDevices`): Klasse, Slots mit Werten, Erreichbarkeit."""
    info = pb.DeviceInfo(id=device.device_id, name=device.name, device_class=device.device_class,
                         available=device.available, subtype=device.subtype)
    for slot in device.slots.values():
        message = info.slots.add(
            slot_id=slot.slot_id, kind=slot.kind, value=_slot_value_or_unset(slot),
            writable=slot.writable, unit=slot.unit, label=slot.label, options=slot.options,
        )
        if slot.required_trust_level is not None:
            message.required_trust_level = slot.required_trust_level
    return info


def device_from_pb(device: "pb.TypedDevice") -> TypedDevice:
    """Ein TypedDevice eines v2-Adapters in die interne Form."""
    slots = {}
    for s in device.slots:
        if s.slot_id in slots:
            log.warning(f"[devices] {device.device_id}: Slot-ID {s.slot_id!r} kommt doppelt vor, der erste gilt")
            continue
        slots[s.slot_id] = Slot(
            slot_id=s.slot_id,
            kind=s.kind,
            value=slot_value_from_pb(s.value),
            writable=s.writable,
            unit=s.unit,
            label=s.label,
            required_trust_level=s.required_trust_level if s.HasField("required_trust_level") else None,
            options=list(s.options),
        )
    return TypedDevice(
        device_id=device.device_id, name=device.name, room=device.room, floor=device.floor,
        device_class=device.device_class, slots=slots, available=device.available, origin=ORIGIN_V2,
        subtype=device.subtype,
    )


class DeviceRegistry:
    """Die typisierten Geräte aller verbundenen Adapter, nach `device_id`.

    Ein Snapshot ersetzt nur die Geräte seiner eigenen Herkunft (v1/v2): zwei Adapter
    unterschiedlicher Generation überschreiben sich nicht gegenseitig. Name und Raum sind
    Pflicht, ein Gerät ohne eines von beiden wird nicht geführt.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._devices: dict[str, TypedDevice] = {}
        self._state_index: dict[str, tuple[str, str]] = {}   # state_id -> (device_id, slot_id), nur v1
        self._listeners: list[Callable[[], None]] = []

    # ------------------------------------------------------------------
    # Lesen

    def get(self, device_id: str) -> Optional[TypedDevice]:
        with self._lock:
            return self._devices.get(device_id)

    def devices(self) -> list[TypedDevice]:
        with self._lock:
            return list(self._devices.values())

    def devices_in_room(self, room: str) -> list[TypedDevice]:
        with self._lock:
            return [d for d in self._devices.values() if d.room == room]

    def lookup_state(self, state_id: str) -> Optional[tuple[str, str]]:
        """`(device_id, slot_id)` zu einem ioBroker-State eines v1-Geräts, sonst None."""
        with self._lock:
            return self._state_index.get(state_id)

    def __len__(self) -> int:
        with self._lock:
            return len(self._devices)

    def add_listener(self, listener: Callable[[], None]) -> None:
        """`listener()` läuft nach jeder Änderung der Geräte-Menge (Snapshot), nicht bei Werten.
        Die NLU baut damit ihren Suchindex neu."""
        self._listeners.append(listener)

    # ------------------------------------------------------------------
    # Schreiben

    def replace(self, origin: str, devices: Iterable[TypedDevice]) -> int:
        """Ersetzt alle Geräte der Herkunft `origin` durch `devices`. Gibt die Anzahl
        der geführten Geräte zurück (Geräte ohne Name oder Raum fallen raus)."""
        kept: dict[str, TypedDevice] = {}
        dropped = 0
        for device in devices:
            if not device.device_id or not device.name or not device.room:
                dropped += 1
                log.debug(f"[devices] Gerät {device.device_id!r} ({device.name!r}) ohne Name oder Raum, nicht geführt")
                continue
            if device.device_id in kept:
                log.warning(f"[devices] Geräte-ID {device.device_id!r} kommt doppelt vor, die erste gilt")
                continue
            device.origin = origin
            kept[device.device_id] = device
        with self._lock:
            self._devices = {
                **{i: d for i, d in self._devices.items() if d.origin != origin},
                **kept,
            }
            self._rebuild_state_index()
        log.info(f"[devices] {len(kept)} typisierte Geräte ({origin})" + (f", {dropped} ohne Name/Raum übersprungen" if dropped else ""))
        for listener in self._listeners:
            try:
                listener()
            except Exception:
                log.exception("[devices] Listener nach Snapshot fehlgeschlagen")
        return len(kept)

    def set_slot_value(self, device_id: str, slot_id: str, value: SlotValue) -> bool:
        """Setzt den Wert eines Slots. False, wenn Gerät oder Slot unbekannt sind."""
        with self._lock:
            slot = self._slot(device_id, slot_id)
            if slot is None:
                return False
            slot.value = value
            return True

    def set_available(self, device_id: str, available: bool) -> bool:
        with self._lock:
            device = self._devices.get(device_id)
            if device is None:
                return False
            device.available = available
            return True

    # ------------------------------------------------------------------
    # hannah.v2-Adapter (Callbacks des Servicers)

    def handle_typed_snapshot(self, devices: Iterable["pb.TypedDevice"]) -> int:
        return self.replace(ORIGIN_V2, [device_from_pb(d) for d in devices])

    def handle_slot_update(self, update: "pb.SlotUpdate") -> None:
        if not self.set_slot_value(update.device_id, update.slot_id, slot_value_from_pb(update.value)):
            log.debug(f"[devices] SlotUpdate für unbekanntes Gerät/Slot {update.device_id!r}/{update.slot_id!r}")

    def handle_device_availability(self, update: "pb.DeviceAvailability") -> None:
        if not self.set_available(update.device_id, update.available):
            log.debug(f"[devices] DeviceAvailability für unbekanntes Gerät {update.device_id!r}")

    # ------------------------------------------------------------------
    # Intern

    def _slot(self, device_id: str, slot_id: str) -> Optional[Slot]:
        device = self._devices.get(device_id)
        return device.slots.get(slot_id) if device else None

    def _rebuild_state_index(self) -> None:
        self._state_index = {
            slot.state_id: (device.device_id, slot.slot_id)
            for device in self._devices.values()
            for slot in device.slots.values()
            if slot.state_id
        }
