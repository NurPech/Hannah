"""
TTS-Modul für Hannah — unterstützt mehrere Backends mit Fallback auf Piper.

Backends:
  piper  — lokal, offline, kein API-Key  (pip install piper-tts)
  azure  — Azure Cognitive Services TTS  (pip install requests)
  polly  — Amazon Polly                  (pip install boto3)

Kein Vendor-Lock: Backend per config.yaml wählbar, Piper ist immer der Fallback
wenn der Cloud-Dienst nicht erreichbar ist.

Disk-Cache: Cloud-Synthesen werden als .pcm-Dateien zwischengespeichert.
Warm-Phrases: Standard-Antworten werden beim Start einmalig synthetisiert
und stehen danach ohne Cloud-Aufruf zur Verfügung.

config.yaml Beispiel:
  tts:
    backend: azure          # piper | azure | polly
    cache_dir: .tts_cache   # Verzeichnis für gecachte Cloud-Synthesen

    # Piper (Fallback — immer konfigurieren wenn Cloud-Backend aktiv)
    model: /pfad/de_DE-kerstin-low.onnx
    length_scale: 1.0
    noise_scale: 0.667
    noise_w: 0.8

    # Azure Cognitive Services
    azure_key: "..."
    azure_region: westeurope
    azure_voice: de-DE-KatjaNeural   # oder: de-DE-ConradNeural

    # Amazon Polly
    polly_key_id: "..."
    polly_secret_key: "..."
    polly_region: eu-central-1
    polly_voice: Vicki              # oder: Daniel
    polly_engine: neural

    # Bestätigungston
    confirmation_sound: /pfad/pling.mp3

    # Standard-Antworten beim Start vorsynthetiisieren (via primärem Backend)
    warm_phrases:
      - "Ich habe dich nicht verstanden."
      - "Tut mir leid, ich weiß nicht was du meinst."
      - "Es wurden keine Geräte gefunden."
"""

import abc
import hashlib
import io
import logging
import math
import os
import struct
import wave
from typing import Callable, Optional

log = logging.getLogger(__name__)

_SAMPLE_RATE_CLOUD = 16000  # Polly-PCM-Output (max 16kHz)
_SAMPLE_RATE_AZURE = 24000  # Azure: raw-24khz-16bit-mono-pcm
_MAX_TTS_CHARS     = 400    # Harte Obergrenze — LLM-Antworten können sehr lang werden


# ── Hilfsfunktion ─────────────────────────────────────────────────────────────

def _strip_ssml_tags(ssml: str) -> str:
    """Entfernt XML-Tags — Fallback wenn Backend kein SSML versteht."""
    import re
    return re.sub(r"<[^>]+>", "", ssml).strip()


def _truncate_for_tts(text: str, max_chars: int = _MAX_TTS_CHARS) -> str:
    """Kürzt Text auf max_chars — schneidet am letzten Satzende ab."""
    if len(text) <= max_chars:
        return text
    chunk = text[:max_chars]
    # Letztes Satzende suchen
    for sep in (".", "!", "?"):
        idx = chunk.rfind(sep)
        if idx > max_chars // 2:
            truncated = chunk[:idx + 1]
            log.warning(f"TTS-Text von {len(text)} auf {len(truncated)} Zeichen gekürzt.")
            return truncated
    # Kein Satzende gefunden — am letzten Wort kürzen
    truncated = chunk.rsplit(" ", 1)[0] + " …"
    log.warning(f"TTS-Text von {len(text)} auf {len(truncated)} Zeichen gekürzt.")
    return truncated


# ── Backend-Interface ──────────────────────────────────────────────────────────

class _TTSBackend(abc.ABC):
    @abc.abstractmethod
    def synthesize(self, text: str) -> Optional[bytes]:
        """Gibt raw PCM (16-bit signed mono) zurück oder None bei Fehler."""
        ...

    def synthesize_ssml(self, ssml: str) -> Optional[bytes]:
        """Synthetisiert SSML. Standard-Implementierung: Tags strippen → plain text."""
        return self.synthesize(_strip_ssml_tags(ssml))

    @property
    @abc.abstractmethod
    def sample_rate(self) -> int: ...

    @property
    @abc.abstractmethod
    def name(self) -> str: ...


# ── Piper-Backend ──────────────────────────────────────────────────────────────

