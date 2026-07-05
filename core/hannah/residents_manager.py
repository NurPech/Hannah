import logging
import threading
from typing import Callable, Optional

from hannah.residents import Resident
from hannah_proto import hannah_pb2 as pb

log = logging.getLogger(__name__)


class ResidentsClient:
    """
    Verbindet Hannah mit dem ioBroker Residents-Adapter.

    Lesen   : Presence-Updates kommen via gRPC (AgentResident), siehe main.py:_on_agent_resident.
    Schreiben: Hannah → ioBroker via agent_set_resident (gRPC).

    Hannah pflegt ihren eigenen Status (hannah_roomie) beim Start/Stop.
    Auf Ankunft/Abreise von Roomies und Gästen wird mit Callbacks reagiert.
    """

    def __init__(self, cfg: dict, user_manager):
        self._lock = threading.Lock()
        # Key ist (resident_cls, roomie_id), nicht nur roomie_id — ein Gast und ein
        # Roomie können denselben Namen haben (separate Präfixe im Residents-Adapter).
        self._residents: dict[tuple[type, str], Resident] = {}

        self.hannah_name = cfg.get("hannah_roomie", "hannah")

        # Maßgebliche Quelle für "echte Bewohner" (vs. Gäste/Pets) ist die User Registry
        # (User.type == "roomie" über den verlinkten Residents-Account), nicht mehr eine
        # statische Config-Liste — vermeidet doppelte Pflege seit #72.
        self._user_manager = user_manager

        # Set by main.py: fn(resident_id, presence_state, resident_type) → sends SetResident via gRPC adapter
        self._setter: Optional[Callable[[str, int, "pb.ResidentType"], bool]] = None
        # Set by main.py: fn(resident_id, mood, resident_type) → sends SetResidentMood via gRPC adapter
        self._mood_setter: Optional[Callable[[str, int, "pb.ResidentType"], bool]] = None

        # Welche presence_state-Werte bedeuten "zuhause" / "weg"?
        # Residents-Adapter: 0=Abwesend, 1=zu Hause, 2=Nacht
        self._state_home = cfg.get("state_home", 1)
        self._state_away = cfg.get("state_away", 0)

        # Callbacks: fn(resident: Resident)
        self._on_arrival:   Optional[Callable[[Resident], None]] = None
        self._on_departure: Optional[Callable[[Resident], None]] = None
        # Callback: fn(resident: Resident, old_mood: int, mood: int)
        self._on_mood_changed: Optional[Callable[[Resident, int, int], None]] = None

    # ------------------------------------------------------------------
    # Callbacks registrieren
    def set_setter(self, fn: Callable[[str, int, "pb.ResidentType"], bool]):
        """Register the gRPC state setter: fn(resident_id, presence_state, resident_type) → True if adapter is connected."""
        self._setter = fn

    def set_mood_setter(self, fn: Callable[[str, int, "pb.ResidentType"], bool]):
        """Register the gRPC mood setter: fn(resident_id, mood, resident_type) → True if adapter is connected."""
        self._mood_setter = fn

    def on_arrival(self, fn: Callable[[Resident], None]):
        """Wird aufgerufen wenn ein Resident (Roomie oder Gast) von weg → zuhause wechselt."""
        self._on_arrival = fn

    def on_departure(self, fn: Callable[[Resident], None]):
        """Wird aufgerufen wenn ein Resident (Roomie oder Gast) von zuhause → weg wechselt."""
        self._on_departure = fn

    def on_mood_changed(self, fn: Callable[[Resident, int, int], None]):
        """Wird aufgerufen wenn sich die Stimmung eines Residents ändert: fn(resident, old_mood, mood)."""
        self._on_mood_changed = fn

    # ------------------------------------------------------------------
    # Resident-Registry

    def get_or_create(self, roomie_id: str, resident_cls: type) -> Resident:
        """Liefert den bekannten Resident zu (roomie_id, resident_cls), legt ihn sonst neu an."""
        key = (resident_cls, roomie_id)
        with self._lock:
            resident = self._residents.get(key)
            if resident is None:
                resident = resident_cls(roomie_id, roomie_id)
                resident.on("arrival", self._dispatch_arrival)
                resident.on("departure", self._dispatch_departure)
                resident.on("mood_changed", self._dispatch_mood_changed)
                self._residents[key] = resident
            return resident

    def get_or_null(self, roomie_id: str, resident_cls: type) -> Optional[Resident]:
        """Liefert den bekannten Resident zu (roomie_id, resident_cls), oder None falls unbekannt — legt nichts neu an."""
        with self._lock:
            return self._residents.get((resident_cls, roomie_id))

    def all_residents(self) -> list[Resident]:
        """Alle bisher per gRPC bekannt gewordenen Residents (Roomies/Guests/Pets)."""
        with self._lock:
            return list(self._residents.values())

    def _dispatch_arrival(self, resident: Resident):
        log.info(f"Residents: {resident.roomie_id} ({type(resident).__name__}) ist angekommen.")
        if self._on_arrival:
            threading.Thread(target=self._on_arrival, args=(resident,), daemon=True).start()

    def _dispatch_departure(self, resident: Resident):
        log.info(f"Residents: {resident.roomie_id} ({type(resident).__name__}) hat das Haus verlassen.")
        if self._on_departure:
            threading.Thread(target=self._on_departure, args=(resident,), daemon=True).start()

    def _dispatch_mood_changed(self, resident: Resident, old_mood: int, mood: int):
        log.info(f"Residents: {resident.roomie_id} ({type(resident).__name__}) Stimmung {old_mood} → {mood}.")
        if self._on_mood_changed:
            threading.Thread(target=self._on_mood_changed, args=(resident, old_mood, mood), daemon=True).start()

    # ------------------------------------------------------------------
    # State setzen (Hannah → ioBroker)

    def set_presence(self, roomie: str, state_value: int, resident_type: "pb.ResidentType" = pb.ResidentType.ROOMIE):
        """Setzt den Anwesenheits-Status eines Residents via gRPC."""
        self._setter(roomie, state_value, resident_type)
        log.info(f"Residents: {roomie} → {state_value!r} ({resident_type})")

    def set_user_home(self, roomie: str):
        self.set_presence(roomie, self._state_home, pb.ResidentType.ROOMIE)

    def set_user_away(self, roomie: str):
        self.set_presence(roomie, self._state_away, pb.ResidentType.ROOMIE)

    def announce_online(self):
        """Setzt Hannahs eigenen Status auf 'home' (beim Start)."""
        self.set_presence(self.hannah_name, self._state_home, pb.ResidentType.ROOMIE)

    def announce_offline(self):
        """Setzt Hannahs eigenen Status auf 'away' (beim Stop)."""
        self.set_presence(self.hannah_name, self._state_away, pb.ResidentType.ROOMIE)

    def set_guest_home(self, roomie: str):
        self.set_presence(roomie, self._state_home, pb.ResidentType.GUEST)

    def set_guest_away(self, roomie: str):
        self.set_presence(roomie, self._state_away, pb.ResidentType.GUEST)

    def set_mood(self, roomie: str, mood: int, resident_type: "pb.ResidentType" = pb.ResidentType.ROOMIE):
        """Pusht eine Stimmungsänderung an den Residents-Adapter, unabhängig vom Presence-Status."""
        self._mood_setter(roomie, mood, resident_type)
        log.info(f"Residents: {roomie} → mood {mood!r} ({resident_type})")

    # ------------------------------------------------------------------
    # Cache lesen

    def is_home(self, roomie_id: Optional[str] = None) -> bool:
        if roomie_id:
            return any(
                resident.is_home()
                for resident in self._residents.values()
                if resident.roomie_id == roomie_id
            )
        roomie_ids = self._user_manager.get_roomie_ids()
        return any(
            resident.is_home()
            for resident in self._residents.values()
            if resident.roomie_id in roomie_ids
        )
