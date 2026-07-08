import datetime
import re
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .iobroker import Device

log = logging.getLogger(__name__)

_STRIP_CHARS = re.compile(r"[.,!?;:()\[\]]")

_HOUR_WORDS: dict[str, int] = {
    "ein": 1, "eins": 1, "zwei": 2, "drei": 3, "vier": 4, "fuenf": 5,
    "sechs": 6, "sieben": 7, "acht": 8, "neun": 9, "zehn": 10,
    "elf": 11, "zwoelf": 12, "dreizehn": 13, "vierzehn": 14,
    "fuenfzehn": 15, "sechzehn": 16, "siebzehn": 17, "achtzehn": 18,
    "neunzehn": 19, "zwanzig": 20, "einundzwanzig": 21, "zweiundzwanzig": 22,
    "dreiundzwanzig": 23, "vierundzwanzig": 24,
}

_MINUTE_WORDS: dict[str, int] = {
    "null": 0, "fuenf": 5, "zehn": 10, "fuenfzehn": 15, "zwanzig": 20,
    "fuenfundzwanzig": 25, "dreissig": 30, "fuenfunddreissig": 35,
    "vierzig": 40, "fuenfundvierzig": 45, "fuenfzig": 50, "fuenfundfuenfzig": 55,
}

_WEEKDAYS_DE: dict[str, int] = {
    "montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3,
    "freitag": 4, "samstag": 5, "sonnabend": 5, "sonntag": 6,
}

def _normalize(s: str) -> str:
    """Normalisiert Text für NLU-Matching: Umlaute→Ascii, ß→ss, Kleinschreibung."""
    s = s.lower()
    s = s.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    s = s.replace("ß", "ss")
    return s
_FILLER = {
    "bitte", "mal", "doch", "denn", "einfach", "kannst", "du", "könntest",
    "mach", "die", "das", "den", "der", "hey", "hannah", "und",
}

# Farbnamen die gleichzeitig gebräuchliche deutsche Wörter/Verben sind.
# Ohne Gerätekontext (kein Raum, kein Gerät im Satz) werden diese ignoriert.
_AMBIGUOUS_COLORS: set[str] = {"weiß"}

# Farbnamen (Deutsch) → hex-Wert oder Sonderwert für colorTemp
_COLORS: dict[str, str] = {
    "rot":       "#FF0000",
    "grün":      "#00FF00",
    "blau":      "#0000FF",
    "gelb":      "#FFFF00",
    "orange":    "#FF8000",
    "lila":      "#8000FF",
    "pink":      "#FF69B4",
    "magenta":   "#FF00FF",
    "cyan":      "#00FFFF",
    "türkis":    "#00CED1",
    "weiß":      "#FFFFFF",
    "warmweiß":  "warm",
    "warm":      "warm",
    "kaltweiß":  "kalt",
    "kalt":      "kalt",
}


@dataclass
class Intent:
    name: str                          # TurnOn | TurnOff | SetLevel | SetColor | Query | Smalltalk | Unknown
    room: Optional[str] = None         # Anzeigename, z.B. "Wohnzimmer"
    room_id: Optional[str] = None      # Lookup-Key (normalisiert), z.B. "wohnzimmer"
    device: Optional[str] = None       # Originalname, z.B. "DeckeSeite"
    device_id: Optional[str] = None    # voller State-Prefix, z.B. "javascript.0...."
    category_filter: Optional[str] = None  # "Licht" | "Stecker" | None (= alle)
    query_state: Optional[str] = None  # "on" | "level" | "color" | None (= alles)
    value: Optional[object] = None     # float (SetLevel) | str (SetColor)
    unit: Optional[str] = None         # "%" | "color"
    label: Optional[str] = None        # Timer-Label, z.B. "Spazierengehen"
    raw_text: str = ""
    confidence: float = 1.0
    candidates: list = field(default_factory=list)  # [(room_id, room_name), ...] bei Mehrdeutigkeit
    weekdays: list = field(default_factory=list)     # [0-6, ...] SetAlarm: erkannter Wochentag (max. 1)
    resolved_date: Optional[object] = None           # datetime.date; DeleteAlarm: konkretes Zieldatum

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None and v != []}