class _PiperBackend(_TTSBackend):
    def __init__(self, cfg: dict):
        from piper import PiperVoice
        model_path = cfg["model"]
        log.info(f"Lade Piper-Stimme: {model_path} …")
        self._voice = PiperVoice.load(model_path)
        self._cfg   = cfg
        log.info("Piper bereit.")

    def synthesize(self, text: str) -> Optional[bytes]:
        try:
            from piper.config import SynthesisConfig
            syn_cfg = SynthesisConfig(
                speaker_id  = self._cfg.get("speaker_id",   None),
                length_scale= self._cfg.get("length_scale",  1.0),
                noise_scale = self._cfg.get("noise_scale",  0.667),
                noise_w_scale=self._cfg.get("noise_w",       0.8),
            )
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(self._voice.config.sample_rate)
                self._voice.synthesize_wav(text, wf, syn_config=syn_cfg)
            buf.seek(0)
            with wave.open(buf, "rb") as wf:
                pcm = wf.readframes(wf.getnframes())
            padding = bytes(int(self._voice.config.sample_rate * 2 * self._cfg.get("padding_secs", 0.4)))
            return pcm + padding
        except Exception as e:
            log.error(f"Piper-Synthese fehlgeschlagen: {e}")
            return None

    @property
    def sample_rate(self) -> int:
        return self._voice.config.sample_rate

    @property
    def name(self) -> str:
        return "piper"


# ── Azure-Backend ──────────────────────────────────────────────────────────────

class _AzureBackend(_TTSBackend):
    """Azure Cognitive Services TTS (REST-API, kein SDK nötig)."""

    def __init__(self, cfg: dict):
        self._key    = cfg["azure_key"]
        self._region = cfg["azure_region"]
        self._voice  = cfg.get("azure_voice", "de-DE-KatjaNeural")
        self._url    = (
            f"https://{self._region}.tts.speech.microsoft.com"
            "/cognitiveservices/v1"
        )
        log.info(f"Azure TTS konfiguriert: {self._voice} ({self._region})")

    def _call(self, ssml: str) -> Optional[bytes]:
        import requests
        headers = {
            "Ocp-Apim-Subscription-Key": self._key,
            "Content-Type":              "application/ssml+xml",
            "X-Microsoft-OutputFormat":  "raw-24khz-16bit-mono-pcm",
        }
        try:
            resp = requests.post(self._url, headers=headers,
                                 data=ssml.encode("utf-8"), timeout=10)
            resp.raise_for_status()
            return resp.content
        except Exception as e:
            log.warning(f"Azure TTS fehlgeschlagen: {e}")
            return None

    def synthesize(self, text: str) -> Optional[bytes]:
        ssml = (
            f"<speak version='1.0' xml:lang='de-DE'>"
            f"<voice name='{self._voice}'>{text}</voice>"
            f"</speak>"
        )
        return self._call(ssml)

    def synthesize_ssml(self, ssml: str) -> Optional[bytes]:
        if not ssml.lstrip().startswith("<speak"):
            ssml = (
                f"<speak version='1.0' xml:lang='de-DE'>"
                f"<voice name='{self._voice}'>{ssml}</voice>"
                f"</speak>"
            )
        return self._call(ssml)

    @property
    def sample_rate(self) -> int:
        return _SAMPLE_RATE_AZURE

    @property
    def name(self) -> str:
        return f"azure_{self._voice}"


# ── Polly-Backend ──────────────────────────────────────────────────────────────

class _PollyBackend(_TTSBackend):
    """Amazon Polly TTS (boto3)."""

    def __init__(self, cfg: dict):
        import boto3
        self._voice  = cfg.get("polly_voice",  "Vicki")
        self._engine = cfg.get("polly_engine", "neural")
        self._client = boto3.client(
            "polly",
            region_name          = cfg.get("polly_region",     "eu-central-1"),
            aws_access_key_id    = cfg.get("polly_key_id"),
            aws_secret_access_key= cfg.get("polly_secret_key"),
        )
        log.info(f"Polly TTS konfiguriert: {self._voice} ({self._engine})")

    def _call(self, text: str, text_type: str = "text") -> Optional[bytes]:
        try:
            resp = self._client.synthesize_speech(
                Text        = text,
                TextType    = text_type,
                OutputFormat= "pcm",
                VoiceId     = self._voice,
                Engine      = self._engine,
                SampleRate  = str(_SAMPLE_RATE_CLOUD),
            )
            return resp["AudioStream"].read()
        except Exception as e:
            log.warning(f"Polly TTS fehlgeschlagen: {e}")
            return None

    def synthesize(self, text: str) -> Optional[bytes]:
        return self._call(text, "text")

    def synthesize_ssml(self, ssml: str) -> Optional[bytes]:
        if not ssml.lstrip().startswith("<speak"):
            ssml = f"<speak>{ssml}</speak>"
        return self._call(ssml, "ssml")

    @property
    def sample_rate(self) -> int:
        return _SAMPLE_RATE_CLOUD

    @property
    def name(self) -> str:
        return f"polly_{self._voice}"


