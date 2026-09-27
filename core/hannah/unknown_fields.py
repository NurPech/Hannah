"""
Unbekannte Protobuf-Felder einer empfangenen Nachricht einsammeln (#367).

Protobuf verwirft Felder, die das eigene Schema nicht kennt, beim Auswerten
stillschweigend — sie bleiben nur als "unknown fields" an der Nachricht hängen.
Der ioBroker-Adapter fragt per AgentMessage.ack_id nach, welche Felder Core nicht
verstanden hat (hannah-proto#16), damit er z.B. vor einem nicht geprüften
Trust-Level warnen kann.
"""

from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.message import Message
from google.protobuf.unknown_fields import UnknownFieldSet


def collect_unknown_fields(msg: Message) -> dict[str, set[int]]:
    """
    Sammelt rekursiv alle unbekannten Feldnummern, pro voll qualifiziertem
    Nachrichtentyp zusammengefasst (inkl. verschachtelter, repeated und
    Map-Value-Nachrichten). Ein unbekannter oneof-Payload taucht als Feldnummer
    der umschließenden Nachricht auf.
    """
    result: dict[str, set[int]] = {}
    _collect(msg, result)
    return result


def _collect(msg: Message, result: dict[str, set[int]]) -> None:
    numbers = {field.field_number for field in UnknownFieldSet(msg)}
    if numbers:
        result.setdefault(msg.DESCRIPTOR.full_name, set()).update(numbers)

    for field, value in msg.ListFields():
        if field.type != FieldDescriptor.TYPE_MESSAGE:
            continue
        if field.message_type.GetOptions().map_entry:
            value_field = field.message_type.fields_by_name["value"]
            if value_field.type == FieldDescriptor.TYPE_MESSAGE:
                for item in value.values():
                    _collect(item, result)
        elif field.is_repeated:
            for item in value:
                _collect(item, result)
        else:
            _collect(value, result)
