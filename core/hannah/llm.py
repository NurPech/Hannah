"""LLM-Client für Hannah — Smalltalk-Backend.

Abstraktion über verschiedene Anbieter. Aktuell implementiert:
  - OpenAICompatibleLLM : OpenAI-API-Format — deckt Ollama (self-hosted),
                          OpenAI, Mistral, Groq, Together AI, u.v.m.
  - DummyLLM            : Feste Fallback-Antwort ohne API-Aufruf.
                          Aktiv wenn LLM nicht konfiguriert oder deaktiviert.

Konfiguration (config.yaml):

    llm:
      enabled: true
      base_url: "http://localhost:11434/v1"   # Ollama lokal
      model: "llama3.2"
      api_key: ""                              # leer bei Ollama
      timeout: 10.0
      system_prompt: "Du bist Hannah ..."
      fallback_response: "Das kann ich leider nicht beantworten."

Für OpenAI: base_url: "https://api.openai.com/v1", api_key: "sk-..."
Für Groq:   base_url: "https://api.groq.com/openai/v1", api_key: "gsk_..."
"""
from __future__ import annotations

import datetime
import logging
import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from .iobroker import IoBrokerClient

_WEEKDAYS_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def prepare_prompt(raw: str, iobroker: "IoBrokerClient | None" = None) -> str:
    """
    Ersetzt Variablen im System-Prompt:
      {{TIME}}    → aktuelle Uhrzeit ("14:32 Uhr")
      {{DATE}}    → aktuelles Datum ("21.04.2026")
      {{WEEKDAY}} → Wochentag auf Deutsch ("Montag")
      {{KW}}      → ISO-Kalenderwoche ("17")
      {{iob.STATE_ID}} → ioBroker-State per REST (z.B. {{iob.javascript.0.foo.bar}})

    Unbekannte oder nicht auflösbare Variablen werden unverändert gelassen.
    """
    now = datetime.datetime.now()
    static_vars = {
        "{{TIME}}":    now.strftime("%H:%M Uhr"),
        "{{DATE}}":    now.strftime("%d.%m.%Y"),
        "{{WEEKDAY}}": _WEEKDAYS_DE[now.weekday()],
        "{{KW}}":      str(now.isocalendar()[1]),
    }
    for placeholder, value in static_vars.items():
        raw = raw.replace(placeholder, value)

    if iobroker is not None:
        for m in re.finditer(r"\{\{iob\.([^}]+)\}\}", raw):
            value = iobroker.get_state_raw(m.group(1))
            if value is not None:
                raw = raw.replace(m.group(0), value)

    return raw

log = logging.getLogger(__name__)

_DEFAULT_FALLBACK = "Das kann ich leider nicht beantworten."
_CLASSIFY_PROMPT = (
    "Antworte ausschließlich mit COMMAND oder SMALLTALK — kein anderes Wort.\n"
    "COMMAND: der Nutzer will ein Gerät steuern (Licht, Heizung, Steckdose, Musik etc.).\n"
    "SMALLTALK: alles andere (Konversation, Fragen, Witze, persönliche Themen, ...)."
)


class LLMClient(ABC):
    """Gemeinsame Schnittstelle für alle LLM-Backends."""

    @abstractmethod
    def chat(
        self,
        user_message: str,
        system_prompt: str = "",
        history: list[dict] | None = None,
    ) -> str:
        """
        Schickt eine Nachricht und gibt die Antwort als String zurück.
        history: optionale Nachrichtenhistorie [{role, content}, ...] vor user_message.
        """

    def classify(self, text: str) -> bool:
        """True = COMMAND (→ NLU), False = SMALLTALK (→ LLM-Chat)."""
        result = self.chat(text, system_prompt=_CLASSIFY_PROMPT)
        return "COMMAND" in result.upper()


class DummyLLM(LLMClient):
    """Gibt eine feste Antwort zurück — kein API-Aufruf."""

    def __init__(self, response: str = _DEFAULT_FALLBACK) -> None:
        self._response = response
        log.info("LLM: DummyLLM aktiv (kein LLM konfiguriert)")

    def chat(
        self,
        user_message: str,       # pyright: ignore[reportUnusedParameter]
        system_prompt: str = "", # pyright: ignore[reportUnusedParameter]
        history: list[dict] | None = None,  # pyright: ignore[reportUnusedParameter]
    ) -> str:
        return self._response

    def classify(self, text: str) -> bool:  # pyright: ignore[reportUnusedParameter]
        return True  # Kein LLM → immer als Command routen