class NLU:
    def __init__(self, cfg: dict, rooms: dict[str, str], devices: dict[str, dict]):
        """
        rooms  : {room_key: display_name}   — aus IoBrokerClient.rooms
        devices: {room_key: {device_key: Device}} — aus IoBrokerClient.devices
        """
        self._rooms = rooms
        self._devices = devices
        self._turn_on        = set(cfg.get("turn_on_words", []))
        self._turn_off       = set(cfg.get("turn_off_words", []))
        self._pct_units      = cfg.get("percentage_units", ["prozent", "%"])
        self._query          = set(cfg.get("query_words", []))
        self._category_words: dict[str, str] = cfg.get("category_words", {
            "licht":    "light",
            "lampe":    "light",
            "lampen":   "light",
            "stecker":  "socket",
            "strom":    "socket",
            "heizung":  "thermostat",
            "heizungen": "thermostat",
            "fenster":  "window",
            "tuer":     "door",
            "tueren":   "door",
            "rollladen":"blind",
            "klima":       "climate",
            "klimaanlage": "climate",
            "klimaanlagen":"climate",
        })
        # Wörter die auf Smalltalk hinweisen (persönliche Anrede / keine Gerätebezug)
        self._smalltalk_words: set[str] = set(cfg.get("smalltalk_words", [
            "dir", "dich", "dein", "deins", "deine", "deiner",
            "ich", "mir", "mich", "mein",
            "geht", "fuehlt", "bist", "heisst", "bitte", "liebst",
        ]))
        # Wörter die auf Abwesenheit hinweisen ("Ich gehe jetzt")
        self._presence_away: set[str] = set(cfg.get("presence_away_words", [
            "gehe", "weg", "tschuess", "tschues", "tschuss", "ciao",
            "verabschiede", "verlasse", "aufwiedersehen",
        ]))
        # Wörter die auf Heimkehr hinweisen ("Ich bin zuhause")
        self._presence_home: set[str] = set(cfg.get("presence_home_words", [
            "zuhause", "daheim", "heimgekommen", "angekommen",
            "zurueck", "wieder", "hallo",
        ]))

        # Wörter die auf eine Wetterabfrage hinweisen
        self._weather_words: set[str] = set(cfg.get("weather_words", [
            "wetter", "temperatur", "grad",
            "warm", "waerme", "heiss",
            "kalt", "kaelte", "kuehle", "kuehl",
            "regen", "regnet", "regnerisch",
            "schnee", "schneit",
            "wind", "windig", "sturm",
            "sonne", "sonnig", "scheint",
            "bewoelkt", "wolken", "wolkig", "nebel",
            "luftfeuchtigkeit", "luftdruck",
        ]))

        # Wörter die auf eine Auto-Abfrage hinweisen
        self._car_words: set[str] = set(cfg.get("car_words", [
            "auto", "wagen", "fahrzeug", "karre",
        ]))
        # scope-Wörter für CarQuery
        self._car_location_words: set[str] = {"wo", "steht", "parkiert", "geparkt", "position", "adresse"}
        self._car_security_words: set[str] = {"abgeschlossen", "gesperrt", "offen", "tuer", "fenster", "sicher"}
        self._car_range_words:    set[str] = {"reichweite", "weit", "kommt", "tankstand", "laden"}
        self._car_odometer_words: set[str] = {"kilometer", "km", "kilometerstand", "tachostand"}

        # Stop/Pause/Resume: Wiedergabe-Steuerung
        self._stop_words: set[str] = set(cfg.get("stop_words", [
            "stopp", "stop", "aufhoeren", "aufhoer", "abbrechen",
        ]))
        self._pause_words: set[str] = set(cfg.get("pause_words", [
            "pause", "pausieren", "pausiere", "pausier",
        ]))
        self._resume_words: set[str] = set(cfg.get("resume_words", [
            "weiter", "weitermachen", "weitermach", "weiterspielen", "fortsetzen", "fortfahren",
        ]))

        # DND: "nicht stören", "Ruhemodus", "DND"
        self._dnd_words: set[str] = set(cfg.get("dnd_words", [
            "stoeren", "dnd", "ruhemodus", "schlafmodus", "stille",
        ]))
        # Mute: "stumm", "Mikrofon aus"
        self._mute_words: set[str] = set(cfg.get("mute_words", [
            "stumm", "mikrofon",
        ]))
        # SetVolume: "Lautstärke auf 50" (absolut, kombiniert mit _find_level) oder
        # "lauter"/"leiser" (relativ) — #63
        self._volume_words: set[str] = set(cfg.get("volume_words", [
            "lautstaerke", "laut",
        ]))
        self._volume_up_words: set[str] = set(cfg.get("volume_up_words", [
            "lauter", "aufdrehen", "hochdrehen",
        ]))
        self._volume_down_words: set[str] = set(cfg.get("volume_down_words", [
            "leiser", "runterdrehen", "herunterdrehen",
        ]))

        # DeleteAlarm / QueryAlarms: nur im Wecker-Kontext relevant (#4)
        self._alarm_delete_words: set[str] = set(cfg.get("alarm_delete_words", [
            "loesche", "loeschen", "entferne", "entfernen",
        ]))
        self._alarm_query_words: set[str] = set(cfg.get("alarm_query_words", [
            "welche", "welchen", "welchem", "was", "liste", "zeig", "zeige",
        ]))

    def _split_compounds(self, text: str) -> str:
        """Trennt deutsche Komposita aus Raumteil + Kategorie.

        "Schlafzimmerlicht" → "Schlafzimmer Licht"

        Verwendet alle Einzelwörter aus bekannten Raumnamen als Prefixe und
        alle category_words-Keys als Suffixe, damit der Rest der NLU wie
        gewohnt matchen kann.
        """
        room_keywords = {
            word
            for name in self._rooms.values()
            for word in name.lower().split()
        }
        category_keywords = set(self._category_words.keys())

        result = []
        for word in text.split():
            w = word.lower()
            split_done = False
            for rk in room_keywords:
                if w.startswith(rk) and len(w) > len(rk):
                    suffix = w[len(rk):]
                    if suffix in category_keywords:
                        result.append(word[:len(rk)])
                        result.append(word[len(rk):])
                        split_done = True
                        break
            if not split_done:
                result.append(word)
        return " ".join(result)

    def parse(self, text: str) -> Intent:
        raw = text
        text = self._split_compounds(text)
        normalized = _STRIP_CHARS.sub(" ", text.lower())
        tokens = [t for t in normalized.split() if t not in _FILLER]
        joined = " ".join(tokens)

        room_key, room_name, room_candidates = self._find_room(joined)
        _, device                            = self._find_device(joined, room_key)
        action              = self._find_action(tokens)
        level               = self._find_level(normalized)
        temperature         = self._find_temperature(normalized)
        is_query            = self._is_query(tokens, raw)
        category_filter     = self._find_category(tokens)
        query_state         = self._find_query_state(joined) if is_query else None
        norm_tokens         = {_normalize(t) for t in tokens}
        climate_mode        = self._find_climate_mode(norm_tokens)
        fan_speed           = self._find_fan_speed(norm_tokens)
        _timer_trigger = bool({"timer"} & norm_tokens) or any(t.startswith("erinner") for t in norm_tokens)
        timer_seconds       = self._find_timer_seconds(raw) if _timer_trigger else None
        timer_label         = self._find_timer_label(raw) if timer_seconds is not None else None
        _alarm_context      = bool({"wecker", "alarm"} & norm_tokens)
        alarm_time          = self._find_alarm_time(raw) if _alarm_context else None
        alarm_weekday       = self._find_weekday(norm_tokens) if _alarm_context else None
        alarm_relative_date = self._find_relative_date(norm_tokens) if _alarm_context else None

        no_device_context = device is None and room_key is None and category_filter is None
        # Mehrdeutige Farbwörter (z.B. "weiß" = Verb) nur werten wenn Gerätekontext vorhanden
        color               = self._find_color(joined, require_context=no_device_context)

        # CarQuery: Auto-Wörter ohne Geräte-/Raumbezug

        # "alles/alle" als Wildcard — erlaubt TurnOn/TurnOff ohne spezifischen Raum/Gerät
        _has_all = no_device_context and bool({"alles", "alle"} & norm_tokens)
        # TurnOn/TurnOff nur wenn Raum, Gerät, Kategorie oder Wildcard vorhanden
        _has_action_context = not no_device_context or _has_all
        is_car = (
            no_device_context
            and bool(self._car_words & norm_tokens)
        )

        # WeatherQuery: Wetterwörter ohne Geräte-/Raumbezug
        is_weather = (
            not is_car
            and no_device_context
            and bool(self._weather_words & set(tokens))
        )

        # SetPresence: Kommen/Gehen ohne Geräte-/Raumbezug, kein Query
        # "Ich gehe schlafen" ist kein Presence-Event — Sleep-Wörter als Veto
        _has_sleep_words = bool({"schlafen", "schlaf", "bett", "nacht", "muede"} & norm_tokens)
        is_presence_away = (
            not is_query
            and no_device_context
            and not _has_sleep_words
            and bool(self._presence_away & norm_tokens)
        )
        is_presence_home = (
            not is_query
            and no_device_context
            and bool(self._presence_home & norm_tokens)
        )

        # Stop/Pause/Resume: kein spezifisches Gerät (Raum erlaubt), kein Query
        # Vorrang vor action-basierten Intents, weil "stoppe" auch in turn_off_words steht
        _has_off = action == "off"
        is_stop = (
            not is_car and not is_weather
            and not is_presence_away and not is_presence_home
            and not is_query and device is None
            and bool(self._stop_words & norm_tokens)
        )
        is_pause = (
            not is_car and not is_weather
            and not is_presence_away and not is_presence_home
            and not is_stop and not is_query and device is None
            and bool(self._pause_words & norm_tokens)
        )
        is_resume = (
            not is_car and not is_weather
            and not is_presence_away and not is_presence_home
            and not is_stop and not is_pause and not is_query and device is None
            and bool(self._resume_words & norm_tokens)
        )

        # SetDND / SetMute: ohne Geräte-/Raumbezug, kein Query
        is_dnd = (
            not is_car and not is_weather
            and not is_presence_away and not is_presence_home
            and no_device_context and not is_query
            and bool(self._dnd_words & norm_tokens)
        )
        is_mute_cmd = (
            not is_car and not is_weather
            and not is_presence_away and not is_presence_home
            and not is_dnd
            and no_device_context and not is_query
            and bool(self._mute_words & norm_tokens)
        )

        # SetVolume: absolut ("Lautstärke auf 50") oder relativ ("lauter"/"leiser").
        # Raum erlaubt (analog Stop/Pause/Resume), aber kein spezifisches ioBroker-Gerät.
        is_volume_up = bool(self._volume_up_words & norm_tokens)
        is_volume_down = bool(self._volume_down_words & norm_tokens)
        is_volume = (
            not is_car and not is_weather
            and not is_presence_away and not is_presence_home
            and not is_dnd and not is_mute_cmd
            and not is_query and device is None
            and (is_volume_up or is_volume_down
                 or (bool(self._volume_words & norm_tokens) and level is not None))
        )

        # DeleteAlarm / QueryAlarms: nur mit Wecker-Kontext (#4). is_delete_alarm hat
        # Vorrang, damit "lösche meinen Wecker für morgen 8 Uhr" nicht wegen des
        # enthaltenen alarm_time als SetAlarm durchgeht.
        is_delete_alarm = _alarm_context and bool(self._alarm_delete_words & norm_tokens)
        is_query_alarms = (
            _alarm_context and not is_delete_alarm
            and bool(self._alarm_query_words & norm_tokens)
        )

        # Smalltalk-Fallback: keine ausführbare Aktion, kein Spezial-Intent.
        # Wenn kein Gerätekontext (Raum/Gerät/Kategorie) vorliegt → immer Smalltalk.
        # Wenn ein Gerätekontext vorliegt aber kein Steuerbefehl ableitbar ist, dann
        # nur Smalltalk wenn der Satz echte Smalltalk-Wörter enthält ("Ich war im Keller").
        # Ohne Smalltalk-Wörter → Unknown, damit inherit_action Folgefragen auflösen kann.
        _has_smalltalk_words = bool(self._smalltalk_words & {_normalize(t) for t in tokens})
        is_smalltalk = (
            not is_car
            and not is_weather
            and not is_presence_away
            and not is_presence_home
            and not is_stop
            and not is_pause
            and not is_resume
            and not is_dnd
            and not is_mute_cmd
            and not is_volume
            and not is_delete_alarm
            and not is_query_alarms
            and (action is None or not _has_action_context)
            and level is None
            and temperature is None
            and color is None
            and climate_mode is None
            and fan_speed is None
            and timer_seconds is None
            and alarm_time is None
            and not (is_query and not no_device_context and not _has_smalltalk_words)
            and (no_device_context or _has_smalltalk_words)
        )

        intent_label: Optional[str] = None
        intent_weekdays: list[int] = []
        intent_resolved_date: Optional[datetime.date] = None

        if is_car:
            car_scope = self._find_car_scope(norm_tokens)
            intent_name, value, unit = "CarQuery", car_scope, None
        elif is_weather:
            weather_scope = self._find_weather_scope(tokens)
            intent_name, value, unit = "WeatherQuery", weather_scope, None
        elif is_presence_away:
            intent_name, value, unit = "SetPresence", "away", None
        elif is_presence_home:
            intent_name, value, unit = "SetPresence", "home", None
        elif is_stop:
            intent_name, value, unit = "StopIntent", None, None
        elif is_pause:
            intent_name, value, unit = "PauseIntent", None, None
        elif is_resume:
            intent_name, value, unit = "ResumeIntent", None, None
        elif is_dnd:
            intent_name, value, unit = "SetDND", "off" if _has_off else "on", None
        elif is_mute_cmd:
            intent_name, value, unit = "SetMute", "off" if _has_off else "on", None
        elif is_volume:
            if is_volume_up:
                intent_name, value, unit = "SetVolume", 10, "relative"
            elif is_volume_down:
                intent_name, value, unit = "SetVolume", -10, "relative"
            else:
                intent_name, value, unit = "SetVolume", level, "%"
        elif timer_seconds is not None:
            intent_name, value, unit = "SetTimer", timer_seconds, None
            intent_label = timer_label
        elif is_delete_alarm:
            intent_name, value, unit = "DeleteAlarm", alarm_time, None
            intent_resolved_date = alarm_relative_date or (
                self._weekday_to_next_date(alarm_weekday) if alarm_weekday is not None else None
            )
        elif is_query_alarms:
            intent_name, value, unit = "QueryAlarms", None, None
        elif alarm_time is not None:
            intent_name, value, unit = "SetAlarm", alarm_time, None
            intent_weekdays = [alarm_weekday] if alarm_weekday is not None else []
        elif is_smalltalk:
            intent_name, value, unit = "Smalltalk", None, None
        elif is_query and not no_device_context:
            intent_name, value, unit = "Query", None, None
        elif climate_mode is not None and not is_query:
            intent_name, value, unit = "SetMode", climate_mode, None
        elif fan_speed is not None and not is_query:
            intent_name, value, unit = "SetFanSpeed", fan_speed, None
        elif temperature is not None and not is_query:
            intent_name, value, unit = "SetTemperature", temperature, "°C"
        elif level is not None:
            intent_name, value, unit = "SetLevel", level, "%"
        elif color is not None:
            intent_name, value, unit = "SetColor", color, "color"
        elif action == "on" and _has_action_context:
            intent_name, value, unit = "TurnOn", None, None
        elif action == "off" and _has_action_context:
            intent_name, value, unit = "TurnOff", None, None
        else:
            intent_name, value, unit = "Unknown", None, None
            log.debug(f"NLU: Kein Intent erkannt für '{raw}'")

        _actionable = intent_name in ("TurnOn", "TurnOff", "SetLevel", "SetColor", "SetTemperature", "SetMode", "SetFanSpeed", "Query", "SetVolume")
        intent = Intent(
            name=intent_name,
            room=room_name,
            room_id=room_key,
            device=device.name if device else None,
            device_id=device.id if device else None,
            category_filter=category_filter,
            query_state=query_state,
            value=value,
            unit=unit,
            label=intent_label,
            raw_text=raw,
            candidates=room_candidates if _actionable else [],
            weekdays=intent_weekdays,
            resolved_date=intent_resolved_date,
        )
        log.debug(f"NLU: {intent}")
        return intent

    # ------------------------------------------------------------------

    def _find_room(self, text: str) -> tuple[Optional[str], Optional[str], list[tuple[str, str]]]:
        """
        Gibt (room_key, room_name, candidates) zurück.
        candidates ist leer wenn eindeutig, sonst alle Räume mit gleichem Treffscore.
        """
        norm_text = _normalize(text)

        # 1. Vollständiger Name-Match — immer eindeutig (längster gewinnt)
        # Match on display name, not room_id key
        best_key = best_name = None
        best_len = 0
        for key, name in self._rooms.items():
            norm_name = _normalize(name)
            if norm_name in norm_text and len(norm_name) > best_len:
                best_key, best_name, best_len = key, name, len(norm_name)
        if best_key:
            return best_key, best_name, []

        # 2. Partieller Match — alle Räume mit gleichem Treffscore sammeln
        text_words = set(norm_text.split())
        scored: list[tuple[str, str, int]] = []  # (key, name, score)
        best_score = 0
        for key, name in self._rooms.items():
            norm_name = _normalize(name)
            score = sum(1 for w in norm_name.split() if w in text_words)
            if score > 0:
                scored.append((key, name, score))
                if score > best_score:
                    best_score = score

        if not scored:
            return None, None, []

        tied = [(k, n) for k, n, s in scored if s == best_score]

        # Eindeutig oder Tiebreak per Namenslänge
        best = max(tied, key=lambda x: len(_normalize(x[1])))
        log.debug(f"NLU: Raum-Match '{best[1]}' (score={best_score}, ties={len(tied)})")
        candidates = tied if len(tied) > 1 else []
        return best[0], best[1], candidates

    def _find_device(self, text: str, room_key: Optional[str]) -> tuple[Optional[str], Optional["Device"]]:
        """
        Sucht Gerät zuerst im erkannten Raum, dann raumübergreifend.
        Längster Treffer gewinnt um Teilstring-Konflikte zu vermeiden.
        """
        norm_text = _normalize(text)
        candidates: list[dict] = []
        if room_key and room_key in self._devices:
            candidates.append(self._devices[room_key])
        for rk, devs in self._devices.items():
            if rk != room_key:
                candidates.append(devs)

        norm_room = _normalize(room_key) if room_key else ""
        for space in candidates:
            best_key = best_dev = None
            best_len = 0
            for key, dev in space.items():
                norm_key = _normalize(key)
                # Gerät überspringen wenn sein Key vollständig im Raum-Key enthalten ist
                # ("schlafzimmer" als Gerät soll nicht matchen wenn Raum "leonie schlafzimmer" ist)
                if norm_room and norm_key in norm_room:
                    continue
                if re.search(r'(?<!\w)' + re.escape(norm_key) + r'(?!\w)', norm_text) and len(norm_key) > best_len:
                    best_key, best_dev, best_len = key, dev, len(norm_key)
            if best_dev:
                return best_key, best_dev

        return None, None

    def _find_action(self, tokens: list[str]) -> Optional[str]:
        for t in tokens:
            if t in self._turn_on:
                return "on"
            if t in self._turn_off:
                return "off"
        return None

    def _find_temperature(self, text: str) -> Optional[float]:
        """Erkennt Temperaturangaben: '21 Grad', '21,5°C', '20 Grad Celsius'."""
        pattern = r"(\d+(?:[.,]\d+)?)\s*(?:grad(?:\s+celsius)?|°c)"
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return float(match.group(1).replace(",", "."))
        return None

    def _find_level(self, text: str) -> Optional[float]:
        pattern = r"(\d+(?:[.,]\d+)?)\s*(?:" + "|".join(re.escape(u) for u in self._pct_units) + r")"
        match = re.search(pattern, text)
        if match:
            return float(match.group(1).replace(",", "."))
        return None

    def _find_timer_seconds(self, text: str) -> Optional[int]:
        """Erkennt Zeitangaben: '20 Minuten', '1 Stunde 30 Minuten', '90 Sekunden'."""
        t = text.lower()
        total = 0
        for pattern, factor in (
            (r"(\d+(?:[.,]\d+)?)\s*stund(?:en|e)?", 3600),
            (r"(\d+(?:[.,]\d+)?)\s*minut(?:en|e)?", 60),
            (r"(\d+(?:[.,]\d+)?)\s*sekund(?:en|e)?", 1),
        ):
            m = re.search(pattern, t)
            if m:
                total += int(float(m.group(1).replace(",", ".")) * factor)
        if re.search(r"eineinhalb\s+stund", t):
            total += 5400
        if re.search(r"eineinhalb\s+minut", t):
            total += 90
        return total if total > 0 else None

    def _find_timer_label(self, text: str) -> Optional[str]:
        """Extrahiert Label aus 'erinnere mich in X Minuten an Y' → 'Y'."""
        t = text.lower()
        m = re.search(r'\d+(?:[.,]\d+)?\s*(?:stund|minut|sekund)\w*', t)
        if not m:
            return None
        rest = t[m.end():]
        lm = re.search(r'\bans?\s+(.+)', rest)
        if not lm:
            return None
        label = re.sub(r'^(?:den|die|das|dem|der|einen|einem|ein)\s+', '', lm.group(1).strip())
        return label.strip() if label.strip() else None

    def _find_climate_mode(self, norm_tokens: set[str]) -> Optional[str]:
        """Erkennt Klimaanlagen-Betriebsmodus aus normalisierten Tokens."""
        if norm_tokens & {"kuehlen", "kuehl", "kuehlung", "kuehlmodus"}:
            return "cool"
        if norm_tokens & {"heizen", "heizbetrieb", "aufwaermen"}:
            return "heat"
        if norm_tokens & {"trocknen", "trocken", "entfeuchten", "dry"}:
            return "dry"
        if norm_tokens & {"lueften", "lueftung", "ventilator", "fan"}:
            return "fan_only"
        if "auto" in norm_tokens:
            return "auto"
        return None

    def _find_alarm_time(self, text: str) -> Optional[str]:
        """Erkennt Uhrzeitangaben und gibt 'HH:MM' zurück.

        Unterstützt: '7:30', '7:30 Uhr', 'halb acht', 'Viertel nach sieben',
        'Viertel vor acht', 'dreiviertel acht', 'sieben Uhr dreißig', 'um sieben Uhr'.
        """
        t = _normalize(text)

        # 1. Numerisch: "7:30" oder "07:30"
        m = re.search(r'\b(\d{1,2}):(\d{2})\b', text)
        if m:
            h, mi = int(m.group(1)), int(m.group(2))
            if 0 <= h <= 23 and 0 <= mi <= 59:
                return f"{h:02d}:{mi:02d}"

        # 2. "halb X" → X-1:30  (z.B. "halb acht" = 07:30)
        m = re.search(r'\bhalb\s+(\w+)', t)
        if m:
            h = _HOUR_WORDS.get(m.group(1))
            if h is not None:
                return f"{(h - 1) % 24:02d}:30"

        # 3. "dreiviertel X" oder "viertel vor X" → X-1:45
        m = re.search(r'\b(?:dreiviertel|viertel\s+vor)\s+(\w+)', t)
        if m:
            h = _HOUR_WORDS.get(m.group(1))
            if h is not None:
                return f"{(h - 1) % 24:02d}:45"

        # 4. "viertel nach X" → X:15
        m = re.search(r'\bviertel\s+nach\s+(\w+)', t)
        if m:
            h = _HOUR_WORDS.get(m.group(1))
            if h is not None:
                return f"{h % 24:02d}:15"

        # 5. "X uhr Y" → X:Y  (Y als Zahlwort oder Ziffer)
        m = re.search(r'\b(\w+)\s+uhr\s+(\w+)', t)
        if m:
            h = _HOUR_WORDS.get(m.group(1)) or (int(m.group(1)) if m.group(1).isdigit() else None)
            mi_str = m.group(2)
            mi = int(mi_str) if mi_str.isdigit() else _MINUTE_WORDS.get(mi_str)
            if h is not None and mi is not None and 0 <= h <= 23 and 0 <= mi <= 59:
                return f"{h:02d}:{mi:02d}"

        # 6. "X uhr" → X:00
        m = re.search(r'\b(\w+)\s+uhr\b', t)
        if m:
            h = _HOUR_WORDS.get(m.group(1))
            if h is None and m.group(1).isdigit():
                h = int(m.group(1))
            if h is not None and 0 <= h <= 23:
                return f"{h:02d}:00"

        # 7. "um X" (reines Zahlwort, ohne "Uhr") → X:00
        m = re.search(r'\bum\s+(\w+)', t)
        if m:
            h = _HOUR_WORDS.get(m.group(1))
            if h is not None:
                return f"{h:02d}:00"

        return None

    def _find_weekday(self, norm_tokens: set[str]) -> Optional[int]:
        """Erkennt einen einzelnen Wochentag aus normalisierten Tokens (0=Mo..6=So).
        Nur EIN Wochentag pro Äußerung wird unterstützt — der SetAlarm-Handler fragt bei
        einem einzelnen Tag explizit nach einer Mo-Fr-Erweiterung nach (#4)."""
        for word, idx in _WEEKDAYS_DE.items():
            if word in norm_tokens:
                return idx
        return None

    def _find_relative_date(self, norm_tokens: set[str]) -> Optional[datetime.date]:
        """'heute'/'morgen'/'übermorgen' → konkretes Datum, sonst None (Wochentage werden
        separat in _find_weekday erkannt und vom Aufrufer zu einem Datum aufgelöst)."""
        today = datetime.date.today()
        if "morgen" in norm_tokens:
            return today + datetime.timedelta(days=1)
        if "uebermorgen" in norm_tokens:
            return today + datetime.timedelta(days=2)
        if "heute" in norm_tokens:
            return today
        return None

    def _weekday_to_next_date(self, weekday: int) -> datetime.date:
        """Nächstes Datum (heute oder später) für den gegebenen Wochentag (0=Mo..6=So)."""
        today = datetime.date.today()
        delta = (weekday - today.weekday()) % 7
        return today + datetime.timedelta(days=delta)

    def _find_fan_speed(self, norm_tokens: set[str]) -> Optional[str]:
        """Erkennt Lüftergeschwindigkeit aus normalisierten Tokens."""
        if norm_tokens & {"leise", "langsam", "niedrig", "schwach"}:
            return "low"
        if norm_tokens & {"mittel", "mittelschnell"}:
            return "medium"
        if norm_tokens & {"schnell", "stark", "hoch", "voll", "maximum", "maximal"}:
            return "high"
        if "auto" in norm_tokens:
            return "auto"
        return None

    def _find_color(self, text: str, require_context: bool = False) -> Optional[str]:
        best_val = None
        best_len = 0
        best_word = None
        for word, hex_val in _COLORS.items():
            if re.search(r'(?<!\w)' + re.escape(word) + r'(?!\w)', text) and len(word) > best_len:
                best_val, best_len, best_word = hex_val, len(word), word
        if require_context and best_word in _AMBIGUOUS_COLORS:
            return None
        return best_val

    def _find_category(self, tokens: list[str]) -> Optional[str]:
        """Erkennt Kategorie-Filter: 'licht'/'lampe' → 'Licht', 'stecker' → 'Stecker'."""
        for t in tokens:
            cat = self._category_words.get(_normalize(t))
            if cat:
                return cat
        return None

    def _is_query(self, tokens: list[str], raw: str) -> bool:
        """Erkennt Fragen anhand von Fragewörtern oder Fragezeichen."""
        if raw.strip().endswith("?"):
            return True
        return bool(self._query & set(tokens))

    def _find_weather_scope(self, tokens: list[str]) -> str:
        """Gibt 'tomorrow', 'week' oder 'today' zurück."""
        normalized = {_normalize(t) for t in tokens}
        if "morgen" in normalized:
            return "tomorrow"
        week_words = {"woche", "naechsten", "tage", "naechste", "kommenden"}
        if normalized & week_words:
            return "week"
        return "today"

    def _find_car_scope(self, norm_tokens: set[str]) -> str:
        """Gibt 'location', 'security', 'range', 'odometer' oder 'all' zurück."""
        if norm_tokens & self._car_location_words:
            return "location"
        if norm_tokens & self._car_security_words:
            return "security"
        if norm_tokens & self._car_range_words:
            return "range"
        if norm_tokens & self._car_odometer_words:
            return "odometer"
        return "all"

    def _find_query_state(self, text: str) -> Optional[str]:
        """
        Leitet ab welcher State abgefragt wird.
        Gibt "on", "level", "color", "power" zurück oder None (= allgemein / kategorie-basiert).
        Sensor-Kategorien (Temperaturen, Fenster, Helligkeit) werden über category_filter
        aufgelöst, nicht über query_state.
        """
        if any(w in text for w in ("hell", "helligkeit", "prozent", "dimm", "level")):
            return "level"
        if any(w in text for w in ("farbe", "color", "farbig")):
            return "color"
        # "power" hat Vorrang vor "on": Steckdosen sind gleichzeitig an/aus-schaltbar
        # und haben einen Watt-Wert — "wie viel Strom braucht..." soll den Watt-Wert
        # liefern, nicht nur den Schaltzustand (#121).
        if any(w in text for w in ("watt", "leistung", "strom", "verbrauch")):
            return "power"
        if any(w in text for w in ("an", "aus", "ein", "status", "zustand")):
            return "on"
        return None


