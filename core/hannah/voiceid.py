"""VoiceID-Client für Hannah — Speaker-Identification.

HTTP-Client für den self-hosted Voice-ID-Dienst (siehe voiceid/app.py).
Core ruft Identify() selbst auf empfangenem Satelliten-Audio auf — sowohl
im Proxy-Pfad (SubmitSatelliteAudio) als auch im Direct-UDP-Pfad — statt
sich auf einen extern vorgekauten Sprecher zu verlassen (#210).

Konfiguration (config.yaml):

    voice_id:
      enabled: false
      base_url: "http://localhost:8765"
      timeout_sec: 3.0
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import requests

log = logging.getLogger(__name__)

# Reservierter user_id-Präfix für Hannahs eigene TTS-Stimme(n) (#216). Getrennt pro
# Backend (z.B. "hannah_self_azure", "hannah_self_piper") statt eines gemeinsamen
# Profils, da unterschiedliche TTS-Engines akustisch unterschiedliche "Stimmen" sind.
HANNAH_SELF_PREFIX = "hannah_self"


def is_hannah_self(user_id: str) -> bool:
    """True wenn user_id eines der reservierten Hannah-Eigenstimme-Profile ist."""
    return bool(user_id) and user_id.startswith(HANNAH_SELF_PREFIX)


class VoiceID(ABC):
    """Gemeinsame Schnittstelle für Speaker-Identification-Backends."""

    @abstractmethod
    def identify(self, pcm: bytes, sample_rate: int = 16000) -> str:
        """Erkennt den Sprecher aus rohem PCM. Leerer String = unbekannt/anonym.
        Nie blockierend — Fehler/Timeouts werden abgefangen und wie "unbekannt" behandelt."""

    @abstractmethod
    def enroll(self, user_id: str, pcm: bytes, sample_rate: int = 16000) -> tuple[bool, str]:
        """Registriert eine Stimmprobe für user_id. Gibt (ok, message) zurück."""


class NullVoiceID(VoiceID):
    """Kein VoiceID-Backend konfiguriert — identify() liefert immer anonym."""

    def identify(self, pcm: bytes, sample_rate: int = 16000) -> str:  # pyright: ignore[reportUnusedParameter]
        return ""

    def enroll(self, user_id: str, pcm: bytes, sample_rate: int = 16000) -> tuple[bool, str]:  # pyright: ignore[reportUnusedParameter]
        return False, "Kein Voice-ID-Backend konfiguriert."


class VoiceIDClient(VoiceID):
    """HTTP-Client für den Voice-ID-Dienst.

    POST /identify → {"user_id": ..., "confidence": ...}; "" oder "unknown" = anonym.
    POST /enroll    → {"ok": true, "message": ...}
    """

    def __init__(self, base_url: str, timeout: float = 3.0) -> None:
        self._url = base_url.rstrip("/")
        self._timeout = timeout
        log.info("VoiceID: aktiv → %s", self._url)

    def identify(self, pcm: bytes, sample_rate: int = 16000) -> str:
        try:
            resp = requests.post(
                f"{self._url}/identify",
                data=pcm,
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-Sample-Rate": str(sample_rate),
                },
                timeout=self._timeout,
            )
            resp.raise_for_status()
            user_id = resp.json().get("user_id", "")
        except Exception as exc:
            log.warning("VoiceID identify fehlgeschlagen: %s", exc)
            return ""
        return "" if user_id in ("", "unknown") else user_id

    def enroll(self, user_id: str, pcm: bytes, sample_rate: int = 16000) -> tuple[bool, str]:
        try:
            resp = requests.post(
                f"{self._url}/enroll",
                data=pcm,
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-Sample-Rate": str(sample_rate),
                    "X-User-ID": user_id,
                },
                timeout=self._timeout,
            )
            resp.raise_for_status()
            body = resp.json()
            return bool(body.get("ok", True)), body.get("message", "")
        except Exception as exc:
            log.warning("VoiceID enroll fehlgeschlagen: %s", exc)
            return False, str(exc)


def load(cfg: dict) -> VoiceID:
    """
    Erstellt einen VoiceID-Client aus dem 'voice_id'-Block der config.yaml.
    Gibt NullVoiceID zurück wenn deaktiviert, nicht konfiguriert oder base_url fehlt.
    """
    if not cfg or not cfg.get("enabled", False):
        return NullVoiceID()

    base_url = cfg.get("base_url", "").strip()
    if not base_url:
        log.warning("VoiceID: enabled=true aber base_url fehlt — NullVoiceID als Fallback")
        return NullVoiceID()

    return VoiceIDClient(base_url=base_url, timeout=float(cfg.get("timeout_sec", 3.0)))
