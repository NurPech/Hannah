"""
Sprachantworten auf Geräte-Abfragen, aus der typisierten Registry (#387, Schritt 3).

Früher las `IoBrokerClient.answer_query` den State-basierten `Device`-Baum: ein Wert hieß
`current`, egal ob Temperatur, Feuchte oder Helligkeit, und ein Raumthermostat (`current` +
`expected`) war ein "Temperatursensor", dessen Sollwert nie genannt wurde. Hier wird über
Geräteklassen und Slots geantwortet: jeder Wert hat seine Art (`SlotKind`) und seine
Einheit, ein Thermostat nennt Ist und Soll, Temperatur und Feuchte kollidieren nicht.

Die Antworttexte bleiben die der alten Abfragen, außer wo der alte Weg einen Fehler hatte.
Geräte eines `hannah.v2`-Adapters werden hier genauso beantwortet wie die eines
`hannah.v1`-Adapters.
"""
import logging
from typing import Callable, Optional

from hannah_proto.v2 import hannah_pb2 as pb

from hannah.iobroker import _category_label, _iaq_label
from hannah.nlu_devices import categories_of
from hannah.typed_devices import DeviceRegistry, TypedDevice

log = logging.getLogger(__name__)

K = pb.SlotKind
C = pb.DeviceClass

_MODE_LABELS = {
    "cool": "Kühlen", "heat": "Heizen", "dry": "Entfeuchten", "fan_only": "Lüfter", "auto": "Auto",
}
_FAN_LABELS = {"low": "niedrig", "medium": "mittel", "high": "hoch", "auto": "auto"}

# Kategorie → was davon gesagt wird: (Slot-Art, Vorsatz, Einheit, Format). Format None = Zahl,
# "open" = offen/geschlossen, "iaq" = Bewertung des Luftqualitäts-Index.
_READERS: dict[str, list[tuple[int, str, str, Optional[str]]]] = {
    "temperature_sensor": [
        (K.SLOT_KIND_TEMPERATURE, "", "Grad", None),
        (K.SLOT_KIND_TARGET_TEMPERATURE, "Soll ", "Grad", None),
    ],
    "thermostat": [
        (K.SLOT_KIND_TEMPERATURE, "", "Grad", None),
        (K.SLOT_KIND_TARGET_TEMPERATURE, "Soll ", "Grad", None),
    ],
    "window": [(K.SLOT_KIND_OPEN, "", "", "open")],
    "door": [(K.SLOT_KIND_OPEN, "", "", "open")],
    "blind": [(K.SLOT_KIND_POSITION, "", "%", None)],
    "air_quality_sensor": [
        (K.SLOT_KIND_IAQ, "", "", "iaq"),
        (K.SLOT_KIND_CO2, "", "ppm CO₂", None),
        (K.SLOT_KIND_VOC, "", "ppm VOC", None),
    ],
    "humidity_sensor": [(K.SLOT_KIND_HUMIDITY, "", "%", None)],
    "illuminance_sensor": [(K.SLOT_KIND_ILLUMINANCE, "", "lx", None)],
    "socket": [(K.SLOT_KIND_POWER, "", "Watt", None)],
}

# In welcher Reihenfolge ein Gerät mit mehreren Codes beschrieben wird, wenn die Frage keinen nennt
_DESCRIBE_ORDER = (
    "blind", "thermostat", "temperature_sensor", "humidity_sensor", "illuminance_sensor",
    "air_quality_sensor", "window", "door", "socket",
)


def _number(value) -> str:
    """Eine Zahl wie in der Sprachantwort: ganze Werte ohne Nachkommastelle, sonst eine."""
    number = float(value)
    return str(int(number)) if number == int(number) else f"{number:.1f}"


def _value(device: TypedDevice, kind: int):
    slot = device.slot_of_kind(kind)
    return slot.value if slot is not None else None


