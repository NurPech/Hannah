import datetime
import json
import logging
import threading
from typing import Callable, Optional

from hannah.models.alarm import Alarm

log = logging.getLogger(__name__)


def format_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} Stunde{'n' if h > 1 else ''}")
    if m:
        parts.append(f"{m} Minute{'n' if m > 1 else ''}")
    if s:
        parts.append(f"{s} Sekunde{'n' if s > 1 else ''}")
    return " und ".join(parts) if parts else "0 Sekunden"


class AlarmManager:
    """Persistenter, wiederkehrender Wecker-Manager (DB-backed via `Alarm`-Model,
    ersetzt die alte JSON-Datei-Persistenz). Kombiniert CRUD (fürs künftige
    WebUI-CRUD über gRPC), Pro-Alarm-Scheduling per `threading.Timer` (kein
    Poll-Loop wie bei TriggerEngine — ein Wecker muss pünktlich klingeln) und
    "klingelt gerade"-State (wiederholender Weckton mit alternierender
    Lautstärke via MQTT, bis `stop_ringing()` aufgerufen wird — #4). Meldet der
    Satellit per on_play_result() einen fehlgeschlagenen Play-Versuch (Asset nicht
    im Cache o.ä.), schaltet der Loop auf eine TTS-Ansage um statt weiter stumm
    ins Leere zu klingeln (#116)."""

    def __init__(
        self,
        db: Callable,
        on_fire: Callable[[dict], None],
        play_asset_fn: Callable[[str, str], None],
        set_volume_fn: Callable[[str, int], None],
        get_volume_fn: Callable[[str], int],
        announce_fn: Optional[Callable[[str, str], None]] = None,
        asset_id: str = "alarm_ring",
        volume_low: int = 30,
        volume_high: int = 80,
        cycle_seconds: float = 4.0,
        fallback_text: str = "Wecker! Wecker!",
    ):
        """
        db: liefert eine pyorm.Database, z.B. hannah.utils.db.get_db.
        on_fire(record): Callback beim Auslösen (TTS-Ansage o.ä.) — bekommt den
            vollen Alarm-Record (dict), nicht nur die ID.
        play_asset_fn/set_volume_fn: mqtt_handler.publish_play_asset/publish_volume_set.
        get_volume_fn(satellite_id): aktuelle Lautstärke, für Restore nach dem Stoppen.
        announce_fn(satellite_id, text): TTS-Fallback für den Klingel-Loop, sobald ein
            play_asset-Versuch per on_play_result() ein Nack meldet (#116) — der Ring-Ton
            allein war komplett Fire-and-Forget und konnte bei einem defekten/fehlenden
            Asset-Cache still für immer ins Leere laufen. None = kein Fallback (alter
            Zustand, z.B. für Tests ohne MQTT-Ack-Wiring).
        """
        self._db = db
        self._on_fire = on_fire
        self._play_asset_fn = play_asset_fn
        self._set_volume_fn = set_volume_fn
        self._get_volume_fn = get_volume_fn
        self._announce_fn = announce_fn
        self._asset_id = asset_id
        self._volume_low = volume_low
        self._volume_high = volume_high
        self._cycle_seconds = cycle_seconds
        self._fallback_text = fallback_text

        self._timers: dict[int, threading.Timer] = {}
        self._lock = threading.Lock()

        self._ringing: dict[str, Optional[threading.Timer]] = {}
        self._ring_high: dict[str, bool] = {}
        self._pre_ring_volume: dict[str, int] = {}
        self._asset_broken: dict[str, bool] = {}
        self._ringing_lock = threading.Lock()

        self._load_and_reschedule_all()

    # ------------------------------------------------------------------
    # CRUD

    def get_alarm_records(self) -> list[dict]:
        """Alle Alarme als rohe Dicts — fürs gRPC-CRUD (WebUI) und QueryAlarms-Intent."""
        return [a.to_dict() for a in Alarm.select(self._db()).all()]

    def create_alarm(self, satellite_id: str, time: str, weekdays: Optional[list[int]],
                      one_shot_date: Optional[str], user_id: int, label: str = "") -> dict:
        """Legt einen Alarm an und plant ihn sofort ein. weekdays=None/[] → One-off
        (braucht one_shot_date). weekdays gesetzt → wiederkehrend."""
        weekdays = sorted(set(weekdays)) if weekdays else None
        a = Alarm.create(
            self._db(), satellite_id=satellite_id, time=time, weekdays=weekdays,
            skip_dates=[], one_shot_date=one_shot_date, enabled=1, label=label,
            user_id=user_id,
        )
        self._schedule(a.to_dict())
        log.info(f"[alarm] Wecker #{a.id} für '{satellite_id}' um {time} angelegt.")
        return a.to_dict()

    def update_alarm(self, id: int, satellite_id: str, time: str, weekdays: Optional[list[int]],
                      skip_dates: list[str], one_shot_date: Optional[str], enabled: bool,
                      label: str = "") -> bool:
        """Voll-Update (WebUI-CRUD). Reschedules intern."""
        a = Alarm.get(self._db(), id=id)
        if not a:
            return False
        weekdays = sorted(set(weekdays)) if weekdays else None
        a.update(
            satellite_id=satellite_id, time=time, weekdays=weekdays,
            skip_dates=list(skip_dates or []), one_shot_date=one_shot_date,
            enabled=1 if enabled else 0, label=label,
        )
        with self._lock:
            self._cancel_timer(id)
        if enabled:
            self._schedule(a.to_dict())
        return True

    def delete_alarm(self, id: int) -> bool:
        """Löscht die gesamte Serie (oder den einzelnen One-off). Cancelt den Timer."""
        a = Alarm.get(self._db(), id=id)
        if not a:
            return False
        with self._lock:
            self._cancel_timer(id)
        a.delete()
        log.info(f"[alarm] Wecker #{id} gelöscht.")
        return True

    def skip_occurrence(self, id: int, date: str) -> bool:
        """Fügt `date` (ISO) zu skip_dates einer wiederkehrenden Serie hinzu und
        reschedules um diesen Termin herum. Für One-offs: False (Aufrufer soll
        stattdessen delete_alarm nutzen — ein One-off hat nur einen Termin)."""
        a = Alarm.get(self._db(), id=id)
        if not a or not a.weekdays:
            return False
        skip = list(a.skip_dates or [])
        if date not in skip:
            skip.append(date)
        a.update(skip_dates=skip)
        self._schedule(a.to_dict())
        log.info(f"[alarm] Wecker #{id}: Termin {date} übersprungen.")
        return True

    def find_occurrences(self, satellite_id: Optional[str], on_date: datetime.date) -> list[dict]:
        """Alle Alarm-Records, die an `on_date` auslösen würden (weekdays+skip_dates für
        wiederkehrende, one_shot_date für One-offs). satellite_id=None → über alle
        Satelliten (Delete braucht keine Satelliten-Bindung, siehe #4)."""
        result = []
        for r in self.get_alarm_records():
            if not r["enabled"]:
                continue
            if satellite_id is not None and r["satellite_id"] != satellite_id:
                continue
            if r["weekdays"]:
                if on_date.weekday() in r["weekdays"] and on_date.isoformat() not in (r["skip_dates"] or []):
                    result.append(r)
            elif r["one_shot_date"] == on_date.isoformat():
                result.append(r)
        return result

    # ------------------------------------------------------------------
    # Scheduling (pro Alarm ein threading.Timer, kein Poll-Loop)

    def _compute_next_fire(self, record: dict) -> Optional[datetime.datetime]:
        h, m = map(int, record["time"].split(":"))
        now = datetime.datetime.now()
        weekdays = record.get("weekdays") or []
        if not weekdays:
            if not record.get("one_shot_date"):
                return None
            d = datetime.date.fromisoformat(record["one_shot_date"])
            dt = datetime.datetime.combine(d, datetime.time(h, m))
            return dt if dt > now else None
        skip = set(record.get("skip_dates") or [])
        for offset in range(8):  # +1 Tag: falls heute der einzige passende Wochentag ist und
            # die Uhrzeit schon vorbei ist, muss derselbe Wochentag nächste Woche (offset 7) greifen
            d = now.date() + datetime.timedelta(days=offset)
            if d.weekday() not in weekdays or d.isoformat() in skip:
                continue
            dt = datetime.datetime.combine(d, datetime.time(h, m))
            if dt > now:
                return dt
        return None

    def _schedule(self, record: dict) -> None:
        next_fire = self._compute_next_fire(record)
        with self._lock:
            self._cancel_timer(record["id"])
            if next_fire is None:
                return
            delay = max(0.0, (next_fire - datetime.datetime.now()).total_seconds())
            t = threading.Timer(delay, self._fire, args=(record["id"],))
            t.daemon = True
            t.start()
            self._timers[record["id"]] = t

    def _cancel_timer(self, id: int) -> None:
        """Muss unter self._lock aufgerufen werden."""
        t = self._timers.pop(id, None)
        if t:
            t.cancel()

    def _fire(self, id: int) -> None:
        a = Alarm.get(self._db(), id=id)
        with self._lock:
            self._timers.pop(id, None)
        if not a or not a.enabled:
            return
        record = a.to_dict()
        log.info(f"[alarm] Wecker #{id} ausgelöst → '{record['satellite_id']}'")
        self._on_fire(record)
        self._start_ringing(record["satellite_id"])
        if not record["weekdays"]:
            a.delete()  # One-off: konsumiert sich selbst, analog zum alten AlarmManager
        else:
            self._schedule(record)

    def _load_and_reschedule_all(self) -> None:
        """Lädt beim Start alle aktiven Alarme neu ein — Ersatz für die alte
        JSON-`_load()`. One-offs, deren Termin während des Downtimes verstrichen
        ist (nie gefeuert), werden gelöscht statt (wie früher) nur übersprungen."""
        for a in Alarm.select(self._db()).where(enabled=1).all():
            record = a.to_dict()
            if not record["weekdays"] and record.get("one_shot_date"):
                if self._compute_next_fire(record) is None:
                    log.info(f"[alarm] Wecker #{a.id} übersprungen — liegt in der Vergangenheit.")
                    a.delete()
                    continue
            self._schedule(record)

    # ------------------------------------------------------------------
    # Ringen ("klingelt gerade", per StopIntent abbrechbar — #4)

    def _start_ringing(self, satellite_id: str) -> None:
        with self._ringing_lock:
            if satellite_id in self._ringing:
                return  # schon am Klingeln (z.B. zwei Alarme kurz hintereinander auf demselben Satelliten)
            self._pre_ring_volume[satellite_id] = self._get_volume_fn(satellite_id)
            self._ring_high[satellite_id] = False
            self._asset_broken[satellite_id] = False
            self._ringing[satellite_id] = None  # Platzhalter, verhindert Re-Entry vor dem ersten Zyklus
        self._ringing_cycle(satellite_id)

    def _ringing_cycle(self, satellite_id: str) -> None:
        with self._ringing_lock:
            if satellite_id not in self._ringing:
                return  # inzwischen gestoppt
            high = not self._ring_high.get(satellite_id, False)
            self._ring_high[satellite_id] = high
            level = self._volume_high if high else self._volume_low
            asset_broken = self._asset_broken.get(satellite_id, False)
            t = threading.Timer(self._cycle_seconds, self._ringing_cycle, args=(satellite_id,))
            t.daemon = True
            self._ringing[satellite_id] = t
        self._set_volume_fn(satellite_id, level)
        if asset_broken and self._announce_fn:
            self._announce_fn(satellite_id, self._fallback_text)
        else:
            self._play_asset_fn(satellite_id, self._asset_id)
        t.start()

    def on_play_result(self, satellite_id: str, asset_id: str, ok: bool) -> None:
        """Reagiert auf das MQTT-Ack/Nack vom Satelliten (hannah_net.c/hannah_asset.c)
        für einen play_asset-Versuch (#116). Bei einem Nack während des Klingelns
        schaltet der Loop für diesen Satelliten von (kaputtem) Asset-Sound auf eine
        TTS-Ansage um — vorher blieb ein defektes/fehlendes Asset komplett unbemerkt
        und der Wecker klingelte still für immer ins Leere."""
        if asset_id != self._asset_id or ok:
            return
        with self._ringing_lock:
            if satellite_id not in self._ringing or self._asset_broken.get(satellite_id):
                return
            self._asset_broken[satellite_id] = True
        log.warning(f"[alarm] Weckton '{asset_id}' auf '{satellite_id}' fehlgeschlagen — falle auf TTS-Ansage zurück.")

    def stop_ringing(self, satellite_id: str) -> bool:
        """Bricht den Klingel-Loop ab und stellt die Lautstärke von vor dem Klingeln
        wieder her. Gibt True zurück wenn tatsächlich etwas gestoppt wurde."""
        with self._ringing_lock:
            was_ringing = satellite_id in self._ringing
            t = self._ringing.pop(satellite_id, None)
            self._ring_high.pop(satellite_id, None)
            self._asset_broken.pop(satellite_id, None)
            pre_volume = self._pre_ring_volume.pop(satellite_id, None)
        if t is not None:
            t.cancel()
        if pre_volume is not None:
            self._set_volume_fn(satellite_id, pre_volume)
        if was_ringing:
            log.info(f"[alarm] Klingeln auf '{satellite_id}' gestoppt.")
        return was_ringing

    def is_ringing(self, satellite_id: str) -> bool:
        with self._ringing_lock:
            return satellite_id in self._ringing

    def ringing_devices(self) -> list[str]:
        with self._ringing_lock:
            return list(self._ringing.keys())