# ── Disk-Cache ─────────────────────────────────────────────────────────────────

class _TTSCache:
    """Speichert synthetisierte PCM-Dateien auf der Festplatte."""

    def __init__(self, cache_dir: str, backend_name: str, sample_rate: int):
        # Sample-Rate im Verzeichnisnamen — verhindert Pitch-Fehler bei Format-Änderungen
        self._dir = os.path.join(cache_dir, f"{backend_name}_{sample_rate}hz")
        os.makedirs(self._dir, exist_ok=True)

    def _path(self, text: str) -> str:
        key = hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]
        return os.path.join(self._dir, key + ".pcm")

    def get(self, text: str) -> Optional[bytes]:
        p = self._path(text)
        if os.path.isfile(p):
            with open(p, "rb") as f:
                return f.read()
        return None

    def put(self, text: str, pcm: bytes):
        with open(self._path(text), "wb") as f:
            f.write(pcm)


# ── Hilfsfunktionen ────────────────────────────────────────────────────────────

def _build_backend(cfg: dict, name: str) -> Optional[_TTSBackend]:
    try:
        if name == "piper":
            if not cfg.get("model"):
                return None
            return _PiperBackend(cfg)
        if name == "azure":
            return _AzureBackend(cfg)
        if name == "polly":
            return _PollyBackend(cfg)
        log.warning(f"Unbekanntes TTS-Backend: '{name}'")
    except ImportError as e:
        log.warning(f"TTS-Backend '{name}' nicht verfügbar: {e}")
    except Exception as e:
        log.error(f"TTS-Backend '{name}' Initialisierung fehlgeschlagen: {e}")
    return None


# ── Haupt-TTS-Klasse ───────────────────────────────────────────────────────────

