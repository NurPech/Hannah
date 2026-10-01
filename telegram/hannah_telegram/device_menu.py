"""Haussteuerungsmenü aus den typisierten Geräten von hannah.v2 (Klasse + Slots).

Reine Funktionen ohne Telegram- und gRPC-Bezug: aus einem `DeviceInfo` entstehen Icon,
Statuspunkt, Statustext und die Steuer-Buttons. Welche Buttons ein Gerät bekommt, sagen
seine Slots: ohne `BRIGHTNESS`-Slot kein Dimmer, ein nur lesbarer Slot bekommt keinen Button.
"""
from typing import Optional, Union

from hannah_proto.v2 import hannah_pb2

SlotValueType = Union[bool, int, float, str]

_BOOL_KINDS = {
    hannah_pb2.SLOT_KIND_ON, hannah_pb2.SLOT_KIND_OPEN, hannah_pb2.SLOT_KIND_MOTION,
    hannah_pb2.SLOT_KIND_STOP, hannah_pb2.SLOT_KIND_GENERIC_BOOL,
}
_TEXT_KINDS = {hannah_pb2.SLOT_KIND_MODE, hannah_pb2.SLOT_KIND_FAN_SPEED, hannah_pb2.SLOT_KIND_GENERIC_TEXT}

_CLASS_ICONS = {
    hannah_pb2.DEVICE_CLASS_LIGHT: "💡",
    hannah_pb2.DEVICE_CLASS_SOCKET: "🔌",
    hannah_pb2.DEVICE_CLASS_GENERIC_BINARY_SWITCH: "🔘",
    hannah_pb2.DEVICE_CLASS_THERMOSTAT: "🌡️",
    hannah_pb2.DEVICE_CLASS_COVER: "↕️",
    hannah_pb2.DEVICE_CLASS_SENSOR: "📊",
    hannah_pb2.DEVICE_CLASS_CLIMATE: "❄️",
}
_CLASS_LABELS = {
    hannah_pb2.DEVICE_CLASS_LIGHT: "Licht",
    hannah_pb2.DEVICE_CLASS_SOCKET: "Steckdose",
    hannah_pb2.DEVICE_CLASS_GENERIC_BINARY_SWITCH: "Schalter",
    hannah_pb2.DEVICE_CLASS_THERMOSTAT: "Thermostat",
    hannah_pb2.DEVICE_CLASS_COVER: "Rollladen",
    hannah_pb2.DEVICE_CLASS_SENSOR: "Sensor",
    hannah_pb2.DEVICE_CLASS_CLIMATE: "Klima",
}

_BRIGHTNESS_STEPS = (25, 50, 75, 100)
_COLORS = (("🔴", "#FF0000"), ("🟢", "#00FF00"), ("🔵", "#0000FF"), ("🟡", "#FFFF00"), ("⚪", "#FFFFFF"))
_COLOR_TEMPERATURES = (("🔥 2700 K", 2700), ("☀️ 4000 K", 4000), ("❄️ 6500 K", 6500))
_OPTIONS_PER_ROW = 3


def iaq_label(value: float) -> str:
    """Übersetzt den BSEC2-IAQ-Index (0–500) in eine Klartext-Bewertung (gespiegelt aus Hannah Core)."""
    if value <= 50:
        return "gut"
    if value <= 100:
        return "okay"
    if value <= 150:
        return "leicht belastet"
    return "schlecht"


def slot_value(slot) -> Optional[SlotValueType]:
    """Python-Wert eines Slots, None wenn er unbekannt ist."""
    if not slot.HasField("value"):
        return None
    which = slot.value.WhichOneof("value")
    return getattr(slot.value, which) if which else None


def _slot(dev, kind: int):
    """Der erste Slot einer Art, sonst None."""
    return next((s for s in dev.slots if s.kind == kind), None)


def _writable(dev, kind: int):
    slot = _slot(dev, kind)
    return slot if slot is not None and slot.writable else None


