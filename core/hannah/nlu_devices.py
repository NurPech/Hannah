"""
Geräte-Sicht der NLU auf die typisierte Registry (#387, Schritt 1).

Die NLU sucht ein Gerät über Raum, Namen und Kategorie-Wort ("Licht", "Rollladen",
"Fenster"). Früher stand dafür der Typ-Code des v1-Adapters am Gerät (`light`, `blind`,
`window`, ...). In der Registry gibt es Klassen, Slots und die Unterart; `categories_of`
leitet daraus dieselben Codes ab, die `nlu.category_words` als Ziel benutzen, damit die
Wortlisten unverändert bleiben. Ein Gerät kann mehrere Codes tragen (ein Raumthermostat ist
"thermostat" und "temperature_sensor").

`build_index` liefert das Format, das `NLU` erwartet: `{room_id: {device_id: NluDevice}}`.
"""
from dataclasses import dataclass

from hannah_proto.v2 import hannah_pb2 as pb

from hannah.iobroker import _camel_to_words
from hannah.typed_devices import DeviceRegistry, TypedDevice

C = pb.DeviceClass
K = pb.SlotKind

_DOOR_WORDS = ("tür", "tuer", "door")
_WINDOW_WORDS = ("fenster", "window")

# Reihenfolge für den einen "Haupt-Code" (Logging, Vergleiche); die NLU prüft auf Zugehörigkeit
_PREFERRED = (
    "light", "socket", "blind", "climate", "thermostat", "window", "door",
    "temperature_sensor", "humidity_sensor", "illuminance_sensor", "air_quality_sensor",
)


@dataclass(frozen=True)
class NluDevice:
    id: str
    name: str
    key: str                     # normalisierter Suchbegriff, z.B. "decke seite"
    room: str                    # room_id
    categories: frozenset        # Codes, unter denen die NLU das Gerät findet
    category: str                # der Haupt-Code, "" wenn das Gerät keinen hat


def _contact_categories(device: TypedDevice) -> set:
    if device.subtype == pb.DEVICE_SUBTYPE_DOOR:
        return {"door"}
    if device.subtype == pb.DEVICE_SUBTYPE_WINDOW:
        return {"window"}
    # Ohne Unterart (ein Adapter, der sie nicht kennt): der Name entscheidet, sonst findet
    # das Gerät jede der beiden Fragen.
    name = device.name.lower()
    if any(w in name for w in _DOOR_WORDS):
        return {"door"}
    if any(w in name for w in _WINDOW_WORDS):
        return {"window"}
    return {"window", "door"}


def categories_of(device: TypedDevice) -> frozenset:
    cls = device.device_class
    codes: set = set()
    if cls == C.DEVICE_CLASS_LIGHT:
        codes.add("light")
    elif cls == C.DEVICE_CLASS_SOCKET:
        codes.add("socket")
    elif cls == C.DEVICE_CLASS_COVER:
        codes.add("blind")
    elif cls in (C.DEVICE_CLASS_CLIMATE, C.DEVICE_CLASS_THERMOSTAT):
        codes.add("climate" if cls == C.DEVICE_CLASS_CLIMATE else "thermostat")
        # Wer nach der Temperatur oder Feuchte fragt, bekommt auch Klimageräte und Thermostate
        if device.has_slot(K.SLOT_KIND_TEMPERATURE):
            codes.add("temperature_sensor")
        if device.has_slot(K.SLOT_KIND_HUMIDITY):
            codes.add("humidity_sensor")
    elif cls == C.DEVICE_CLASS_CONTACT:
        codes |= _contact_categories(device)
    elif cls == C.DEVICE_CLASS_SENSOR:
        if device.has_slot(K.SLOT_KIND_TEMPERATURE):
            codes.add("temperature_sensor")
        if device.has_slot(K.SLOT_KIND_HUMIDITY):
            codes.add("humidity_sensor")
        if device.has_slot(K.SLOT_KIND_ILLUMINANCE):
            codes.add("illuminance_sensor")
        if any(device.has_slot(k) for k in (K.SLOT_KIND_IAQ, K.SLOT_KIND_CO2, K.SLOT_KIND_VOC)):
            codes.add("air_quality_sensor")
    return frozenset(codes)


def to_nlu_device(device: TypedDevice) -> NluDevice:
    codes = categories_of(device)
    primary = next((c for c in _PREFERRED if c in codes), "")
    return NluDevice(
        id=device.device_id, name=device.name, key=_camel_to_words(device.name),
        room=device.room, categories=codes, category=primary,
    )


def build_index(registry: DeviceRegistry) -> dict:
    """`{room_id: {device_id: NluDevice}}` aller Geräte der Registry."""
    index: dict = {}
    for device in registry.devices():
        index.setdefault(device.room, {})[device.device_id] = to_nlu_device(device)
    return index
