import logging
import threading
from typing import Optional

log = logging.getLogger(__name__)


class WeatherCache:
    """
    Empfängt Wetter-Updates aus ioBroker via MQTT (openweathermap/0/forecast/#)
    und baut natürlichsprachliche Antworten daraus.

    Topic-Struktur (Suffix nach dem konfigurierten Prefix):
      current/temperature, current/state, current/windSpeed, ...
      day0/temperatureMax, day0/precipitationRain, ...
      day1/..., day2/..., ..., day6/...
    """

    def __init__(self, topic_prefix: str = "openweathermap/0/forecast"):
        self.topic_prefix = topic_prefix
        self._cache: dict[str, dict[str, object]] = {}
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return bool(self._cache.get("current"))

    def update(self, topic: str, raw_value: str):
        """Verarbeitet ein eingehendes MQTT-Topic."""
        suffix = topic[len(self.topic_prefix):].strip("/")
        parts = suffix.split("/", 1)
        if len(parts) != 2:
            return
        bucket, key = parts
        value: object
        try:
            value = float(raw_value)
        except (ValueError, TypeError):
            value = raw_value.strip()
        with self._lock:
            self._cache.setdefault(bucket, {})[key] = value
        log.debug(f"Wetter: {bucket}/{key} = {value!r}")

    def build_answer(self, scope: str = "today") -> str:
        """
        scope: "today" | "tomorrow" | "week"
        """
        if scope == "tomorrow":
            return self._answer_tomorrow()
        if scope == "week":
            return self._answer_week()
        return self._answer_today()

    # ------------------------------------------------------------------

    def _answer_today(self) -> str:
        with self._lock:
            current = dict(self._cache.get("current", {}))
            day0    = dict(self._cache.get("day0", {}))

        if not current:
            return "Ich habe leider keine aktuellen Wetterdaten."

        parts: list[str] = []

        # "Aktuell 8 Grad, überwiegend bewölkt."
        temp  = current.get("temperature")
        state = current.get("state") or current.get("title")
        line1: list[str] = []
        if temp is not None:
            line1.append(f"Aktuell {round(float(temp))} Grad")
        if state and isinstance(state, str):
            line1.append(state.lower())
        if line1:
            parts.append(", ".join(line1))

        # "Heute bis zu 12 Grad."
        temp_max = day0.get("temperatureMax")
        if temp_max is not None:
            parts.append(f"Heute bis zu {round(float(temp_max))} Grad")

        # Regen
        rain = _as_float(day0.get("precipitationRain") or current.get("precipitationRain"))
        if rain is not None and rain > 0:
            parts.append("Regen erwartet")

        # Wind nur wenn spürbar (>= 7 m/s ≈ 25 km/h)
        wind = _as_float(current.get("windSpeed"))
        if wind is not None and wind >= 7:
            parts.append(_wind_text(wind, current.get("windDirectionText")))

        return ". ".join(parts) + "."

    def _answer_tomorrow(self) -> str:
        with self._lock:
            day1 = dict(self._cache.get("day1", {}))

        if not day1:
            return "Ich habe leider keine Vorhersage für morgen."

        return _day_sentence("Morgen", day1)

    def _answer_week(self) -> str:
        with self._lock:
            days = [
                dict(self._cache.get(f"day{i}", {}))
                for i in range(1, 7)
            ]

        days = [d for d in days if d]
        if not days:
            return "Ich habe leider keine Wochenvorhersage."

        # Temperaturspanne über alle Tage
        mins = [_as_float(d.get("temperatureMin")) for d in days]
        maxs = [_as_float(d.get("temperatureMax")) for d in days]
        mins = [v for v in mins if v is not None]
        maxs = [v for v in maxs if v is not None]

        # Häufigster Zustand
        states = [d.get("state") or d.get("title") for d in days]
        states = [s for s in states if s and isinstance(s, str)]
        dominant_state = max(set(states), key=states.count).lower() if states else None

        # Gesamtniederschlag
        total_rain = sum(
            v for v in (_as_float(d.get("precipitationRain")) for d in days)
            if v is not None
        )

        # Max-Wind über alle Tage
        max_wind = max(
            (v for v in (_as_float(d.get("windSpeed")) for d in days) if v is not None),
            default=None,
        )

        parts: list[str] = []

        if mins and maxs:
            parts.append(
                f"In den nächsten {len(days)} Tagen "
                f"{round(min(mins))} bis {round(max(maxs))} Grad"
            )
        if dominant_state:
            parts.append(dominant_state)
        if total_rain > 0:
            parts.append(f"insgesamt {round(total_rain, 1)} mm Regen")
        if max_wind is not None and max_wind >= 7:
            parts.append(f"Wind bis {round(max_wind * 3.6)} km/h")

        return ". ".join(parts) + "."


# ------------------------------------------------------------------
# Hilfsfunktionen

def _day_sentence(label: str, day: dict) -> str:
    """Baut einen Satz für einen einzelnen Prognosetag."""
    parts: list[str] = []

    temp_min = _as_float(day.get("temperatureMin"))
    temp_max = _as_float(day.get("temperatureMax"))
    state = day.get("state") or day.get("title")
    rain = _as_float(day.get("precipitationRain"))
    wind = _as_float(day.get("windSpeed"))

    temp_str = ""
    if temp_min is not None and temp_max is not None:
        temp_str = f"{round(temp_min)} bis {round(temp_max)} Grad"
    elif temp_max is not None:
        temp_str = f"bis zu {round(temp_max)} Grad"

    line1: list[str] = [label]
    if temp_str:
        line1.append(temp_str)
    if state and isinstance(state, str):
        line1.append(state.lower())
    parts.append(", ".join(line1))

    if rain is not None and rain > 0:
        parts.append("Regen erwartet")
    if wind is not None and wind >= 7:
        parts.append(_wind_text(wind, day.get("windDirectionText")))

    return ". ".join(parts) + "."


def _wind_text(speed_ms: float, direction: object) -> str:
    kmh = round(speed_ms * 3.6)
    if direction and isinstance(direction, str):
        return f"Wind {kmh} km/h aus {direction}"
    return f"Wind {kmh} km/h"


def _as_float(v: object) -> Optional[float]:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