def _contact_noun(dev) -> str:
    if dev.subtype == hannah_pb2.DEVICE_SUBTYPE_DOOR:
        return "Tür"
    if dev.subtype == hannah_pb2.DEVICE_SUBTYPE_WINDOW:
        return "Fenster"
    return "Kontakt"


def device_icon(dev) -> str:
    """Icon nach Geräteklasse, ein Kontakt je nach Unterart."""
    if dev.device_class == hannah_pb2.DEVICE_CLASS_CONTACT:
        return "🚪" if dev.subtype == hannah_pb2.DEVICE_SUBTYPE_DOOR else "🪟"
    return _CLASS_ICONS.get(dev.device_class, "⚙️")


def device_dot(dev) -> str:
    """Statuspunkt aus dem `ON`-Slot: an, aus oder unbekannt (auch ohne `ON`-Slot)."""
    on = _slot(dev, hannah_pb2.SLOT_KIND_ON)
    value = slot_value(on) if on is not None else None
    if value is True:
        return "🟢"
    if value is False:
        return "🔴"
    return "⚫"


def _number(value) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def device_status_text(dev) -> str:
    """Statustext einer Geräteansicht (Markdown)."""
    if dev.device_class == hannah_pb2.DEVICE_CLASS_CONTACT:
        label = _contact_noun(dev)
    else:
        label = _CLASS_LABELS.get(dev.device_class, "Gerät")
    parts = [f"*{dev.name}* ({label})"]

    for slot in dev.slots:
        value = slot_value(slot)
        number = _number(value)
        kind = slot.kind
        if value is None:
            continue
        if kind == hannah_pb2.SLOT_KIND_ON:
            parts.append("Status: " + ("🟢 an" if value else "🔴 aus"))
        elif kind == hannah_pb2.SLOT_KIND_BRIGHTNESS and number is not None:
            parts.append(f"Helligkeit: {int(number)}%")
        elif kind == hannah_pb2.SLOT_KIND_COLOR and isinstance(value, int):
            parts.append(f"Farbe: #{value & 0xFFFFFF:06X}")
        elif kind == hannah_pb2.SLOT_KIND_COLOR_TEMPERATURE and number is not None:
            parts.append(f"Farbtemperatur: {int(number)} K")
        elif kind == hannah_pb2.SLOT_KIND_TEMPERATURE and number is not None:
            parts.append(f"Ist: {number:.1f}°")
        elif kind == hannah_pb2.SLOT_KIND_TARGET_TEMPERATURE and number is not None:
            parts.append(f"Soll: {number:.1f}°")
        elif kind == hannah_pb2.SLOT_KIND_HUMIDITY and number is not None:
            parts.append(f"Luftfeuchte: {int(number)}%")
        elif kind == hannah_pb2.SLOT_KIND_ILLUMINANCE and number is not None:
            parts.append(f"Helligkeit: {number:g} lx")
        elif kind == hannah_pb2.SLOT_KIND_OPEN:
            parts.append(f"{_contact_noun(dev)}: " + ("offen" if value else "geschlossen"))
        elif kind == hannah_pb2.SLOT_KIND_POSITION and number is not None:
            parts.append(f"Position: {int(number)}% offen")
        elif kind == hannah_pb2.SLOT_KIND_POWER and number is not None:
            parts.append(f"Verbrauch: {number:g} W")
        elif kind == hannah_pb2.SLOT_KIND_IAQ and number is not None:
            parts.append(f"Luftqualität: {iaq_label(number)}")
        elif kind == hannah_pb2.SLOT_KIND_CO2 and number is not None:
            parts.append(f"CO₂: {number:.1f} ppm")
        elif kind == hannah_pb2.SLOT_KIND_VOC and number is not None:
            parts.append(f"VOC: {number:.2f} ppm")
        elif kind == hannah_pb2.SLOT_KIND_MODE:
            parts.append(f"Modus: {value}")
        elif kind == hannah_pb2.SLOT_KIND_FAN_SPEED:
            parts.append(f"Lüfter: {value}")
        elif kind in (hannah_pb2.SLOT_KIND_GENERIC_NUMBER, hannah_pb2.SLOT_KIND_GENERIC_BOOL,
                      hannah_pb2.SLOT_KIND_GENERIC_TEXT):
            parts.append(f"{slot.label or slot.slot_id}: {value}")
    return "\n".join(parts)