# ── Rückfragen-Helfer ──────────────────────────────────────────────────────────

_ORDINALS: dict[str, int] = {
    "1": 0, "erste": 0, "ersten": 0, "erster": 0, "erstere": 0,
    "2": 1, "zweite": 1, "zweiten": 1, "zweiter": 1, "letztere": 1,
    "3": 2, "dritte": 2, "dritten": 2, "dritter": 2,
    "4": 3, "vierte": 3, "vierten": 3,
}


def build_clarification_question(candidates: list[tuple[str, str]]) -> str:
    names = [name for _, name in candidates]
    if len(names) == 2:
        return f"Welchen Raum meinst du — {names[0]} oder {names[1]}?"
    options = ", ".join(names[:-1]) + " oder " + names[-1]
    return f"Welchen Raum meinst du? {options}?"


def resolve_clarification_answer(
    text: str, candidates: list[tuple[str, str]]
) -> Optional[tuple[str, str]]:
    """Gibt (room_id, room_name) zurück oder None wenn keine Zuordnung möglich."""
    norm = _normalize(text)
    words = set(norm.split())

    for word, idx in _ORDINALS.items():
        if word in words and idx < len(candidates):
            return candidates[idx]

    best: Optional[tuple[str, str]] = None
    best_score = 0
    for room_id, room_name in candidates:
        score = sum(1 for w in _normalize(room_id).split() if w in words)
        score += sum(1 for w in _normalize(room_name).split() if w in words)
        if score > best_score:
            best_score = score
            best = (room_id, room_name)

    return best if best_score > 0 else None


_YES_WORDS = {"ja", "jep", "jup", "jo", "klar", "gerne", "genau", "positiv", "mach"}
_NO_WORDS  = {"nein", "ne", "nee", "negativ", "lass"}


def resolve_yes_no(text: str) -> Optional[bool]:
    """Gibt True/False zurück, oder None wenn weder eindeutig Ja noch Nein erkannt wurde.
    Für Ja/Nein-Rückfragen (z.B. Wecker-Serie-Erweiterung/-Löschung, #4) — NICHT für
    Raum-Klarifizierung, siehe resolve_clarification_answer."""
    words = set(_normalize(text).split())
    if words & _NO_WORDS:
        return False
    if words & _YES_WORDS:
        return True
    return None