class OpenAICompatibleLLM(LLMClient):
    """
    HTTP-Client für alle OpenAI-kompatiblen APIs.

    Kompatibel mit: Ollama, OpenAI, Mistral, Groq, Together AI, vLLM,
    LM Studio, llama.cpp Server, LocalAI, ...

    Alle Antworten sind blocking (requests) — passt zur synchronen Hannah-Pipeline.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout: float = 10.0,
        max_tokens: int = 300,
    ) -> None:
        self._url       = base_url.rstrip("/") + "/chat/completions"
        self._model     = model
        self._api_key   = api_key
        self._timeout   = timeout
        self._max_tokens = max_tokens
        log.info("LLM: OpenAICompatibleLLM → %s (model=%s)", base_url, model)

    def chat(
        self,
        user_message: str,
        system_prompt: str = "",
        history: list[dict] | None = None,
    ) -> str:
        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        try:
            resp = requests.post(
                self._url,
                json={
                    "model":      self._model,
                    "messages":   messages,
                    "max_tokens": self._max_tokens,
                },
                headers=headers,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
        except requests.exceptions.Timeout:
            log.warning("LLM-Anfrage: Timeout nach %.1fs", self._timeout)
            return _DEFAULT_FALLBACK
        except Exception as exc:
            log.error("LLM-Anfrage fehlgeschlagen: %s", exc)
            return _DEFAULT_FALLBACK
        
class OllamaLLM(LLMClient):
    """
    Client für die native Ollama API (/api/chat).
    Verwenden wenn Ollama < 0.1.24 oder die OpenAI-kompatible API nicht
    funktioniert. Für neuere Ollama-Versionen reicht OpenAICompatibleLLM.

    provider: ollama
    base_url: "http://localhost:11434"
    """

    def __init__(self, base_url: str, model: str, timeout: float = 10.0, max_tokens: int = 300) -> None:
        self._url        = base_url.rstrip("/") + "/api/chat"
        self._model      = model
        self._timeout    = timeout
        self._max_tokens = max_tokens
        log.info("LLM: OllamaLLM → %s (model=%s)", base_url, model)

    def chat(
        self,
        user_message: str,
        system_prompt: str = "",
        history: list[dict] | None = None,
    ) -> str:
        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        try:
            resp = requests.post(
                self._url,
                json={"model": self._model, "messages": messages, "stream": False,
                      "options": {"num_predict": self._max_tokens}},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            return resp.json()["message"]["content"].strip()
        except requests.exceptions.Timeout:
            log.warning("LLM-Anfrage: Timeout nach %.1fs", self._timeout)
            return _DEFAULT_FALLBACK
        except Exception as exc:
            log.error("LLM-Anfrage fehlgeschlagen: %s", exc)
            return _DEFAULT_FALLBACK


def load(cfg: dict) -> LLMClient:
    """
    Erstellt einen LLMClient aus dem 'llm'-Block der config.yaml.
    Gibt DummyLLM zurück wenn LLM deaktiviert, nicht konfiguriert oder
    nicht erreichbar (Verbindungsfehler werden abgefangen).

    provider: ollama       → OllamaLLM        (/api/chat, Ollama nativ)
    provider: openai_compat → OpenAICompatibleLLM (/v1/chat/completions)
      → kompatibel mit Ollama ≥ 0.1.24, GPT4All, LM Studio, Groq, ...
    """
    if not cfg or not cfg.get("enabled", False):
        fallback = (cfg or {}).get("fallback_response", _DEFAULT_FALLBACK)
        return DummyLLM(fallback)

    base_url = cfg.get("base_url", "").strip()
    if not base_url:
        log.warning("LLM: enabled=true aber base_url fehlt — DummyLLM als Fallback")
        return DummyLLM(cfg.get("fallback_response", _DEFAULT_FALLBACK))

    provider = cfg.get("provider", "openai_compat")
    timeout   = float(cfg.get("timeout", 10.0))
    max_tokens = int(cfg.get("max_tokens", 300))
    model     = cfg.get("model", "llama3.2")

    if provider == "ollama":
        return OllamaLLM(base_url=base_url, model=model, timeout=timeout, max_tokens=max_tokens)

    return OpenAICompatibleLLM(
        base_url=base_url,
        model=model,
        api_key=cfg.get("api_key", ""),
        timeout=timeout,
        max_tokens=max_tokens,
    )