class DeviceAnswers:
    """Beantwortet `Query`-Intents über Geräte aus der Registry."""

    def __init__(self, registry: DeviceRegistry, rooms: Callable[[], dict]):
        self._registry = registry
        self._rooms = rooms          # () -> {room_id: Anzeigename}

    # ------------------------------------------------------------------
    # Einstieg

    def answer(self, intent) -> Optional[str]:
        """Der Antworttext zu einem `Query`-Intent, None wenn es nichts zu sagen gibt."""
        cf = intent.category_filter
        if intent.device_id:
            device = self._registry.get(intent.device_id)
            if device is None:
                return None
            targets = [device]
            room_label = intent.room
        elif intent.room:
            room_devices = self._registry.devices_in_room(intent.room_id or "")
            targets = [d for d in room_devices if cf is None or cf in categories_of(d)]
            room_label = intent.room
            if not room_devices:
                return f"Ich kenne keine Geräte im {intent.room}."
            if not targets:
                # Raum ist bekannt und hat Geräte, nur keins der gefragten Kategorie (#276)
                return f"Ich kenne keine {_category_label(cf)} im {intent.room}."
        else:
            everyone = [d for d in self._registry.devices() if cf is None or cf in categories_of(d)]
            return self._answer_global(everyone, intent.query_state, cf)

        if len(targets) == 1:
            return self._describe_device(targets[0], intent.query_state, cf)
        return self._summarize(targets, intent.query_state, room_label, cf)

    # ------------------------------------------------------------------
    # Hilfen

    def _room_name(self, device: TypedDevice) -> str:
        return self._rooms().get(device.room, device.room)

    @staticmethod
    def _describing_category(device: TypedDevice, category_filter: Optional[str], qs: Optional[str]) -> Optional[str]:
        """Der Code, unter dem das Gerät beschrieben wird: der gefragte, sonst der erste passende.
        Steckdosen nennen den Verbrauch nur auf eine Frage danach (#121), sonst den Schaltzustand."""
        codes = categories_of(device)
        ordered = ([category_filter] if category_filter in codes else []) + [c for c in _DESCRIBE_ORDER if c in codes]
        for code in ordered:
            if code not in _READERS:
                continue
            if code == "socket" and qs != "power":
                continue
            return code
        return None

    @staticmethod
    def _level(device: TypedDevice):
        for kind in (K.SLOT_KIND_BRIGHTNESS, K.SLOT_KIND_POSITION):
            value = _value(device, kind)
            if value is not None:
                return value
        return None

    # ------------------------------------------------------------------
    # Beschreiben

    def _describe_category(self, category: str, targets: list, room: str) -> Optional[str]:
        readers = _READERS.get(category)
        if not readers:
            return None
        lines = []
        for device in targets:
            parts = []
            for kind, prefix, unit, fmt in readers:
                value = _value(device, kind)
                if value is None:
                    continue
                if fmt == "open":
                    parts.append("offen" if value else "geschlossen")
                elif fmt == "iaq":
                    parts.append(_iaq_label(float(value)))
                else:
                    parts.append(f"{prefix}{_number(value)} {unit}".strip())
            if parts:
                lines.append(f"{device.name}: {', '.join(parts)}" if len(targets) > 1 else ", ".join(parts))
        if not lines:
            return None
        prefix = f"Im {room}" if len(targets) > 1 else f"{targets[0].name} im {room}"
        return prefix + ": " + ", ".join(lines) + "."

    def _describe_climate(self, device: TypedDevice) -> str:
        name, room = device.name, self._room_name(device)
        parts = []
        on = _value(device, K.SLOT_KIND_ON)
        if on is not None:
            parts.append("an" if on else "aus")
        mode = _value(device, K.SLOT_KIND_MODE)
        if mode is not None:
            parts.append(f"Modus {_MODE_LABELS.get(str(mode), str(mode))}")
        current = _value(device, K.SLOT_KIND_TEMPERATURE)
        if current is not None:
            parts.append(f"{float(current):.1f}°C")
        target = _value(device, K.SLOT_KIND_TARGET_TEMPERATURE)
        if target is not None:
            parts.append(f"Soll {float(target):.1f}°C")
        fan = _value(device, K.SLOT_KIND_FAN_SPEED)
        if fan is not None:
            parts.append(f"Lüfter {_FAN_LABELS.get(str(fan), str(fan))}")
        if not parts:
            return f"Ich habe keine Daten für {name} im {room}."
        return f"{name} im {room}: {', '.join(parts)}."

    def _describe_device(self, device: TypedDevice, qs: Optional[str], category_filter: Optional[str] = None) -> str:
        name, room = device.name, self._room_name(device)

        if device.device_class == C.DEVICE_CLASS_CLIMATE:
            # Auf die Frage nach der Temperatur antwortet die Klimaanlage mit Ist und Soll, der
            # Gesamtzustand (an, Modus, Lüfter) bleibt die Antwort auf alles andere.
            if category_filter in ("temperature_sensor", "thermostat"):
                answer = self._describe_category("thermostat", [device], room)
                if answer is not None:
                    return answer
            return self._describe_climate(device)

        category = self._describing_category(device, category_filter, qs)
        if category is not None:
            answer = self._describe_category(category, [device], room)
            if answer is not None:
                return answer

        level = self._level(device)
        if qs == "level" or (qs is None and level is not None):
            if level is not None:
                return f"{name} im {room} ist auf {int(level)} Prozent."
            return f"Keine Helligkeitsdaten für {name}."

        color = _value(device, K.SLOT_KIND_COLOR)
        if qs == "color" or (qs is None and color is not None):
            if color is not None:
                return f"{name} im {room} leuchtet in #{int(color):06X}."
            return f"Keine Farbdaten für {name}."

        on = _value(device, K.SLOT_KIND_ON)
        if on is None:
            return f"Ich weiß nicht ob {name} im {room} an oder aus ist."
        status = "an" if on else "aus"
        power = _value(device, K.SLOT_KIND_POWER)
        if power is not None:
            return f"{name} im {room} ist {status} ({_number(power)} W)."
        return f"{name} im {room} ist {status}."

    def _common_category(self, targets: list, category_filter: Optional[str], qs: Optional[str]) -> Optional[str]:
        """Der gemeinsame Beschreibungs-Code einer Gerätegruppe, None wenn es keinen gibt."""
        found = {self._describing_category(d, category_filter, qs) for d in targets}
        return found.pop() if len(found) == 1 else None

    def _summarize(self, targets: list, qs: Optional[str], room_label: str, category_filter: Optional[str]) -> Optional[str]:
        """Fasst mehrere Geräte in einem Raum zusammen."""
        category = self._common_category(targets, category_filter, qs)
        if category is not None:
            answer = self._describe_category(category, targets, room_label)
            if answer is not None:
                return answer

        if qs == "on" or qs is None:
            on_devs = [d for d in targets if _value(d, K.SLOT_KIND_ON) is True]
            off_devs = [d for d in targets if _value(d, K.SLOT_KIND_ON) is False]
            unknown = [d for d in targets if _value(d, K.SLOT_KIND_ON) is None]
            parts = []
            if on_devs:
                parts.append(f"{', '.join(d.name for d in on_devs)} {'ist' if len(on_devs) == 1 else 'sind'} an")
            if off_devs:
                parts.append(f"{', '.join(d.name for d in off_devs)} {'ist' if len(off_devs) == 1 else 'sind'} aus")
            if unknown:
                parts.append(f"von {', '.join(d.name for d in unknown)} habe ich keinen Status")
            if not parts:
                return f"Ich habe noch keine Statusdaten für {room_label}."
            return f"Im {room_label}: " + ", ".join(parts) + "."

        if qs == "level":
            lines = [f"{d.name} {int(level)} Prozent" for d in targets if (level := self._level(d)) is not None]
            return (f"Helligkeit im {room_label}: " + ", ".join(lines) + ".") if lines \
                else f"Keine Helligkeitsdaten für {room_label}."

        return None

    def _answer_global(self, targets: list, qs: Optional[str], category_filter: Optional[str]) -> str:
        """Globale Abfrage über alle Räume, raumweise zusammengefasst."""
        if not targets:
            if category_filter:
                return f"Ich kenne keine {_category_label(category_filter)}."
            return "Ich habe keine Gerätedaten."

        by_room = sorted(targets, key=lambda d: d.room)

        if category_filter and self._common_category(targets, category_filter, qs) is not None:
            lines = [self._describe_device(d, qs, category_filter) for d in by_room]
            lines = [line for line in lines if line]
            return " ".join(lines) if lines else f"Keine {_category_label(category_filter)}-Daten verfügbar."

        if qs == "on" or qs is None:
            rooms_on = sorted({self._room_name(d) for d in targets if _value(d, K.SLOT_KIND_ON) is True})
            if not rooms_on:
                return f"Keine {_category_label(category_filter)} sind eingeschaltet."
            return f"Eingeschaltete {_category_label(category_filter)} in: {', '.join(rooms_on)}."

        if qs == "level":
            lines = [f"{d.name} im {self._room_name(d)}: {int(level)} Prozent"
                     for d in by_room if (level := self._level(d)) is not None]
            return ("Helligkeit: " + ", ".join(lines) + ".") if lines else "Keine Helligkeitsdaten verfügbar."

        if len({self._describing_category(d, category_filter, qs) for d in targets}) == 1:
            lines = [self._describe_device(d, qs, category_filter) for d in by_room]
            lines = [line for line in lines if line]
            return " ".join(lines) if lines else "Keine Sensordaten verfügbar."

        return "Bitte nenne einen Raum für diese Abfrage."
