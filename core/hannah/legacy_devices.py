"""
Legacy-Klassifikation (#385): v1-Snapshot → typisierte Geräte.

Ein `hannah.v1`-Adapter meldet State für State (`AgentDevice`: State-ID, Raum, Gerätename,
Typ-Hinweis, kanonischer Key, Schreibbarkeit, Wert). Diese Klassifikation gruppiert die
States eines Geräts und macht daraus die Klasse und die Slots von `hannah.v2`
(`typed_devices.py`). Die Regeln stammen aus dem Konzept #377:

- Funktion/Typ-Hinweis vor Slot-Menge (die Function-Zuordnung war in den Echtdaten besser
  als die reine Rollen-Erkennung)
- `Thermostat` nur mit Sollwert, reine Messgeräte sind `Sensor`, nicht `Socket`
- schreibbare Slots nur zum Steuern: ein Gerät mit schreibgeschütztem `on` wird keine
  Lampe, Steckdose oder Schalter
- Bei mehreren States derselben Art gewinnt nur ein eindeutiger: Comfort/Eco/Offset-
  Sollwerte sind keine Sollwerte, sonst wird nichts gewählt. Was nicht gewählt wird, bleibt
  als Generic-Slot erhalten, nichts geht verloren
- Name und Raum sind Pflicht (Geräte ohne Raum sind rohe States und fehlen hier)

Die v1-Daten kennen keine Einheit und keine Rolle, nur den Typ-Hinweis und den kanonischen
Key des Adapters. Grenzen des v1-Adapters werden hier nicht repariert: der Adapter legt jede
Rolle `level.color.*` auf den Key `color`, auch die Farbtemperatur, ein `color`-State trägt
dann Kelvin statt RGB (NurPech/ioBroker.hannah#210). Das behebt der Adapter, ein v2-Adapter
liefert die Slots fertig.

Die Klassifikation arbeitet duck-typed auf den AgentDevice-Nachrichten, sie braucht keine
v1-Importe.
"""
import logging
import re
from dataclasses import dataclass
from typing import Iterable, Optional

from hannah_proto.v2 import hannah_pb2 as pb

from hannah.iobroker import IoBrokerClient
from hannah.typed_devices import (
    BOOL_KINDS, ORIGIN_V1, TEXT_KINDS, DeviceRegistry, Slot, SlotValue, TypedDevice, slot_id_for_kind,
)

log = logging.getLogger(__name__)

K = pb.SlotKind
C = pb.DeviceClass

# hannah.v1 StateType (shared.proto); v2 kennt ihn nicht mehr
_BOOLEAN, _NUMERIC, _ENUM, _COLOR, _TEXT = 1, 2, 3, 4, 5

_GENERIC_KINDS = {K.SLOT_KIND_GENERIC_NUMBER, K.SLOT_KIND_GENERIC_BOOL, K.SLOT_KIND_GENERIC_TEXT}
_MEASUREMENT_KINDS = {
    K.SLOT_KIND_TEMPERATURE, K.SLOT_KIND_HUMIDITY, K.SLOT_KIND_ILLUMINANCE, K.SLOT_KIND_PRESSURE,
    K.SLOT_KIND_CO2, K.SLOT_KIND_IAQ, K.SLOT_KIND_VOC, K.SLOT_KIND_POWER, K.SLOT_KIND_ENERGY,
    K.SLOT_KIND_MOTION,
}

# kanonischer Key des v1-Adapters → Slot-Art (`level` und `current` hängen vom Typ-Hinweis ab)
_KIND_BY_KEY = {
    "on": K.SLOT_KIND_ON,
    "color": K.SLOT_KIND_COLOR,
    "colorTemp": K.SLOT_KIND_COLOR_TEMPERATURE,
    "expected": K.SLOT_KIND_TARGET_TEMPERATURE,
    "illuminance": K.SLOT_KIND_ILLUMINANCE,
    "open": K.SLOT_KIND_OPEN,
    "iaq": K.SLOT_KIND_IAQ,
    "co2_equiv": K.SLOT_KIND_CO2,
    "voc_equiv": K.SLOT_KIND_VOC,
    "power": K.SLOT_KIND_POWER,
}

# Namensbestandteile (CamelCase/Unterstrich getrennt, klein) als letzte Stufe der Auswahl
_CLIMATE_DTYPE = "climate"
_DOOR_HINTS = ("door", "tuer", "tür")
_WINDOW_HINTS = ("window", "fenster")
_ENUM_KINDS = {K.SLOT_KIND_MODE, K.SLOT_KIND_FAN_SPEED}

_SETPOINT_EXCLUDE = {
    "comfort", "komfort", "eco", "offset", "startup", "night", "nacht", "frost", "boost",
    "vacation", "urlaub", "absenk", "absenkung", "min", "max", "default", "away",
}
_TOKEN = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


def _tokens(name: str) -> set[str]:
    return {t.lower() for t in _TOKEN.findall(name)}


# ------------------------------------------------------------------
# Werte

