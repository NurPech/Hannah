"""
Trigger-Engine — proaktive Ansagen aus ioBroker-States und Zeitplänen.

Konfiguration in triggers.yaml:

    triggers:
      - id: aussentuer_abend
        when:
          time: "23:00"
        say: "Leonie, denk an die Außentüren."
        room: all

      - id: fenster_kalt
        when:
          state: "javascript.0.virtualDevice.Fenster.Wohnzimmer.open"
          value: true
          also:
            state: "javascript.0.virtualDevice.Temperaturen.Wohnzimmer.Raumtemperatur.current"
            below: 12
        say: "Das Fenster ist noch offen und es wird kalt draußen."
        cooldown: 3600   # Sekunden, Standard 3600

Hot-Reload: Dateiänderung wird beim nächsten Tick/State-Update erkannt.
"""
import logging
import os
import threading
import time
from datetime import date, datetime
from typing import Callable, Optional

import yaml

log = logging.getLogger(__name__)


class TriggerEngine:
    def __init__(self, path: str, announce_fn: Callable[[str, str], None]):
        """
        path:        Pfad zur triggers.yaml
        announce_fn: fn(room, text) — ruft process_announcement() auf
        """
        self._path = path
        self._announce = announce_fn
        self._triggers: list[dict] = []
        self._mtime: float = -1.0

        # State-Cache: {state_id: parsed_value} — wird von on_state_update befüllt
        self._state_cache: dict[str, object] = {}
        # Vorherige Werte für Transition-Erkennung: {state_id: value}
        self._prev_state: dict[str, object] = {}
        # Cooldown-Tracking: {trigger_id: last_fired_timestamp}
        self._last_fired: dict[str, float] = {}
        # Zeit-Trigger: {trigger_id: last_fired_date} — einmal pro Tag
        self._last_fired_date: dict[str, date] = {}

        self._lock = threading.Lock()
        self._load()

        t = threading.Thread(target=self._tick_loop, daemon=True, name="trigger-engine")
        t.start()
        log.info("TriggerEngine gestartet.")

    # ------------------------------------------------------------------
    # Öffentliche Schnittstelle

    def get_referenced_state_ids(self) -> set[str]:
        """Gibt alle in Triggern referenzierten ioBroker-State-IDs zurück.
        Wird vom gRPC-Agent genutzt um den Adapter per WatchMore zu informieren."""
        ids: set[str] = set()
        with self._lock:
            for t in self._triggers:
                when = t.get("when", {})
                if "state" in when:
                    ids.add(when["state"])
                self._collect_condition_state_ids(when.get("unless"), ids)
                self._collect_condition_state_ids(when.get("also"), ids)
        return ids

    @staticmethod
    def _collect_condition_state_ids(condition, ids: set[str]) -> None:
        if not condition:
            return
        if isinstance(condition, list):
            for c in condition:
                TriggerEngine._collect_condition_state_ids(c, ids)
        elif isinstance(condition, dict) and "state" in condition:
            ids.add(condition["state"])

    def on_state_update(self, state_id: str, raw: str) -> None:
        """Vom mqtt_handler aufgerufen wenn sich ein ioBroker-State ändert."""
        value = self._parse(raw)
        with self._lock:
            prev = self._prev_state.get(state_id)
            self._state_cache[state_id] = value
            self._prev_state[state_id] = value
            if prev == value:
                return  # kein Übergang, nichts prüfen
            triggers = list(self._triggers)

        for trigger in triggers:
            when = trigger.get("when", {})
            if "state" not in when:
                continue
            if when["state"] != state_id:
                continue
            if not self._state_condition_matches(when, value):
                continue
            if not self._also_condition_matches(when.get("also")):
                continue
            if not self._unless_condition_matches(when.get("unless")):
                continue
            self._fire(trigger)

    # ------------------------------------------------------------------
    # Tick-Loop für Zeit-Trigger

    def _tick_loop(self) -> None:
        while True:
            now = datetime.now()
            sleep_secs = 60 - now.second + 1
            time.sleep(sleep_secs)
            self._load()
            self._check_time_triggers()

    _DAYS_MAP = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

    def _check_time_triggers(self) -> None:
        now = datetime.now()
        now_str = now.strftime("%H:%M")
        today = now.date()
        today_wd = now.weekday()
        with self._lock:
            triggers = list(self._triggers)

        for trigger in triggers:
            when = trigger.get("when", {})
            if when.get("time") != now_str:
                continue
            allowed_days = when.get("days")
            if allowed_days is not None:
                allowed_wds = [self._DAYS_MAP.get(str(d).lower(), -1) for d in allowed_days]
                if today_wd not in allowed_wds:
                    continue
            if not self._unless_condition_matches(when.get("unless")):
                continue
            tid = trigger.get("id", "")
            with self._lock:
                if self._last_fired_date.get(tid) == today:
                    continue
                self._last_fired_date[tid] = today
            self._fire(trigger)

    # ------------------------------------------------------------------
    # Trigger auslösen

    def _fire(self, trigger: dict) -> None:
        tid = trigger.get("id", "?")
        cooldown = float(trigger.get("cooldown", 3600))
        now = time.monotonic()

        with self._lock:
            last = self._last_fired.get(tid, 0.0)
            if now - last < cooldown:
                log.debug(f"Trigger '{tid}' im Cooldown, übersprungen.")
                return
            self._last_fired[tid] = now

        say = trigger.get("say", "").strip()
        room = trigger.get("room", "all")
        if not say:
            log.warning(f"Trigger '{tid}': kein 'say' definiert.")
            return

        log.info(f"Trigger '{tid}' ausgelöst → [{room}] \"{say}\"")
        try:
            self._announce(room, say)
        except Exception as e:
            log.error(f"Trigger '{tid}': Announcement fehlgeschlagen: {e}")

    # ------------------------------------------------------------------
    # Bedingungen prüfen

    def _state_condition_matches(self, when: dict, value: object) -> bool:
        if "value" in when:
            expected = self._parse(str(when["value"]))
            return value == expected
        if "above" in when:
            try:
                return float(value) > float(when["above"])  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return False
        if "below" in when:
            try:
                return float(value) < float(when["below"])  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return False
        # Kein Wert-Filter → jeder Wechsel reicht
        return True

    def _also_condition_matches(self, also: Optional[dict | list]) -> bool:
        if not also:
            return True
        if isinstance(also, list):
            return all(self._also_condition_matches(a) for a in also)
        state_id = also.get("state")
        if not state_id:
            return True
        with self._lock:
            current = self._state_cache.get(state_id)
        if current is None:
            return False
        return self._state_condition_matches(also, current)

    def _unless_condition_matches(self, unless: Optional[dict | list]) -> bool:
        """Gibt True zurück wenn der Trigger feuern darf (unless-Bedingung NICHT erfüllt)."""
        if not unless:
            return True
        if isinstance(unless, list):
            return all(self._unless_condition_matches(u) for u in unless)
        state_id = unless.get("state")
        if not state_id:
            return True
        with self._lock:
            current = self._state_cache.get(state_id)
        if current is None:
            return True  # State unbekannt → nicht blockieren
        return not self._state_condition_matches(unless, current)

    # ------------------------------------------------------------------
    # Laden

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        mtime = os.path.getmtime(self._path)
        with self._lock:
            if mtime == self._mtime:
                return
        try:
            with open(self._path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            triggers = data.get("triggers", [])
            with self._lock:
                self._triggers = triggers
                self._mtime = mtime
            log.info(f"TriggerEngine: {len(triggers)} Trigger geladen aus '{self._path}'")
        except Exception as e:
            log.error(f"TriggerEngine: Fehler beim Laden von '{self._path}': {e}")

    # ------------------------------------------------------------------
    # Helpers

    @staticmethod
    def _parse(raw: str) -> object:
        s = str(raw).strip()
        if s.lower() == "true":
            return True
        if s.lower() == "false":
            return False
        try:
            return int(s)
        except ValueError:
            pass
        try:
            return float(s)
        except ValueError:
            pass
        return s
