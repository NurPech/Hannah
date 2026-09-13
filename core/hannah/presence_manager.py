"""
Hannah Presence Fusion (hannah#294)

Fusioniert Presence-Rohsignale (WLAN-States via source_type="iobroker_state", BLE-
Sichtungen via source_type="ble_tag") zu einem stabilen Home/Away-Zustand pro User.
Ersetzt den bisherigen direkten Schreibpfad (BLE-Sichtung -> user.presence = True in
main.py._on_ble_location_change) durch eine Fusionslogik mit Hysterese, weil BLE und
ein extern per Foreign-State gekoppelter WLAN-Presence-State bisher unkoordiniert
denselben ioBroker-Residents-State beeinflussten (Flapping bei kurzen Aussetzern).

Fusionsregel pro User (relativer Vergleich statt fixem Schwellwert — siehe die
Korrektur vom 2026-09-13 im hannah#294-Kommentarverlauf für die Herleitung):

    strongest_home = max(home_confidence der Quellen, die aktuell "home" melden und
                          frisch sind; 0 falls keine)
    strongest_away = max(away_confidence der Quellen, die aktuell "away" melden oder
                          veraltet sind; 0 falls keine)
    instantaneous_home = strongest_home >= strongest_away   (Gleichstand gewinnt Home)

Home wird sofort übernommen; Away erst, nachdem instantaneous_home durchgehend für
grace_period_seconds False war (Hysterese gegen kurze Funk-/Signal-Aussetzer).

BLE-Quellen sind push-only (eine Sichtung ist ein "home"-Ereignis, es gibt keine
explizite "weg"-Meldung) — eine BLE-Lesung zählt daher für strongest_away statt
strongest_home, sobald sie älter als ble_staleness_seconds ist. iobroker_state-Quellen
sind level-getriggert (der State selbst ist immer aktuell) und brauchen das nicht.
"""
import logging
import threading
import time
from typing import Callable, Optional

from hannah.models.presence_source import PresenceSource as PresenceSourceModel

log = logging.getLogger(__name__)


class PresenceManager:
    def __init__(
        self,
        db: Callable,
        user_lookup: Callable[[int], Optional[object]],  # -> User | None (models.user.User)
        grace_period_seconds: float = 120.0,
        ble_staleness_seconds: float = 30.0,  # ~3x BLE-Scan-Intervall (10s)
    ):
        self._db = db
        self._user_lookup = user_lookup
        self._grace_period_seconds = grace_period_seconds
        self._ble_staleness_seconds = ble_staleness_seconds

        self._lock = threading.Lock()
        self._raw: dict[int, tuple[bool, float]] = {}    # source_id -> (value, ts)
        self._pending_away_since: dict[int, float] = {}  # user_id -> ts (fehlt = nicht pending)

    # ------------------------------------------------------------------
    # Quellen-Zugriff — presence_sources ändert sich selten (Admin-UI-CRUD),
    # daher immer frisch aus der DB statt gecacht.

    def _enabled_sources(self) -> list:
        return PresenceSourceModel.select(self._db()).where(enabled=1).all()

    def get_referenced_state_ids(self) -> set[str]:
        """ioBroker-State-IDs aller aktiven iobroker_state-Quellen — für AgentWatchMore."""
        return {s.reference for s in self._enabled_sources() if s.source_type == "iobroker_state"}

    # ------------------------------------------------------------------
    # Signal-Eingänge

    def on_state_update(self, state_id: str, raw_value: str) -> None:
        # Nicht nur bool-States (true/false): das Residents-Adapter-Präsenzfeld z.B. ist
        # tristate (0=weg, 1=wach, 2=schlafend) — 1 UND 2 bedeuten "zuhause". Daher als
        # Home werten, was nicht explizit "weg"/"aus"/falsy ist, statt nur "true"/"1"/"on"
        # als Home zu akzeptieren (das hätte 2/schlafend fälschlich als "weg" gezählt).
        value = raw_value.strip().lower() not in ("0", "false", "off", "")
        now = time.time()
        matched_user_ids: set[int] = set()
        with self._lock:
            for s in self._enabled_sources():
                if s.source_type == "iobroker_state" and s.reference == state_id:
                    self._raw[s.id] = (value, now)
                    matched_user_ids.add(s.user_id)
        for user_id in matched_user_ids:
            self._run_fusion(user_id)

    def on_ble_sighting(self, user_id: int) -> None:
        """BLE-Sichtung für user_id. Welcher konkrete Tag gesichtet wurde spielt keine
        Rolle — ble_location.py hat die User-Identität bereits aufgelöst, bevor dieser
        Callback feuert; alle enabled ble_tag-Quellen dieses Users gelten als frisch."""
        now = time.time()
        with self._lock:
            for s in self._enabled_sources():
                if s.source_type == "ble_tag" and s.user_id == user_id:
                    self._raw[s.id] = (True, now)
        self._run_fusion(user_id)

    def tick(self) -> None:
        """Periodischer Re-Check, damit BLE-Staleness und ablaufende Grace-Perioden
        auch ohne neu eintreffende Signale zum Tragen kommen."""
        user_ids = {s.user_id for s in self._enabled_sources()}
        for user_id in user_ids:
            self._run_fusion(user_id)

    # ------------------------------------------------------------------
    # Fusion

    def _run_fusion(self, user_id: int) -> None:
        now = time.time()
        sources = [s for s in self._enabled_sources() if s.user_id == user_id]
        if not sources:
            return

        decision: Optional[bool] = None
        with self._lock:
            strongest_home = 0.0
            strongest_away = 0.0
            for s in sources:
                raw = self._raw.get(s.id)
                if raw is None:
                    continue
                value, ts = raw
                stale = s.source_type == "ble_tag" and (now - ts) > self._ble_staleness_seconds
                if value and not stale:
                    strongest_home = max(strongest_home, s.home_confidence)
                else:
                    strongest_away = max(strongest_away, s.away_confidence)

            instantaneous_home = strongest_home >= strongest_away
            if instantaneous_home:
                self._pending_away_since.pop(user_id, None)
                decision = True
            else:
                since = self._pending_away_since.get(user_id)
                if since is None:
                    self._pending_away_since[user_id] = now
                elif now - since >= self._grace_period_seconds:
                    decision = False

        if decision is not None:
            self._apply(user_id, decision)

    def _apply(self, user_id: int, is_home: bool) -> None:
        user = self._user_lookup(user_id)
        if user is None:
            return
        # Schlaf-Status (residents.*.presence.night, gesetzt über den expliziten "ich gehe
        # schlafen"-Pfad, siehe main.py's _trigger_set_presence -> residents.set_user_asleep)
        # geht nie über user.presence/user.asleep, daher weiß die Fusion sonst nichts davon.
        # Ohne diese Sperre würde ein nächtlicher Signalausfall (Handy im Doze-Mode, BLE
        # kurz nicht gesichtet) nach der Grace-Period fälschlich "weg" auslösen und damit
        # den Night-Flag in ioBroker zurücksetzen, obwohl die Person nur schläft.
        if not is_home and user.asleep:
            return
        if user.presence != is_home:
            user.presence = is_home