def normalize_value(kind: int, raw: str, inverted: bool = False) -> SlotValue:
    """Rohwert eines v1-States in die Skala der Slot-Art. None = unbekannt/nicht lesbar."""
    parsed = IoBrokerClient._parse_payload(raw)
    if isinstance(parsed, str):
        if parsed == "" or parsed.lower() == "null":
            return None
        if len(parsed) >= 2 and parsed[0] == '"' and parsed[-1] == '"':
            parsed = parsed[1:-1]

    if kind in BOOL_KINDS:
        if isinstance(parsed, str):
            return parsed.lower() in ("true", "1", "on")
        return bool(parsed)
    if kind in TEXT_KINDS:
        return str(parsed)
    if kind == K.SLOT_KIND_COLOR:
        if isinstance(parsed, str):
            if not parsed.startswith("#"):
                return None
            try:
                return int(parsed[1:], 16) & 0xFFFFFF
            except ValueError:
                return None
        return int(parsed) & 0xFFFFFF

    try:
        number = float(parsed)
    except (TypeError, ValueError):
        return None
    if inverted and kind == K.SLOT_KIND_POSITION:
        number = 100 - number
    return number


def apply_state_update(registry: DeviceRegistry, state_id: str, raw: str) -> bool:
    """Live-Update eines v1-States in die Registry. False, wenn der State zu keinem
    typisierten Gerät gehört (z.B. ein per AgentWatchMore beobachteter State)."""
    target = registry.lookup_state(state_id)
    if target is None:
        return False
    device_id, slot_id = target
    device = registry.get(device_id)
    slot = device.slots.get(slot_id) if device else None
    if slot is None:
        return False
    return registry.set_slot_value(device_id, slot_id, normalize_value(slot.kind, raw, slot.inverted))


# ------------------------------------------------------------------
# Klassifikation

@dataclass
class _State:
    key: str            # kanonischer Key des Adapters, sonst der rohe State-Suffix
    suffix: str
    state_id: str
    raw: str
    state_type: int
    writable: bool
    dtype: str          # Typ-Hinweis des Adapters für diesen State ("light", "blind", ...)
    inverted: bool
    trust: Optional[int]
    options: list       # erlaubte Werte eines Enum-States (Modus, Lüfterstufe), sonst leer


def _preliminary_kind(state: _State, blind: bool, climate: bool) -> Optional[int]:
    key = state.key
    if climate and key == "mode":
        return K.SLOT_KIND_MODE
    if climate and key == "fanSpeed":
        return K.SLOT_KIND_FAN_SPEED
    if key == "level":
        return K.SLOT_KIND_POSITION if blind else K.SLOT_KIND_BRIGHTNESS
    if key == "current":
        if state.dtype == "humidity_sensor":
            return K.SLOT_KIND_HUMIDITY
        if state.dtype == "illuminance_sensor":
            return K.SLOT_KIND_ILLUMINANCE
        return K.SLOT_KIND_TEMPERATURE
    return _KIND_BY_KEY.get(key)


def _resolve_kinds(states: list[_State], blind: bool, climate: bool) -> dict[str, int]:
    """state_id → Slot-Art für die States, die eindeutig eine Standardart tragen. Pro Art
    höchstens ein State: bei mehreren gewinnt nur ein eindeutiger, sonst wird nichts
    gewählt (alle bleiben Generic-Slots)."""
    groups: dict[int, list[_State]] = {}
    for state in states:
        kind = _preliminary_kind(state, blind, climate)
        if kind is not None:
            groups.setdefault(kind, []).append(state)

    chosen: dict[str, int] = {}

    def pick(kind: int, candidates: list[_State]) -> None:
        if len(candidates) == 1:
            chosen[candidates[0].state_id] = kind

    for kind, members in groups.items():
        if len(members) == 1:
            pick(kind, members)
        elif kind == K.SLOT_KIND_TARGET_TEMPERATURE:
            pick(kind, [s for s in members if not (_tokens(s.suffix) & _SETPOINT_EXCLUDE)])
        # alles andere: mehrdeutig, nichts gewählt
    return chosen


def _decide_class(slots: list[Slot], dtypes: set[str]) -> int:
    """Geräteklasse aus Typ-Hinweis und Slot-Menge. Nur Standardslots zählen, nur
    schreibbare Slots machen ein Gerät steuerbar."""
    by_kind = {s.kind: s for s in slots if s.kind not in _GENERIC_KINDS}

    def writable(kind: int) -> bool:
        return kind in by_kind and by_kind[kind].writable

    if writable(K.SLOT_KIND_ON) and (_CLIMATE_DTYPE in dtypes or by_kind.keys() & _ENUM_KINDS):
        return C.DEVICE_CLASS_CLIMATE
    if K.SLOT_KIND_TARGET_TEMPERATURE in by_kind:
        return C.DEVICE_CLASS_THERMOSTAT
    if writable(K.SLOT_KIND_POSITION):
        return C.DEVICE_CLASS_COVER
    if writable(K.SLOT_KIND_ON):
        if "light" in dtypes or writable(K.SLOT_KIND_BRIGHTNESS) or writable(K.SLOT_KIND_COLOR) \
                or writable(K.SLOT_KIND_COLOR_TEMPERATURE):
            return C.DEVICE_CLASS_LIGHT
        if "socket" in dtypes or K.SLOT_KIND_POWER in by_kind:
            return C.DEVICE_CLASS_SOCKET
        return C.DEVICE_CLASS_GENERIC_BINARY_SWITCH
    if K.SLOT_KIND_OPEN in by_kind and not writable(K.SLOT_KIND_OPEN):
        return C.DEVICE_CLASS_CONTACT
    if by_kind.keys() & _MEASUREMENT_KINDS and not any(s.writable for s in by_kind.values()):
        return C.DEVICE_CLASS_SENSOR
    return C.DEVICE_CLASS_GENERIC


