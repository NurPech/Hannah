import re
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .iobroker import Device

log = logging.getLogger(__name__)

_STRIP_CHARS = re.compile(r"[.,!?;:()\[\]]")

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
    raw_text: str = ""
    confidence: float = 1.0
    candidates: list = field(default_factory=list)  # [(room_id, room_name), ...] bei Mehrdeutigkeit

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
            "licht":   "Licht",
            "lampe":   "Licht",
            "lampen":  "Licht",
            "stecker": "Stecker",
            "strom":   "Stecker",
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

    def parse(self, text: str) -> Intent:
        raw = text
        normalized = _STRIP_CHARS.sub(" ", text.lower())
        tokens = [t for t in normalized.split() if t not in _FILLER]
        joined = " ".join(tokens)

        room_key, room_name, room_candidates = self._find_room(joined)
        _, device                            = self._find_device(joined, room_key)
        action              = self._find_action(tokens)
        level               = self._find_level(normalized)
        color               = self._find_color(joined)
        is_query            = self._is_query(tokens, raw)
        category_filter     = self._find_category(tokens)

        query_state = self._find_query_state(joined) if is_query else None

        no_device_context = device is None and room_key is None and category_filter is None

        # CarQuery: Auto-Wörter ohne Geräte-/Raumbezug
        norm_tokens = {_normalize(t) for t in tokens}

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
        is_presence_away = (
            not is_query
            and no_device_context
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
            and (action is None or not _has_action_context)
            and level is None
            and color is None
            and not (is_query and not no_device_context and not _has_smalltalk_words)
            and (no_device_context or _has_smalltalk_words)
        )

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
        elif is_smalltalk:
            intent_name, value, unit = "Smalltalk", None, None
        elif is_query and not no_device_context:
            intent_name, value, unit = "Query", None, None
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

        _actionable = intent_name in ("TurnOn", "TurnOff", "SetLevel", "SetColor", "Query")
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
            raw_text=raw,
            candidates=room_candidates if _actionable else [],
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

        # 1. Vollständiger Key-Match — immer eindeutig (längster gewinnt)
        best_key = best_name = None
        best_len = 0
        for key, name in self._rooms.items():
            norm_key = _normalize(key)
            if norm_key in norm_text and len(norm_key) > best_len:
                best_key, best_name, best_len = key, name, len(norm_key)
        if best_key:
            return best_key, best_name, []

        # 2. Partieller Match — alle Räume mit gleichem Treffscore sammeln
        text_words = set(norm_text.split())
        scored: list[tuple[str, str, int]] = []  # (key, name, score)
        best_score = 0
        for key, name in self._rooms.items():
            norm_key = _normalize(key)
            score = sum(1 for w in norm_key.split() if w in text_words)
            if score > 0:
                scored.append((key, name, score))
                if score > best_score:
                    best_score = score

        if not scored:
            return None, None, []

        tied = [(k, n) for k, n, s in scored if s == best_score]

        # Eindeutig oder Tiebreak per Key-Länge
        best = max(tied, key=lambda x: len(_normalize(x[0])))
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
                if norm_key in norm_text and len(norm_key) > best_len:
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

    def _find_level(self, text: str) -> Optional[float]:
        pattern = r"(\d+(?:[.,]\d+)?)\s*(?:" + "|".join(re.escape(u) for u in self._pct_units) + r")"
        match = re.search(pattern, text)
        if match:
            return float(match.group(1).replace(",", "."))
        return None

    def _find_color(self, text: str) -> Optional[str]:
        best_val = None
        best_len = 0
        for word, hex_val in _COLORS.items():
            if re.search(r'(?<!\w)' + re.escape(word) + r'(?!\w)', text) and len(word) > best_len:
                best_val, best_len = hex_val, len(word)
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
        Gibt "on", "level", "color" zurück oder None (= allgemein / kategorie-basiert).
        Sensor-Kategorien (Temperaturen, Fenster, Helligkeit) werden über category_filter
        aufgelöst, nicht über query_state.
        """
        if any(w in text for w in ("hell", "helligkeit", "prozent", "dimm", "level")):
            return "level"
        if any(w in text for w in ("farbe", "color", "farbig")):
            return "color"
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
