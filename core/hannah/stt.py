"""
STT-Modul für Hannah — unterstützt mehrere Backends mit Fallback.

Backends (Priorität):
  azure  — Azure Cognitive Services STT  (schnell, Cloud, 5h/Monat kostenlos)
  remote — faster-whisper-server         (lokal, OpenAI-kompatibel)
  local  — faster-whisper direkt         (immer verfügbar, langsamer)

config.yaml Beispiel:
  stt:
    language: "de"
    no_speech_threshold: 0.6

    # Azure STT (primär)
    azure_key: "..."
    azure_region: westeurope

    # Remote STT (sekundär)
    remote_url: "http://psrvai01.gessinger.local:8000"
    remote_model: "Systran/faster-whisper-large-v3"
    remote_timeout: 30.0

    # Lokal (Fallback)
    model: "base"
    device: "cpu"
    compute_type: "int8"
"""

import io
import logging
import wave

import numpy as np
import requests

from hannah.log_shipping import TRANSCRIPT

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
        log.debug(f"STT (lokal): '{text}' (no_speech={max_no_speech:.2f})", extra=TRANSCRIPT)
        return text, max_no_speech


class _AzureSTT:
    """Azure Cognitive Services STT (REST-API, kein SDK nötig)."""

    def __init__(self, cfg: dict):
        self._key      = cfg["azure_key"]
        self._region   = cfg["azure_region"]
        self._language = cfg.get("language", "de-DE")
        if "-" not in self._language:
            self._language = self._language + "-" + self._language.upper()
        self._url = (
            f"https://{self._region}.stt.speech.microsoft.com"
            "/speech/recognition/conversation/cognitiveservices/v1"
        )
        log.info(f"Azure STT konfiguriert: {self._region} ({self._language})")

    def transcribe(self, audio: np.ndarray) -> tuple[str, float]:
        wav = _to_wav(audio)
        headers = {
            "Ocp-Apim-Subscription-Key": self._key,
            "Content-Type": "audio/wav; codecs=audio/pcm; samplerate=16000",
        }
        params = {"language": self._language, "format": "simple"}
        resp = requests.post(self._url, headers=headers, params=params,
                             data=wav, timeout=10)
        resp.raise_for_status()
        data   = resp.json()
        status = data.get("RecognitionStatus", "")
        if status != "Success":
            raise ValueError(f"Azure STT: RecognitionStatus={status}")
        text = data.get("DisplayText", "").strip()
        log.debug(f"STT (azure): '{text}'", extra=TRANSCRIPT)
        return text, 0.0


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
        log.debug(f"STT (remote): '{text}'", extra=TRANSCRIPT)
        return text, 0.0


class STT:
    """
    STT mit konfigurierbarer Fallback-Kette: Azure → Remote → Lokal.
    Jede Stufe wird nur versucht wenn sie konfiguriert ist.
    """

    def __init__(self, cfg: dict):
        self._azure:  _AzureSTT  | None = None
        self._remote: _RemoteSTT | None = None

        if cfg.get("azure_key") and cfg.get("azure_region"):
            self._azure = _AzureSTT(cfg)
        if cfg.get("remote_url"):
            self._remote = _RemoteSTT(cfg)

        self._local = _LocalSTT(cfg)

        chain = []
        if self._azure:  chain.append("azure")
        if self._remote: chain.append("remote")
        chain.append("local")
        log.info(f"STT-Kette: {' → '.join(chain)}")

    def transcribe(self, audio: np.ndarray) -> tuple[str, float]:
        if self._azure:
            try:
                return self._azure.transcribe(audio)
            except Exception as e:
                log.warning(f"Azure-STT fehlgeschlagen, Fallback auf Remote/Lokal: {e}")
        if self._remote:
            try:
                return self._remote.transcribe(audio)
            except Exception as e:
                log.warning(f"Remote-STT fehlgeschlagen, Fallback auf lokal: {e}")
        return self._local.transcribe(audio)
