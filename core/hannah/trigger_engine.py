"""
Trigger-Engine — proaktive Ansagen und Fragen aus ioBroker-States und Zeitplänen.

Trigger liegen in der "triggers"-Tabelle (hannah.db, Modell `Trigger`), nicht mehr
in einer YAML-Datei. Spaltenform entspricht 1:1 der früheren YAML-Struktur (Beispiele
unten) — `when`/`cancel_when`/`on_response` sind JSON-Spalten (siehe `Trigger.__json_fields__`),
`delay` entspricht dem früheren `for`-Feld (Python-Keyword, daher umbenannt).

Frühere triggers.yaml-Form, zur Illustration der when/cancel_when/on_response-Struktur:

    triggers:
      # Zeit-Trigger (einmal pro Tag):
      - id: aussentuer_abend
        when:
          time: "23:00"
          days: [mon, tue, wed, thu, fri]   # optional; ohne days: täglich
        say: "Leonie, denk an die Außentüren."
        rephrase: true   # optional: LLM formuliert 'say' vor der Ausgabe um
        room: all

      # State-Trigger mit Zusatzbedingung:
      - id: fenster_kalt
        when:
          state: "javascript.0.virtualDevice.Fenster.Wohnzimmer.open"
          value: true
          also:
            state: "javascript.0.virtualDevice.Temperaturen.Wohnzimmer.Raumtemperatur.current"
            below: 12
          unless:
            state: "0_userdata.0.abwesend"
            value: true
        say: "Das Fenster ist noch offen und es wird kalt draußen."
        cooldown: 3600   # Sekunden, Standard 3600

      # Delay-Trigger — Aktion erst nach Ablauf einer Wartezeit (via Timer Service):
      - id: friteuse_lang_an
        when:
          state: "javascript.0.virtualDevice.Steckdosen.Friteuse.on"
          value: true
        for: "5h"            # Wartezeit: "5h" | "30m" | "90s"
        cancel_when:         # Timer canceln wenn Bedingung vorher nicht mehr zutrifft
          state: "javascript.0.virtualDevice.Steckdosen.Friteuse.on"
          value: false
        ask: "Die Friteuse ist seit 5 Stunden an. Soll ich sie ausschalten?"
        room: all
        on_response:
          - condition: 'llm_match("Zustimmung")'
            say: "Okay, ich schalte die Friteuse aus."
            set_state:       # optional: ioBroker-State direkt setzen
              id: "javascript.0.virtualDevice.Steckdosen.Friteuse.on"
              value: false
          - condition: 'llm_match("Verneinung")'
            say: "Alright, ich lasse sie an."
          - say: "Ich habe dich leider nicht verstanden."   # Fallback (keine condition)

Schlüsselfelder im Überblick:
  when                      — Dict (eine Bedingung) ODER Liste von Dicts (OR-verknüpft —
                              irgendeine reicht). Jedes Dict wie gewohnt: state/value,
                              above/below, time/days, phrase, plus optional also/unless.
  when.state / when.value   — State-Übergang auf exakten Wert
  when.above / when.below   — numerischer Schwellwert
  when.also                 — Dict (eine Bedingung) ODER Liste (UND, Alt-Verhalten) ODER
                              {"op": "and"|"or", "conditions": [...]} (neu, explizit wählbar)
  when.unless               — Sperrbedingung (State oder UND-Liste davon) — bleibt AND-only
  when.time / when.days     — Uhrzeit-Trigger (HH:MM, Wochentage: mon–sun)
  when.phrase                — Sprachphrase (Substring-Match auf normalisierten Text, vor NLU
                              geprüft, siehe match_phrase()). Synchron statt async: feuert nicht
                              über die Engine, sondern liefert die Antwort direkt als Rückgabewert
                              von match_phrase(). Kein Cooldown (#139 — Nachfolger der alten
                              "Routinen", die nie gedrosselt wurden), kein for/ask/on_response.
  for                       — Wartezeit vor Ausführung (Timer Service, SQLite-persistent)
  cancel_when               — bricht den Delay-Timer ab wenn Bedingung eintritt
  cooldown                  — Mindestabstand zwischen zwei Auslösungen (Standard: 3600s)
  say                       — TTS-Ansage (Legacy; ignoriert wenn actions gesetzt ist)
  actions                   — Liste von Aktionen, ersetzt say wenn nicht-leer:
                              [{"say": "...", "room": "..."} | {"set_state": {"id", "value"}}]
  ask                       — Frage per TTS; Antwort wird per on_response ausgewertet
  rephrase                  — LLM formuliert say/ask/actions[].say vor der Ausgabe um
  on_response               — Regeln nach ask; condition: llm_match("Kategorie")
  set_state                 — ioBroker-State in on_response setzen: {id, value}

Reload: triggers-Tabelle wird einmal pro Minute (Tick-Loop) und beim Start neu abgefragt —
SQL-Query ist immer aktuell, kein Hot-Reload-Mechanismus mehr nötig.
"""
import logging
import re
import sqlite3
import threading
import time
import uuid as _uuid
from datetime import date, datetime
from typing import Any, Callable, Optional

