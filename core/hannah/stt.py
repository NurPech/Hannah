import io
import logging
import wave

import numpy as np
import requests

log = logging.getLogger(__name__)


def _to_wav(audio: np.ndarray, sample_rate: int = 16000) -> bytes:
    pcm = (audio * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


class _LocalSTT:
    def __init__(self, cfg: dict):
        from faster_whisper import WhisperModel

        model_size = cfg.get("model", "base")
        device = cfg.get("device", "cpu")
        compute_type = cfg.get("compute_type", "int8")
        log.info(f"Lade Whisper-Modell '{model_size}' ({device}, {compute_type}) ...")
        self._model = WhisperModel(model_size, device=device, compute_type=compute_type)
        self._language = cfg.get("language", "de")
        self._no_speech_threshold = cfg.get("no_speech_threshold", 0.6)
        log.info("Whisper bereit.")

    def transcribe(self, audio: np.ndarray) -> tuple[str, float]:
        segments, _ = self._model.transcribe(
            audio,
            language=self._language,
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 300},
        )
        parts = []
        max_no_speech = 0.0
        for seg in segments:
            max_no_speech = max(max_no_speech, seg.no_speech_prob)
            if seg.no_speech_prob < self._no_speech_threshold:
                parts.append(seg.text.strip())
        text = " ".join(parts).strip()
        log.debug(f"STT (lokal): '{text}' (no_speech={max_no_speech:.2f})")
        return text, max_no_speech


class _RemoteSTT:
    def __init__(self, cfg: dict):
        self._url      = cfg["remote_url"].rstrip("/")
        self._model    = cfg.get("remote_model", "Systran/faster-whisper-large-v3")
        self._language = cfg.get("language", "de")
        self._timeout  = float(cfg.get("remote_timeout", 15.0))
        log.info(f"Remote-STT: {self._url} (Modell: {self._model})")

    def transcribe(self, audio: np.ndarray) -> tuple[str, float]:
        wav = _to_wav(audio)
        resp = requests.post(
            f"{self._url}/v1/audio/transcriptions",
            files={"file": ("audio.wav", wav, "audio/wav")},
            data={"model": self._model, "language": self._language},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        text = resp.json().get("text", "").strip()
        log.debug(f"STT (remote): '{text}'")
        return text, 0.0


class STT:
    """
    Wählt automatisch zwischen lokalem Whisper und Remote-STT-Server.
    Wenn remote_url gesetzt ist, wird Remote bevorzugt — bei Fehler
    fällt STT automatisch auf das lokale Modell zurück.
    """

    def __init__(self, cfg: dict):
        self._remote: _RemoteSTT | None = None
        if cfg.get("remote_url"):
            self._remote = _RemoteSTT(cfg)
        self._local = _LocalSTT(cfg)

    def transcribe(self, audio: np.ndarray) -> tuple[str, float]:
        if self._remote:
            try:
                return self._remote.transcribe(audio)
            except Exception as e:
                log.warning(f"Remote-STT fehlgeschlagen, Fallback auf lokal: {e}")
        return self._local.transcribe(audio)
