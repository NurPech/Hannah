"""
Stufen-Timing pro Sprachäußerung (#402): eine strukturierte Logzeile je Äußerung mit den
Zeiten von VoiceID, STT, NLU, Geräteausführung, LLM und TTS, damit Latenzfragen aus Loki
beantwortet werden statt aus dem Bauchgefühl.

    latency device=9070690de424 intent=TurnOn audio_ms=3490 total_ms=2870 voiceid_ms=310 ...

Der Timer hängt als ContextVar am verarbeitenden Thread. Aufrufstellen in geteiltem Code
(z.B. _handle_text, das auch Telegram/gRPC-Text nutzt) rufen einfach stage() — ohne laufenden
Timer ist das ein No-Op. Gemessen wird mit time.monotonic(), Core und Proxy laufen auf
demselben Pi und teilen sich damit eine Zeitbasis fürs Auswerten in Loki.
"""
import contextvars
import logging
import time
from contextlib import contextmanager
from typing import Iterator, Optional

log = logging.getLogger(__name__)

# Feste Reihenfolge der Felder in der Logzeile. Eine Stufe, die nicht lief, fehlt in der Zeile.
STAGES = ("voiceid", "stt", "nlu", "exec", "llm", "tts")

# 16 kHz, 16 Bit, mono
_BYTES_PER_SECOND = 32000

_current: contextvars.ContextVar[Optional["UtteranceTimer"]] = contextvars.ContextVar(
    "utterance_timer", default=None,
)


class UtteranceTimer:
    def __init__(self, device: str, audio_bytes: int):
        self.device = device
        self.audio_ms = round(audio_bytes / _BYTES_PER_SECOND * 1000)
        self._t0 = time.monotonic()
        self._stages: dict[str, float] = {}
        self._token: Optional[contextvars.Token] = None

    def add(self, name: str, seconds: float) -> None:
        self._stages[name] = self._stages.get(name, 0.0) + seconds

    def finish(self, intent: str) -> None:
        """Beendet den Timer und schreibt die Logzeile. other_ms ist der Rest, den keine
        Stufe abdeckt (Trigger-Phrasen, Gesprächskontext, Activity-Log, ...)."""
        total = time.monotonic() - self._t0
        if self._token is not None:
            _current.reset(self._token)
            self._token = None
        parts = [
            f"device={self.device}", f"intent={intent}",
            f"audio_ms={self.audio_ms}", f"total_ms={round(total * 1000)}",
        ]
        parts += [f"{name}_ms={round(self._stages[name] * 1000)}" for name in STAGES if name in self._stages]
        other = total - sum(self._stages.values())
        parts.append(f"other_ms={max(0, round(other * 1000))}")
        log.info("latency " + " ".join(parts))


def start(device: str, audio_bytes: int) -> UtteranceTimer:
    timer = UtteranceTimer(device, audio_bytes)
    timer._token = _current.set(timer)
    return timer


@contextmanager
def stage(name: str) -> Iterator[None]:
    timer = _current.get()
    if timer is None:
        yield
        return
    t0 = time.monotonic()
    try:
        yield
    finally:
        timer.add(name, time.monotonic() - t0)
