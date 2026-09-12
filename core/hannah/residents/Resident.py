from abc import ABC, abstractmethod
from typing import Callable, Optional

from hannah.utils import EventEmitterMixin

AWAY_PRESENCE_STATE = 0
HOME_PRESENCE_STATE = 1
NIGHT_PRESENCE_STATE = 2


class Resident(EventEmitterMixin, ABC):
    def __init__(self, roomie_id: str, display_name: str, presence_state: Optional[int] = None, mood: Optional[int] = None):
        self.roomie_id = roomie_id
        self.display_name = display_name
        self.presence_state = presence_state
        self.mood = mood

    @property
    @abstractmethod
    def id(self) -> str:
        """Gibt die berechnete ID des Bewohners zurück."""
        pass

    # ------------------------------------------------------------------
    # Presence

    def is_home(self) -> bool:
        """"Zuhause" umfasst sowohl HOME als auch NIGHT — der Residents-Adapter
        bildet presence.state aus den Booleans away/home/night, und home bleibt
        während der Nacht durchgehend true (#286). Nur AWAY bedeutet "nicht da"."""
        return self.presence_state in (HOME_PRESENCE_STATE, NIGHT_PRESENCE_STATE)

    def is_asleep(self) -> bool:
        return self.presence_state == NIGHT_PRESENCE_STATE

    def update(self, display_name: Optional[str], presence_state: Optional[int], mood: Optional[int] = None):
        """Aktualisiert den Resident und feuert arrival/departure/sleep_changed/mood_changed bei Zustandswechsel.

        display_name/presence_state/mood sind None, wenn das jeweilige Feld im
        AgentResident-Update nicht gesetzt war (proto3 `optional`) — ein
        presence-only Update darf den zuletzt bekannten Namen nicht löschen (#206),
        genau wie ein name-only Update die Presence nicht zurücksetzen soll.

        presence_state ist beim allerersten Update None (unbekannt) — dann wird keine
        Transition gemeldet, da es keinen Vorher-Zustand zum Vergleich gibt.
        """
        old_presence = self.presence_state
        old_mood = self.mood

        if display_name is not None:
            self.display_name = display_name
        if presence_state is not None:
            self.presence_state = presence_state
        if mood is not None:
            self.mood = mood

        if presence_state is not None and old_presence is not None:
            was_home = old_presence in (HOME_PRESENCE_STATE, NIGHT_PRESENCE_STATE)
            is_home = self.is_home()
            if is_home and not was_home:
                self._emit("arrival")
            elif was_home and not is_home:
                self._emit("departure")

            was_asleep = old_presence == NIGHT_PRESENCE_STATE
            is_asleep = self.is_asleep()
            if is_asleep != was_asleep:
                self._emit("sleep_changed", is_asleep)

        if mood is not None and mood != old_mood:
            self._emit("mood_changed", old_mood, mood)
