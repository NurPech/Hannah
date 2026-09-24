"""
Core-Logs zusätzlich an den Log-Collector schicken (#341), über die Lib hannah-logging.

Die bestehende Ausgabe (stdout/journald, Syslog→Loki) bleibt unverändert. Die Lib puffert
ab install() und schickt, sobald ein Collector bekannt ist. Core kennt die Adresse aus der
eigenen Component-Registry (der Collector meldet sich per LogCollectorConnect an) und gibt
sie direkt an die Lib, statt den eigenen SubscribeInfrastructure-Broadcast zu abonnieren.

Kategorien, damit ein Export sensible Inhalte weglassen kann:
- TRANSCRIPT: was Nutzer gesagt oder geschrieben haben, pro Log-Aufruf über extra=TRANSCRIPT
- METADATA:   Personen- und Anwesenheitsbezug, über die Logger der betroffenen Module.
              Raum- und Gerätenamen allein sind kein METADATA — sie stehen in fast jeder Zeile.
"""
import logging
import re
from typing import Any, Iterator, Optional

import hannah_logging

from hannah.component_registry import (
    ComponentRegistry, EVENT_REGISTERED, EVENT_UNREGISTERED, KIND_LOG_COLLECTOR,
)

log = logging.getLogger(__name__)

# extra= für Log-Aufrufe, die Nutzertext enthalten: log.info(f"... {text!r}", extra=TRANSCRIPT)
TRANSCRIPT = {hannah_logging.CATEGORY_ATTR: hannah_logging.TRANSCRIPT}

_METADATA_LOGGERS = (
    "hannah.presence_manager",
    "hannah.residents_manager",
    "hannah.ble_location",
    "hannah.voiceid",
    "hannah.activity_log",
)

# Config-Schlüssel, deren Wert ein Secret ist. Der Name muss auf eines dieser Wörter
# enden — "state_key" oder "max_tokens" sind keine Secrets.
_SECRET_KEY = re.compile(r"(password|passwd|secret|secret_key|api_key|azure_key|access_key|token|psk)$", re.I)


def install(version: str) -> hannah_logging.LogShipping:
    """Hängt den Handler an den Root-Logger. So früh wie möglich aufrufen (direkt nach
    setup_logging), damit ab Prozessstart gepuffert wird."""
    return hannah_logging.install(
        "core",
        version=version,
        logger_categories={name: hannah_logging.METADATA for name in _METADATA_LOGGERS},
    )


def config_secrets(cfg: Any) -> Iterator[str]:
    """Alle Secret-Werte aus der Config (verschachtelte Dicts/Listen)."""
    if isinstance(cfg, dict):
        for key, value in cfg.items():
            if isinstance(value, str) and _SECRET_KEY.search(str(key)):
                yield value
            else:
                yield from config_secrets(value)
    elif isinstance(cfg, list):
        for item in cfg:
            yield from config_secrets(item)


def _format_address(host: str, port: int) -> str:
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"  # IPv6
    return f"{host}:{port}"


def follow_registry(shipping: hannah_logging.LogShipping, registry: ComponentRegistry) -> None:
    """Gibt die Adresse des angemeldeten Log-Collectors an die Lib weiter und folgt
    An- und Abmeldungen. Ohne Collector läuft der Puffer der Lib als Ring weiter.

    Vor dem Start des gRPC-Servers aufrufen: Dann kann sich zwischen Snapshot und
    Listener noch kein Collector an- oder abmelden."""
    collectors: dict[str, str] = {}  # instance -> host:port

    def apply() -> None:
        # Mehrere Collector sind unüblich — deterministisch einen wählen.
        address: Optional[str] = collectors[min(collectors)] if collectors else None
        shipping.set_collector_address(address)

    def on_change(event: str, kind: str, name: str, handle: Any) -> None:
        # Läuft unter dem Registry-Lock: set_collector_address blockiert nicht.
        if kind != KIND_LOG_COLLECTOR:
            return
        if event == EVENT_REGISTERED:
            collectors[name] = _format_address(handle.host, handle.port)
        elif event == EVENT_UNREGISTERED:
            collectors.pop(name, None)
        apply()

    for kind, name, handle in registry.subscribe(on_change):
        if kind == KIND_LOG_COLLECTOR:
            collectors[name] = _format_address(handle.host, handle.port)
    if collectors:
        apply()
