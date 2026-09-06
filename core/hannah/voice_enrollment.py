"""Geführter Voice-Enrollment-Dialog (hannah#8).

Wird von einem bereits authentifizierten externen Client (WebUI/Telegram) über die
gRPC-RPC StartVoiceEnrollment ausgelöst (siehe grpc_server.py). Core übernimmt danach
den kompletten Dialog selbst: ein paar offene Fragen stellen, die Antworten als rohes
PCM einsammeln (bewusst OHNE STT — der Text wird nie gebraucht, nur das Audio, STT
würde das System unnötig belasten) und am Ende das VoiceID-Backend füttern.

Der Turn-Taking-Mechanismus ist bewusst NICHT die Capture-Infrastruktur aus dem
Wakeword-Training (RequestSatelliteCapture/StreamSatelliteAudio) — die ist für
kontinuierliches Sampling gedacht (Rohaudio-Dauerstrom ohne Utterance-Grenzen).
Stattdessen wird main.py's bestehender publish_listen-Mechanismus wiederverwendet
(dieselbe Idee wie die Trigger-Engine's "ask": Frage stellen, Mic öffnen, nächste
Äußerung des Geräts als Antwort werten) — nur dass main.py hier die Rohantwort VOR
STT an try_consume() reicht statt sie zu transkribieren.
"""
from __future__ import annotations

import logging
import random
import threading
from typing import Callable, Optional

import numpy as np

log = logging.getLogger(__name__)

# Sicherheitsgrenze aus dem Proto-Design (speaker_enrollment.proto:
# StartVoiceEnrollmentRequest), kein Tuning-Parameter — daher Code-Konstante statt Setting.
_OTHER_ENROLL_MIN_TRUST = 10

# Wie main.py's _ask_fn-Pending-Frage-Timeout — ebenfalls Code-Konstante, kein Setting.
_ANSWER_TIMEOUT_S = 60.0


class VoiceEnrollmentManager:
    """Trust-Gate + Dialog-Orchestrierung für hannah#8. Nimmt alle IO (TTS/MQTT/VoiceID)
    als injizierte Callbacks/Objekte entgegen — main.py verdrahtet die echten,
    Tests können mit Fakes arbeiten (Muster: ActivityLogManager in activity_log.py)."""

    def __init__(
        self,
        user_manager,
        voice_id,
        questions: list[str],
        target_speech_s: float,
        max_questions: int,
        ask_fn: Callable[[str, str], None],       # (device_id, question) -> TTS-Frage + Mic öffnen
        confirm_fn: Callable[[str, str], None],   # (device_id, text) -> TTS-Bestätigung/Fehlermeldung
    ) -> None:
        self._user_manager = user_manager
        self._voice_id = voice_id
        self._questions = list(questions)
        self._target_speech_s = target_speech_s
        self._max_questions = max_questions
        self._ask_fn = ask_fn
        self._confirm_fn = confirm_fn

        self._lock = threading.Lock()
        self._active_devices: set[str] = set()
        # device_id -> (event, holder, timer); holder bleibt leer bei Timeout,
        # sonst genau ein (audio_array, sample_rate)-Tupel.
        self._waiters: dict[str, tuple[threading.Event, list, threading.Timer]] = {}

    def _trust_level(self, user_id: int) -> int:
        user = self._user_manager.get_user_by_id(user_id) if user_id else None
        return user.trust_level if user else 0

    def _display_name(self, user_id: int) -> str:
        user = self._user_manager.get_user_by_id(user_id)
        return user.display_name if user else "dich"

    def start(self, requestor_id: int, user_id: int, device_id: str) -> tuple[bool, str]:
        """Trust-Check + Start des Dialogs in einem Hintergrund-Thread. Gibt sofort
        zurück (True, ...) — der eigentliche Dialog dauert ~1-2 Minuten, dafür soll
        der Unary-RPC nicht offen bleiben."""
        if user_id != requestor_id and self._trust_level(requestor_id) < _OTHER_ENROLL_MIN_TRUST:
            return False, "Trust-Level zu niedrig, um die Stimme einer anderen Person zu lernen."

        with self._lock:
            if device_id in self._active_devices:
                return False, f"Für '{device_id}' läuft bereits ein Enrollment."
            self._active_devices.add(device_id)

        threading.Thread(
            target=self._run, args=(user_id, device_id), daemon=True,
            name=f"voice-enroll-{device_id}",
        ).start()
        return True, "Enrollment gestartet."

    def _run(self, user_id: int, device_id: str) -> None:
        try:
            self._ask_fn(
                device_id,
                "Ich lerne jetzt deine Stimme. Ich stelle dir dafür ein paar Fragen — "
                "antworte einfach ganz normal.",
            )
            pool = self._questions.copy()
            random.shuffle(pool)

            chunks: list[bytes] = []
            cumulative_s = 0.0
            asked = 0
            while cumulative_s < self._target_speech_s and asked < self._max_questions and pool:
                question = pool.pop()
                asked += 1
                audio_array, sample_rate = self._ask_and_wait(device_id, question)
                if audio_array is None:
                    continue
                cumulative_s += len(audio_array) / sample_rate
                chunks.append((audio_array * 32767).astype(np.int16).tobytes())

            name = self._display_name(user_id)
            if not chunks:
                self._confirm_fn(device_id, "Ich habe leider keine Antworten bekommen — Enrollment abgebrochen.")
                return

            pcm = b"".join(chunks)
            ok, msg = self._voice_id.enroll(str(user_id), pcm, 16000)
            if ok:
                self._confirm_fn(device_id, f"Stimmabdruck für {name} gespeichert.")
            else:
                self._confirm_fn(device_id, f"Enrollment für {name} fehlgeschlagen: {msg}")
        finally:
            with self._lock:
                self._active_devices.discard(device_id)

    def _ask_and_wait(self, device_id: str, question: str) -> tuple[Optional[np.ndarray], int]:
        """Registriert die Wartestelle VOR dem Fragen (nicht danach) — sonst gäbe es ein
        (winziges, aber unnötiges) Zeitfenster, in dem eine bereits eintreffende Antwort
        nicht als Enrollment-Antwort erkannt würde."""
        event = threading.Event()
        holder: list = []
        timer = threading.Timer(_ANSWER_TIMEOUT_S, event.set)
        with self._lock:
            self._waiters[device_id] = (event, holder, timer)
        timer.start()
        self._ask_fn(device_id, question)
        event.wait()
        with self._lock:
            self._waiters.pop(device_id, None)
        timer.cancel()
        if not holder:
            return None, 16000
        return holder[0]

    def try_consume(self, device_id: str, audio_array: np.ndarray, sample_rate: int) -> bool:
        """Von main.py aus dem Satelliten-Audio-Handler gerufen, BEVOR STT läuft.
        True = Audio wurde als Enrollment-Antwort konsumiert (STT/NLU überspringen)."""
        with self._lock:
            entry = self._waiters.get(device_id)
        if not entry:
            return False
        event, holder, timer = entry
        timer.cancel()
        holder.append((audio_array, sample_rate))
        event.set()
        return True