from hannah.models.trigger import Trigger

log = logging.getLogger(__name__)

_UMLAUT_MAP = {"ae": "ä", "oe": "ö", "ue": "ü", "Ae": "Ä", "Oe": "Ö", "Ue": "Ü"}


def _normalize(s: str) -> str:
    """Normalisiert Text für den Phrase-Match: lowercase, ae/oe/ue -> Umlaute."""
    s = s.lower()
    return re.sub(r"[AaOoUu]e", lambda m: _UMLAUT_MAP.get(m.group(), m.group()), s)


class TriggerEngine:
    def __init__(
        self,
        db: Callable,
        announce_fn: Callable[[str, str], None],
        rephrase_fn: Callable[[str], str] | None = None,
        ask_fn: Callable[[str, str, Callable[[str], None]], None] | None = None,
        match_fn: Callable[[str, str], bool] | None = None,
        set_state_fn: Callable[[str, Any], None] | None = None,
        schedule_timer_fn: Callable[[str, str, int, dict], None] | None = None,  # (timer_id, label, fire_at, metadata)
        cancel_timer_fn: Callable[[str], None] | None = None,                    # (timer_id)
        on_change: Callable[[], None] | None = None,                            # nach Create/Update: WatchMore neu pushen
    ):
        """
        db:            Callable → pyorm.Database (siehe hannah.utils.db.get_db)
        announce_fn:   fn(room, text) — ruft process_announcement() auf
        rephrase_fn:   fn(text) → text — LLM-Umformulierung; None = Feature deaktiviert
        ask_fn:        fn(room, question, callback) — stellt eine Frage per TTS und ruft
                       callback(answer_text) auf wenn der Nutzer antwortet
        match_fn:      fn(text, category) → bool — LLM-Klassifikation für on_response
        set_state_fn:  fn(state_id, value) — setzt einen ioBroker-State; für set_state in on_response
        on_change:     fn() — nach create_trigger/update_trigger; lässt den Aufrufer die aktuelle
                       Menge referenzierter State-IDs erneut per WatchMore an den Adapter pushen,
                       sonst würde ein frisch angelegter State-Trigger erst beim nächsten
                       Adapter-Reconnect live werden
        """
        self._db = db
        self._announce = announce_fn
        self._rephrase_fn = rephrase_fn
        self._ask_fn = ask_fn
        self._match_fn = match_fn
        self._set_state_fn = set_state_fn
        self._schedule_timer_fn = schedule_timer_fn
        self._cancel_timer_fn = cancel_timer_fn
        self._on_change = on_change
        self._triggers: list[dict] = []

        # State-Cache: {state_id: parsed_value} — wird von on_state_update befüllt
        self._state_cache: dict[str, object] = {}
        # Vorherige Werte für Transition-Erkennung: {state_id: value}
        self._prev_state: dict[str, object] = {}
        # Cooldown-Tracking: {trigger_id: last_fired_timestamp}
        self._last_fired: dict[str, float] = {}
        # Zeit-Trigger: {trigger_id: last_fired_date} — einmal pro Tag
        self._last_fired_date: dict[str, date] = {}
        # Laufende Delay-Timer: {trigger_id: timer_id} — in-memory, per reconcile nach Restart befüllt
        self._delay_timers: dict[str, str] = {}

        self._lock = threading.Lock()
        self._load()

        t = threading.Thread(target=self._tick_loop, daemon=True, name="trigger-engine")
        t.start()
        log.info("TriggerEngine gestartet.")

    # ------------------------------------------------------------------
    # Öffentliche Schnittstelle

    def fire_delayed(self, trigger_id: str) -> None:
        """Aufgerufen wenn ein Delay-Timer des Timer Service gefeuert hat."""
        with self._lock:
            timer_id = self._delay_timers.pop(trigger_id, None)
            triggers = list(self._triggers)

        if timer_id is None:
            log.debug(f"Trigger '{trigger_id}': fire_delayed ohne pending Timer (bereits gecancelt?) — ignoriert.")
            return

        trigger = next((t for t in triggers if t.get("id") == trigger_id), None)
        if trigger is None:
            log.warning(f"Trigger '{trigger_id}': fire_delayed aber Trigger nicht mehr in YAML — ignoriert.")
            return

        room = trigger.get("room", "all")
        log.info(f"Trigger '{trigger_id}': Delay abgelaufen → Aktion ausführen")
        self._execute_trigger_action(trigger, room)

    def reconcile_timers(self, timer_infos: list) -> None:
        """
        Verarbeitet TimerListResponse nach Reconnect zum Timer Service.
        Trigger-Timer werden im RAM wiederhergestellt oder gecancelt.
        """
        with self._lock:
            triggers_by_id = {t.get("id", ""): t for t in self._triggers}
            state_cache = dict(self._state_cache)

        for info in timer_infos:
            trigger_id = info.metadata.get("trigger_id")
            if not trigger_id:
                continue
            timer_id = info.timer_id

            trigger = triggers_by_id.get(trigger_id)
            if trigger is None:
                log.info(f"Reconcile: Timer '{timer_id}' (trigger_id={trigger_id!r}) — Trigger nicht mehr vorhanden → canceln")
                if self._cancel_timer_fn:
                    try:
                        self._cancel_timer_fn(timer_id)
                    except Exception as e:
                        log.error(f"Reconcile: cancel_timer_fn fehlgeschlagen: {e}")
                continue

            state_conds = [c for c in self._as_or_list(trigger.get("when", {}))
                           if c.get("state") and c["state"] in state_cache]
            if state_conds:
                condition_met = any(self._state_condition_matches(c, state_cache[c["state"]]) for c in state_conds)
            else:
                condition_met = True  # State unbekannt → konservativ behalten

            if condition_met:
                log.info(f"Reconcile: Trigger '{trigger_id}' Bedingung noch erfüllt → Timer wiederherstellen")
                with self._lock:
                    self._delay_timers[trigger_id] = timer_id
            else:
                log.info(f"Reconcile: Trigger '{trigger_id}' Bedingung nicht mehr erfüllt → Timer canceln")
                if self._cancel_timer_fn:
                    try:
                        self._cancel_timer_fn(timer_id)
                    except Exception as e:
                        log.error(f"Reconcile: cancel_timer_fn fehlgeschlagen: {e}")

    def get_referenced_state_ids(self) -> set[str]:
        """Gibt alle in Triggern referenzierten ioBroker-State-IDs zurück.
        Wird vom gRPC-Agent genutzt um den Adapter per WatchMore zu informieren."""
        ids: set[str] = set()
        with self._lock:
            for t in self._triggers:
                for cond in self._as_or_list(t.get("when", {})):
                    if "state" in cond:
                        ids.add(cond["state"])
                    self._collect_condition_state_ids(cond.get("unless"), ids)
                    self._collect_condition_state_ids(cond.get("also"), ids)
                cancel_when = t.get("cancel_when")
                if isinstance(cancel_when, dict) and "state" in cancel_when:
                    ids.add(cancel_when["state"])
        return ids

    def get_trigger_records(self) -> list[dict]:
        """Alle Trigger als rohe DB-Dicts (id, when, cancel_when, on_response, say, ask,
        rephrase, room, cooldown, delay) — fürs Admin-UI."""
        return [t.to_dict() for t in Trigger.select(self._db()).all()]

    def create_trigger(self, id: str, when: dict, cancel_when: Optional[dict], on_response: list, actions: list,
                        say: str, ask: str, rephrase: bool, room: str, cooldown: int, delay: str) -> bool:
        """Legt einen neuen Trigger an. Gibt False zurück wenn die ID bereits existiert."""
        try:
            Trigger.create(self._db(), id=id, when=when, cancel_when=cancel_when, on_response=on_response,
                            actions=actions, say=say, ask=ask, rephrase=rephrase, room=room, cooldown=cooldown,
                            delay=delay)
        except sqlite3.IntegrityError:
            return False
        self._load()
        if self._on_change:
            self._on_change()
        return True

    def update_trigger(self, id: str, when: dict, cancel_when: Optional[dict], on_response: list, actions: list,
                        say: str, ask: str, rephrase: bool, room: str, cooldown: int, delay: str) -> bool:
        t = Trigger.get(self._db(), id=id)
        if not t:
            return False
        t.update(when=when, cancel_when=cancel_when, on_response=on_response, actions=actions, say=say, ask=ask,
                  rephrase=rephrase, room=room, cooldown=cooldown, delay=delay)
        self._load()
        if self._on_change:
            self._on_change()
        return True

    def delete_trigger(self, id: str) -> bool:
        t = Trigger.get(self._db(), id=id)
        if not t:
            return False
        t.delete()
        self._load()
        return True

    @staticmethod
    def _collect_condition_state_ids(condition, ids: set[str]) -> None:
        if not condition:
            return
        if isinstance(condition, list):
            for c in condition:
                TriggerEngine._collect_condition_state_ids(c, ids)
        elif isinstance(condition, dict):
            if "conditions" in condition:
                TriggerEngine._collect_condition_state_ids(condition["conditions"], ids)
            elif "state" in condition:
                ids.add(condition["state"])

    @staticmethod
    def _as_or_list(when: dict | list) -> list[dict]:
        """Normalisiert 'when' auf eine Liste von Bedingungs-Dicts (OR-verknüpft).
        Alt-Format (einzelnes Dict) wird zur Einer-Liste — Verhalten bleibt identisch."""
        if isinstance(when, list):
            return when
        return [when] if when else []

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
            tid = trigger.get("id", "")

            cancel_when = trigger.get("cancel_when")
            if cancel_when and cancel_when.get("state") == state_id:
                if self._state_condition_matches(cancel_when, value):
                    self._cancel_delay(tid)

            for cond in self._as_or_list(trigger.get("when", {})):
                if cond.get("state") != state_id:
                    continue
                if not self._state_condition_matches(cond, value):
                    continue
                if not self._also_condition_matches(cond.get("also")):
                    continue
                if not self._unless_condition_matches(cond.get("unless")):
                    continue
                self._fire(trigger)
                break

    def match_phrase(self, text: str) -> Optional[str]:
        """
        Prüft, ob text eine when.phrase-Bedingung trifft — vom Sprach-/Text-Pfad synchron
        aufgerufen, VOR NLU (Nachfolger von RoutineManager.match(), #139).

        Bei Treffer werden die Trigger-Actions sofort ausgeführt (Seiteneffekte: Announce/
        set_state in anderen Räumen) und 'say' wird — anders als beim async _fire()-Pfad —
        nicht announced, sondern direkt als Antworttext zurückgegeben. Kein Cooldown: ein
        bewusst gesprochener Befehl soll jedes Mal wirken, nicht gedrosselt werden.
        """
        norm = _normalize(text)
        with self._lock:
            triggers = list(self._triggers)

        for trigger in triggers:
            for cond in self._as_or_list(trigger.get("when", {})):
                phrase = cond.get("phrase")
                if not phrase or phrase not in norm:
                    continue
                if not self._also_condition_matches(cond.get("also")):
                    continue
                if not self._unless_condition_matches(cond.get("unless")):
                    continue

                tid = trigger.get("id", "?")
                log.info(f"Trigger '{tid}' per Phrase getriggert: '{phrase}'")

                for action in (trigger.get("actions") or []):
                    self._execute_trigger_action_entry(action, tid, trigger.get("room", "all"),
                                                          bool(trigger.get("rephrase")))

                say = trigger.get("say", "").strip()
                if say and trigger.get("rephrase") and self._rephrase_fn:
                    try:
                        say = self._rephrase_fn(say) or say
                    except Exception as e:
                        log.warning(f"Trigger '{tid}': LLM-Rephrase fehlgeschlagen, nutze Original: {e}")
                return say or "Ok."
        return None

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
            matched = False
            for cond in self._as_or_list(trigger.get("when", {})):
                if cond.get("time") != now_str:
                    continue
                allowed_days = cond.get("days")
                if allowed_days is not None:
                    allowed_wds = [self._DAYS_MAP.get(str(d).lower(), -1) for d in allowed_days]
                    if today_wd not in allowed_wds:
                        continue
                if not self._unless_condition_matches(cond.get("unless")):
                    continue
                matched = True
                break
            if not matched:
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

        room = trigger.get("room", "all")
        if trigger.get("for"):
            self._schedule_delay(trigger, room)
        else:
            self._execute_trigger_action(trigger, room)

    def _execute_trigger_action(self, trigger: dict, room: str) -> None:
        """Führt die ask/actions/say-Aktion eines Triggers aus (ohne Cooldown-Prüfung)."""
        tid = trigger.get("id", "?")
        ask = trigger.get("ask", "").strip()
        say = trigger.get("say", "").strip()

        if ask:
            if not self._ask_fn:
                log.warning(f"Trigger '{tid}': 'ask' definiert aber ask_fn fehlt — Fallback auf say.")
                if say:
                    self._announce(room, say)
                return
            text = ask
            if trigger.get("rephrase") and self._rephrase_fn:
                try:
                    text = self._rephrase_fn(ask) or ask
                except Exception as e:
                    log.warning(f"Trigger '{tid}': LLM-Rephrase fehlgeschlagen, nutze Original: {e}")
            on_response = trigger.get("on_response", [])
            log.info(f"Trigger '{tid}' fragt → [{room}] \"{text}\"")
            try:
                self._ask_fn(room, text, lambda answer, _tid=tid, _room=room, _rules=on_response:
                             self._process_response(answer, _tid, _room, _rules))
            except Exception as e:
                log.error(f"Trigger '{tid}': ask_fn fehlgeschlagen: {e}")
            return

        actions = trigger.get("actions") or []
        if actions:
            rephrase = bool(trigger.get("rephrase"))
            for action in actions:
                self._execute_trigger_action_entry(action, tid, room, rephrase)
            return

        if not say:
            log.warning(f"Trigger '{tid}': weder 'say'/'actions' noch 'ask' definiert.")
            return

        text = say
        if trigger.get("rephrase") and self._rephrase_fn:
            try:
                text = self._rephrase_fn(say) or say
            except Exception as e:
                log.warning(f"Trigger '{tid}': LLM-Rephrase fehlgeschlagen, nutze Original: {e}")

        log.info(f"Trigger '{tid}' ausgelöst → [{room}] \"{text}\"")
        try:
            self._announce(room, text)
        except Exception as e:
            log.error(f"Trigger '{tid}': Announcement fehlgeschlagen: {e}")

    def _execute_trigger_action_entry(self, action: dict, tid: str, room: str, rephrase: bool) -> None:
        """Führt einen einzelnen Eintrag aus trigger['actions'] aus (say und/oder set_state)."""
        say = (action.get("say") or "").strip()
        if say:
            action_room = action.get("room") or room
            text = say
            if rephrase and self._rephrase_fn:
                try:
                    text = self._rephrase_fn(say) or say
                except Exception as e:
                    log.warning(f"Trigger '{tid}': LLM-Rephrase fehlgeschlagen, nutze Original: {e}")
            log.info(f"Trigger '{tid}' Aktion → [{action_room}] \"{text}\"")
            try:
                self._announce(action_room, text)
            except Exception as e:
                log.error(f"Trigger '{tid}': Announcement fehlgeschlagen: {e}")

        set_state = action.get("set_state")
        if set_state:
            if not self._set_state_fn:
                log.warning(f"Trigger '{tid}': set_state definiert aber set_state_fn fehlt — übersprungen.")
            elif isinstance(set_state, dict):
                state_id = (set_state.get("id") or "").strip()
                value = set_state.get("value")
                if state_id:
                    log.info(f"Trigger '{tid}' set_state → {state_id} = {value!r}")
                    try:
                        self._set_state_fn(state_id, value)
                    except Exception as e:
                        log.error(f"Trigger '{tid}': set_state fehlgeschlagen: {e}")

    def _schedule_delay(self, trigger: dict, room: str) -> None:
        """Registriert einen Delay-Timer beim Timer Service statt sofortiger Ausführung."""
        tid = trigger.get("id", "?")
        for_str = str(trigger.get("for", ""))

        if not self._schedule_timer_fn:
            log.warning(f"Trigger '{tid}': 'for' definiert aber schedule_timer_fn fehlt — sofortige Ausführung.")
            self._execute_trigger_action(trigger, room)
            return

        try:
            delay_secs = self._parse_duration(for_str)
        except (ValueError, TypeError) as e:
            log.error(f"Trigger '{tid}': Ungültiger 'for'-Wert {for_str!r}: {e} — übersprungen.")
            return

        with self._lock:
            if tid in self._delay_timers:
                log.debug(f"Trigger '{tid}': Delay-Timer läuft bereits, übersprungen.")
                return

        timer_id = str(_uuid.uuid4())
        fire_at = int(time.time()) + delay_secs
        label = f"Trigger: {tid}"
        log.info(f"Trigger '{tid}': Delay-Timer für {delay_secs}s registriert (timer_id={timer_id!r})")
        try:
            self._schedule_timer_fn(timer_id, label, fire_at, {"trigger_id": tid, "room": room})
        except Exception as e:
            log.error(f"Trigger '{tid}': schedule_timer_fn fehlgeschlagen: {e}")
            return

        with self._lock:
            self._delay_timers[tid] = timer_id

    def _cancel_delay(self, trigger_id: str) -> None:
        """Cancelt einen laufenden Delay-Timer (cancel_when erfüllt)."""
        with self._lock:
            timer_id = self._delay_timers.pop(trigger_id, None)
        if timer_id is None:
            return
        log.info(f"Trigger '{trigger_id}': Delay-Timer gecancelt (cancel_when erfüllt), timer_id={timer_id!r}")
        if self._cancel_timer_fn:
            try:
                self._cancel_timer_fn(timer_id)
            except Exception as e:
                log.error(f"Trigger '{trigger_id}': cancel_timer_fn fehlgeschlagen: {e}")

    def _process_response(self, answer: str, tid: str, room: str, rules: list) -> None:
        """Wertet on_response-Regeln aus und führt die erste passende Aktion aus."""
        log.info(f"Trigger '{tid}' Antwort erhalten für Raum '{room}': {answer!r}")
        fallback: Optional[dict] = None
        for rule in rules:
            condition = rule.get("condition", "").strip()
            if not condition:
                if fallback is None:
                    fallback = rule  # letzte Regel ohne Condition = Fallback
                continue
            m = re.match(r"""llm_match\(['"](.+)['"]\)""", condition)
            if not m:
                log.warning(f"Trigger '{tid}': unbekannte Condition {condition!r} — übersprungen.")
                continue
            category = m.group(1)
            if self._match_fn:
                if not self._match_fn(answer, category):
                    continue
            else:
                log.warning(f"Trigger '{tid}': llm_match benötigt match_fn — übersprungen.")
                continue
            self._execute_response_action(rule, tid, room)
            return
        if fallback is not None:
            self._execute_response_action(fallback, tid, room)

    def _execute_response_action(self, rule: dict, tid: str, room: str) -> None:
        say = rule.get("say", "").strip()
        if say:
            log.info(f"Trigger '{tid}' on_response → [{room}] \"{say}\"")
            try:
                self._announce(room, say)
            except Exception as e:
                log.error(f"Trigger '{tid}': on_response Announcement fehlgeschlagen: {e}")

        set_state = rule.get("set_state")
        if set_state:
            if not self._set_state_fn:
                log.warning(f"Trigger '{tid}': set_state definiert aber set_state_fn fehlt — übersprungen.")
            elif isinstance(set_state, dict):
                state_id = set_state.get("id", "").strip()
                value = set_state.get("value")
                if state_id:
                    log.info(f"Trigger '{tid}' set_state → {state_id} = {value!r}")
                    try:
                        self._set_state_fn(state_id, value)
                    except Exception as e:
                        log.error(f"Trigger '{tid}': set_state fehlgeschlagen: {e}")

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
        if "conditions" in also:
            conditions = also.get("conditions") or []
            combine = any if also.get("op") == "or" else all
            return combine(self._also_condition_matches(c) for c in conditions)
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
        try:
            rows = Trigger.select(self._db()).all()
            triggers = []
            for row in rows:
                d = row.to_dict()
                d["for"] = d.pop("delay", None)
                # NULL-Spalten (say/ask/on_response) sollen wie fehlende YAML-Keys
                # behandelt werden, nicht als None durchgereicht werden — sonst
                # crasht z.B. ask.strip() oder on_response wird nicht-iterierbar.
                d["say"] = d.get("say") or ""
                d["ask"] = d.get("ask") or ""
                d["on_response"] = d.get("on_response") or []
                d["actions"] = d.get("actions") or []
                triggers.append(d)
            with self._lock:
                self._triggers = triggers
            log.info(f"TriggerEngine: {len(triggers)} Trigger aus der Datenbank geladen")
        except Exception as e:
            log.error(f"TriggerEngine: Fehler beim Laden der Trigger aus der Datenbank: {e}")

    # ------------------------------------------------------------------
    # Helpers

    @staticmethod
    def _parse_duration(s: str) -> int:
        """Parst Dauer-Strings wie '5h', '30m', '90s' in Sekunden."""
        s = s.strip()
        if s.endswith("h"):
            return int(s[:-1]) * 3600
        if s.endswith("m"):
            return int(s[:-1]) * 60
        if s.endswith("s"):
            return int(s[:-1])
        return int(s)

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