class TTS:
    """
    TTS mit konfigurierbarem primärem Backend und automatischem Piper-Fallback.

    synthesize() gibt (pcm_bytes, sample_rate) zurück oder None.
    """

    def __init__(self, cfg: dict):
        self._cfg = cfg
        backend_name = cfg.get("backend", "piper")

        # Primäres Backend
        self._primary: Optional[_TTSBackend] = _build_backend(cfg, backend_name)

        # Piper-Fallback (nur wenn primäres Backend kein Piper ist)
        self._fallback: Optional[_TTSBackend] = None
        if backend_name != "piper" and cfg.get("model"):
            self._fallback = _build_backend(cfg, "piper")
            if self._fallback:
                log.info("Piper als Fallback konfiguriert.")

        # Disk-Cache (nur für Cloud-Backends sinnvoll)
        self._cache: Optional[_TTSCache] = None
        if self._primary and backend_name != "piper":
            cache_dir = cfg.get("cache_dir", ".tts_cache")
            self._cache = _TTSCache(cache_dir, self._primary.name, self._primary.sample_rate)
            log.info(f"TTS-Cache: {cache_dir}/{self._primary.name}_{self._primary.sample_rate}hz/")

        # Bestätigungston laden
        self._confirmation_pcm:  Optional[bytes] = None
        self._confirmation_rate: int = 16000
        sound_path = cfg.get("confirmation_sound", "")
        if sound_path:
            self._confirmation_pcm, self._confirmation_rate = self._load_audio(sound_path)

        self._last_active_backend: str = self._primary.name if self._primary else "none"
        self._on_backend_change: Optional[Callable[[str], None]] = None

        if not self._primary:
            log.info("TTS deaktiviert (kein Backend konfiguriert).")
        else:
            log.info(f"TTS bereit: primär={self._primary.name}"
                     + (f", fallback=piper" if self._fallback else ""))

    # ------------------------------------------------------------------

    def set_backend_change_handler(self, callback: Callable[[str], None]):
        """Callback(backend_name) wird aufgerufen wenn sich das aktive Backend ändert."""
        self._on_backend_change = callback
        if self._primary:
            callback(self._primary.name)

    def _notify_backend(self, name: str):
        if name != self._last_active_backend:
            self._last_active_backend = name
            if self._on_backend_change:
                self._on_backend_change(name)

    @property
    def enabled(self) -> bool:
        return self._primary is not None

    @property
    def sample_rate(self) -> int:
        return self._primary.sample_rate if self._primary else 16000

    # ------------------------------------------------------------------

    def synthesize(self, text: str) -> Optional[tuple[bytes, int]]:
        """
        Synthetisiert Text zu (raw_pcm, sample_rate).
        Reihenfolge: Cache → primäres Backend → Piper-Fallback.
        Gibt None zurück wenn TTS deaktiviert oder alle Backends fehlgeschlagen.
        """
        if not self._primary:
            return None

        text = _truncate_for_tts(text)

        # 1. Cache prüfen
        if self._cache:
            cached = self._cache.get(text)
            if cached:
                log.debug(f"TTS aus Cache: '{text[:50]}'")
                return cached, self._primary.sample_rate

        # 2. Primäres Backend
        pcm = self._primary.synthesize(text)
        if pcm:
            self._notify_backend(self._primary.name)
            if self._cache:
                self._cache.put(text, pcm)
            return pcm, self._primary.sample_rate

        # 3. Piper-Fallback
        if self._fallback:
            log.warning(f"Primäres TTS-Backend fehlgeschlagen — Fallback auf Piper.")
            self._notify_backend("piper")
            pcm = self._fallback.synthesize(text)
            if pcm:
                return pcm, self._fallback.sample_rate

        log.error("Alle TTS-Backends fehlgeschlagen.")
        return None

    def synthesize_ssml(self, ssml: str) -> Optional[tuple[bytes, int]]:
        """
        Synthetisiert SSML zu (raw_pcm, sample_rate).
        Azure/Polly: natives SSML. Piper: Tags werden gestripped.
        Kein Disk-Cache (SSML zu variabel für sinnvolles Caching).
        """
        if not self._primary:
            return None
        pcm = self._primary.synthesize_ssml(ssml)
        if pcm:
            self._notify_backend(self._primary.name)
            return pcm, self._primary.sample_rate
        if self._fallback:
            log.warning("Primäres TTS-Backend fehlgeschlagen — Fallback auf Piper (kein SSML).")
            self._notify_backend("piper")
            pcm = self._fallback.synthesize(_strip_ssml_tags(ssml))
            if pcm:
                return pcm, self._fallback.sample_rate
        log.error("Alle TTS-Backends fehlgeschlagen (SSML).")
        return None

    def warm_cache(self, phrases: list[str]):
        """
        Synthetisiert Standard-Phrasen via primärem Backend und speichert sie im Cache.
        Wird beim Start aufgerufen — danach sind diese Phrasen ohne Cloud-Aufruf verfügbar.
        """
        if not self._primary or not self._cache:
            return
        log.info(f"TTS-Cache: {len(phrases)} Standard-Phrasen vorsynthetiisieren …")
        for phrase in phrases:
            if self._cache.get(phrase):
                continue
            result = self.synthesize(phrase)
            if result:
                log.debug(f"Gecacht: '{phrase[:60]}'")
        log.info("TTS-Cache warm.")

    def confirmation_tone(self) -> tuple[bytes, int]:
        """Gibt den Bestätigungston zurück (Datei wenn konfiguriert, sonst synthetisiert)."""
        if self._confirmation_pcm:
            return self._confirmation_pcm, self._confirmation_rate
        return self._synthesize_confirmation_tone()

    # ------------------------------------------------------------------

    @staticmethod
    def _synthesize_confirmation_tone(volume: float = 0.4) -> tuple[bytes, int]:
        rate     = 44100
        duration = 0.4
        n        = int(rate * duration)
        freq     = 1318.51
        samples  = []
        for i in range(n):
            t        = i / rate
            envelope = t / 0.01 if t < 0.01 else math.exp(-12.0 * (t - 0.01) / duration)
            val      = int(32767 * volume * envelope * math.sin(2 * math.pi * freq * t))
            samples.append(max(-32768, min(32767, val)))
        return struct.pack(f"{len(samples)}h", *samples), rate

    @staticmethod
    def _load_audio(path: str) -> tuple[bytes, int]:
        try:
            import miniaudio
            decoded = miniaudio.decode_file(
                path,
                output_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=1,
            )
            log.info(f"Bestätigungston geladen: {path} ({decoded.sample_rate}Hz)")
            return bytes(decoded.samples), decoded.sample_rate
        except Exception as e:
            log.warning(f"Bestätigungston '{path}' nicht ladbar: {e} — nutze synthetisierten Ton.")
            return b"", 16000