def control_rows(dev) -> list[list[tuple[str, str, str]]]:
    """Die Steuer-Buttons eines Geräts als Zeilen von (Beschriftung, Slot-ID, Wert als Text).

    Nur schreibbare Slots bekommen Buttons. Der Wert ist Text, weil er in der callback_data
    des Buttons reist; `slot_value_from_text()` macht den typisierten Wert daraus."""
    rows: list[list[tuple[str, str, str]]] = []

    on = _writable(dev, hannah_pb2.SLOT_KIND_ON)
    if on is not None:
        if slot_value(on) is True:
            rows.append([("⏹ Ausschalten", on.slot_id, "false")])
        else:
            rows.append([("✅ Einschalten", on.slot_id, "true")])

    brightness = _writable(dev, hannah_pb2.SLOT_KIND_BRIGHTNESS)
    if brightness is not None:
        current = _number(slot_value(brightness))
        rows.append([
            (f"{'▶' if current is not None and int(current) == pct else '·'}{pct}%", brightness.slot_id, str(pct))
            for pct in _BRIGHTNESS_STEPS
        ])

    position = _writable(dev, hannah_pb2.SLOT_KIND_POSITION)
    if position is not None:
        rows.append([
            ("⬆ Auf", position.slot_id, "100"),
            ("50%", position.slot_id, "50"),
            ("⬇ Zu", position.slot_id, "0"),
        ])

    color = _writable(dev, hannah_pb2.SLOT_KIND_COLOR)
    if color is not None:
        rows.append([(icon, color.slot_id, rgb) for icon, rgb in _COLORS])

    color_temperature = _writable(dev, hannah_pb2.SLOT_KIND_COLOR_TEMPERATURE)
    if color_temperature is not None:
        rows.append([(label, color_temperature.slot_id, str(kelvin)) for label, kelvin in _COLOR_TEMPERATURES])

    for kind in (hannah_pb2.SLOT_KIND_MODE, hannah_pb2.SLOT_KIND_FAN_SPEED):
        slot = _writable(dev, kind)
        if slot is not None and slot.options:
            current = slot_value(slot)
            buttons = [(f"{'▶' if option == current else '·'}{option}", slot.slot_id, option) for option in slot.options]
            rows.extend(buttons[i:i + _OPTIONS_PER_ROW] for i in range(0, len(buttons), _OPTIONS_PER_ROW))

    return rows


def find_slot(dev, slot_id: str):
    """Der Slot eines Geräts zu seiner ID, sonst None."""
    return next((s for s in dev.slots if s.slot_id == slot_id), None)


def slot_value_from_text(slot, text: str) -> "hannah_pb2.SlotValue":
    """Typisierter Wert für einen Slot aus dem Text einer callback_data. ValueError bei Unpassendem."""
    if slot.kind in _BOOL_KINDS:
        if text not in ("true", "false"):
            raise ValueError(f"kein Wahrheitswert: {text!r}")
        return hannah_pb2.SlotValue(boolean=text == "true")
    if slot.kind in _TEXT_KINDS:
        return hannah_pb2.SlotValue(text=text)
    if slot.kind == hannah_pb2.SLOT_KIND_COLOR:
        if not (len(text) == 7 and text.startswith("#")):
            raise ValueError(f"keine Farbe: {text!r}")
        return hannah_pb2.SlotValue(rgb=int(text[1:], 16))
    return hannah_pb2.SlotValue(number=float(text))
