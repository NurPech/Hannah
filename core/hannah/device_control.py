"""
Geräte über Slots steuern (#386, Teil 2 von #383).

Ein Schreibbefehl adressiert ein Gerät und einen Slot der typisierten Registry
(`typed_devices.py`), nicht einen ioBroker-State oder einen kanonischen Key. Was daraus wird,
hängt von der Herkunft des Geräts ab:

- `hannah.v2`-Adapter: Core schickt `SetSlot`. Der Adapter setzt den echten State und
  denormalisiert den Wert, Core kennt keine Wertebereiche. Bestätigt ist der Befehl, wenn ein
  `SlotUpdate` mit `ack=true` für diesen Slot zurückkommt.
- `hannah.v1`-Adapter: der Slot eines klassifizierten Geräts kennt seine ioBroker-`state_id`,
  Core schickt wie immer `SetState`, im Wert-Format, das die v1-Adapter kennen (`true`,
  Zahl, `#RRGGBB`). Einen invertierten Rollladen (0 % = offen) rechnet Core dabei zurück.
  Die Bestätigung läuft wie bisher über das v1-State-Update.

Das Trust-Level (#366) wird pro Slot geprüft (`Slot.required_trust_level`), bei v1-Geräten
ist das der Wert, den der Adapter pro State gemeldet hat.
"""
import logging
import threading
from typing import Callable, NamedTuple, Optional

from hannah_proto.v2 import hannah_pb2 as pb

from hannah.iobroker import TrustLevelDenied
from hannah.typed_devices import (
    ORIGIN_V2, DeviceRegistry, Slot, slot_value_from_pb, slot_value_to_pb,
)

log = logging.getLogger(__name__)

DEFAULT_CONFIRM_TIMEOUT = 3.0


class SlotWrite(NamedTuple):
    found: bool                        # Gerät und Slot sind in der Registry bekannt
    sent: bool                         # der Befehl ist an einen verbundenen Adapter rausgegangen
    confirmed: Optional[bool] = None   # None = nicht gewartet


def _v1_wire_value(slot: Slot, value: "pb.SlotValue"):
    """Der Wert eines Slots im Format, das ein hannah.v1-Adapter beim Schreiben erwartet."""
    which = value.WhichOneof("value")
    if which == "rgb":
        return f"#{value.rgb & 0xFFFFFF:06X}"
    if which == "number":
        number = value.number
        if slot.inverted and slot.kind == pb.SLOT_KIND_POSITION:
            number = 100 - number
        return int(number) if number == int(number) else number
    return slot_value_from_pb(value)


class DeviceController:
    def __init__(
        self,
        registry: DeviceRegistry,
        *,
        send_set_slot: Callable[[str, str, "pb.SlotValue"], bool],
        send_set_state: Callable[[str, object], bool],
        confirm_timeout: float = DEFAULT_CONFIRM_TIMEOUT,
    ):
        """send_set_slot(device_id, slot_id, value) → True, wenn ein hannah.v2-Adapter verbunden ist;
        send_set_state(state_id, value) → True, wenn der Befehl an einen Adapter rausging."""
        self._registry = registry
        self._send_set_slot = send_set_slot
        self._send_set_state = send_set_state
        self._confirm_timeout = confirm_timeout
        self._waiters: dict[tuple[str, str], threading.Event] = {}
        self._lock = threading.Lock()

    def set_slot(
        self, device_id: str, slot_id: str, value, *, trust_level: Optional[int], wait_confirm: bool = False,
    ) -> SlotWrite:
        """Setzt einen Slot.

        trust_level: Trust-Level des anfragenden Users (#366), None = Aufrufer ohne User, der
          bewusst ungeprüft bleibt; unbekannte User übergeben GUEST_TRUST_LEVEL.
        wait_confirm: bis zu `confirm_timeout` auf die Bestätigung des Adapters warten, nur bei
          Geräten eines v2-Adapters (bei v1 bestätigt das State-Update wie bisher).
        Wirft TrustLevelDenied, wenn das Trust-Level nicht reicht, und ValueError, wenn der Wert
        nicht zur Art des Slots passt.
        """
        device = self._registry.get(device_id)
        slot = device.slots.get(slot_id) if device else None
        if slot is None:
            log.warning(f"set_slot: Gerät/Slot {device_id!r}/{slot_id!r} nicht bekannt")
            return SlotWrite(found=False, sent=False)

        required = slot.required_trust_level
        if trust_level is not None and required is not None and trust_level < required:
            log.info(f"set_slot: Trust-Level {trust_level} < {required} für {device.name!r}/{slot_id}")
            raise TrustLevelDenied(f"{device_id}/{slot_id}")
        if not slot.writable:
            log.warning(f"set_slot: {device.name!r}/{slot_id} ist nicht schreibbar")
            return SlotWrite(found=True, sent=False)

        slot_value = slot_value_to_pb(slot.kind, value)

        if device.origin == ORIGIN_V2:
            return self._write_v2(device_id, slot_id, slot_value, wait_confirm)

        sent = self._send_set_state(slot.state_id, _v1_wire_value(slot, slot_value))
        if sent:
            # Die Antwort des Adapters ist asynchron, ein GetDevices direkt danach sähe sonst den alten Wert
            self._registry.set_slot_value(device_id, slot_id, slot_value_from_pb(slot_value))
        return SlotWrite(found=True, sent=sent)

    def handle_slot_update(self, update: "pb.SlotUpdate") -> None:
        """Ein SlotUpdate des Adapters: mit ack=true bestätigt es einen wartenden set_slot()."""
        if not update.ack:
            return
        with self._lock:
            waiter = self._waiters.get((update.device_id, update.slot_id))
        if waiter is not None:
            waiter.set()

    # ------------------------------------------------------------------

    def _write_v2(self, device_id: str, slot_id: str, value: "pb.SlotValue", wait_confirm: bool) -> SlotWrite:
        key = (device_id, slot_id)
        confirmed_event = None
        if wait_confirm:
            # Vor dem Senden registrieren, sonst kann eine schnelle Bestätigung ungehört verpuffen
            confirmed_event = threading.Event()
            with self._lock:
                self._waiters[key] = confirmed_event
        try:
            sent = self._send_set_slot(device_id, slot_id, value)
            if not sent or confirmed_event is None:
                return SlotWrite(found=True, sent=sent)
            return SlotWrite(found=True, sent=True, confirmed=confirmed_event.wait(self._confirm_timeout))
        finally:
            if confirmed_event is not None:
                with self._lock:
                    self._waiters.pop(key, None)