def _subtype(device_class: int, dtypes: set[str]) -> int:
    """Fenster oder Tür bei einem Kontakt, aus dem Typ-Hinweis oder dem Funktionsnamen des Adapters."""
    if device_class == C.DEVICE_CLASS_CONTACT:
        lowered = [d.lower() for d in dtypes]
        if any(h in d for d in lowered for h in _DOOR_HINTS):
            return pb.DEVICE_SUBTYPE_DOOR
        if any(h in d for d in lowered for h in _WINDOW_HINTS):
            return pb.DEVICE_SUBTYPE_WINDOW
    return pb.DEVICE_SUBTYPE_UNSPECIFIED


def _generic_kind(state: _State) -> int:
    if state.state_type == _BOOLEAN:
        return K.SLOT_KIND_GENERIC_BOOL
    if state.state_type == _NUMERIC:
        return K.SLOT_KIND_GENERIC_NUMBER
    return K.SLOT_KIND_GENERIC_TEXT


def _classify_device(device_id: str, name: str, room: str, floor: str, states: list[_State]) -> TypedDevice:
    blind = any(s.dtype == "blind" for s in states)
    climate = any(s.dtype == _CLIMATE_DTYPE for s in states)
    kinds = _resolve_kinds(states, blind, climate)
    standard_ids = {slot_id_for_kind(k) for k in kinds.values()}

    slots: dict[str, Slot] = {}
    for state in states:
        kind = kinds.get(state.state_id)
        if kind is not None:
            slot_id, label = slot_id_for_kind(kind), ""
        else:
            kind = _generic_kind(state)
            slot_id, label = state.suffix, state.key
            n = 2
            while slot_id in slots or slot_id in standard_ids:
                slot_id, n = f"{state.suffix}_{n}", n + 1
        slots[slot_id] = Slot(
            slot_id=slot_id,
            kind=kind,
            value=normalize_value(kind, state.raw, state.inverted),
            writable=state.writable,
            label=label,
            required_trust_level=state.trust,
            state_id=state.state_id,
            inverted=state.inverted and kind == K.SLOT_KIND_POSITION,
            options=list(state.options) if kind in _ENUM_KINDS else [],
            identifier=state.state_id,
        )

    dtypes = {s.dtype for s in states if s.dtype}
    device_class = _decide_class(list(slots.values()), dtypes)
    return TypedDevice(
        device_id=device_id, name=name, room=room, floor=floor, device_class=device_class,
        slots=slots, available=True, origin=ORIGIN_V1, subtype=_subtype(device_class, dtypes),
    )


def classify_snapshot(agent_devices: Iterable) -> list[TypedDevice]:
    """Die typisierten Geräte zu einem v1-Snapshot (Liste von `AgentDevice`).

    Wie beim State-basierten Pfad: States ohne Raum sind rohe States (Wetter, Auto, ...) und
    gehören zu keinem Gerät, States ohne `device_id` kommen von Adaptern vor 1.1.0 und
    lassen sich nicht gruppieren."""
    order: list[str] = []
    meta: dict[str, dict] = {}
    states: dict[str, list[_State]] = {}

    for d in agent_devices:
        if not d.room or not d.device_id:
            continue
        if d.device_id not in meta:
            order.append(d.device_id)
            meta[d.device_id] = {"name": d.device, "room": d.room, "floor": d.floor}
            states[d.device_id] = []
        elif not meta[d.device_id]["floor"] and d.floor:
            meta[d.device_id]["floor"] = d.floor
        suffix = d.state_id.rsplit(".", 1)[-1]
        states[d.device_id].append(_State(
            key=d.canonical_key or suffix,
            suffix=suffix,
            state_id=d.state_id,
            raw=d.value.value,
            state_type=d.state_type,
            writable=d.writable,
            dtype=d.device_type,
            inverted=bool(d.inverted) if d.HasField("inverted") else False,
            trust=d.required_trust_level if d.HasField("required_trust_level") else None,
            options=sorted(d.enum_values.values),
        ))

    return [
        _classify_device(device_id, meta[device_id]["name"], meta[device_id]["room"], meta[device_id]["floor"], states[device_id])
        for device_id in order
    ]


def load_snapshot(registry: DeviceRegistry, agent_devices: Iterable) -> int:
    """Klassifiziert einen v1-Snapshot und ersetzt damit die v1-Geräte der Registry."""
    return registry.replace(ORIGIN_V1, classify_snapshot(agent_devices))
