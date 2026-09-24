"""VoiceID-Logs zusätzlich an den Log-Collector senden (#347), über die Lib hannah-logging.

Die bestehende Ausgabe (stdout/journald) bleibt unverändert. Die Lib puffert ab install()
und schickt, sobald Hannah Core einen Collector meldet (Discovery über `hannah.address`).
Ohne `hannah.address` wird nur gepuffert, nichts verschickt.

Die Logs von VoiceID beziehen sich praktisch immer auf eine Person (Profil-ID, Erkennungs-
ergebnis) und laufen deshalb komplett als METADATA, damit ein Export sie weglassen kann.
"""
from __future__ import annotations

import re
from typing import Any, Iterator, Optional

import hannah_logging

# Logger, über den app.py loggt — alles darunter ist METADATA
LOGGER_NAME = "voiceid"

# Config-Schlüssel, deren Wert ein Secret ist. Der Name muss auf eines dieser Wörter enden.
_SECRET_KEY = re.compile(r"(password|passwd|secret|secret_key|api_key|azure_key|access_key|token|psk)$", re.I)


def install(version: str, *, hannah_address: Optional[str] = None, cfg: Any = None) -> hannah_logging.LogShipping:
    """Hängt den Handler an den Root-Logger und aktiviert, falls `hannah_address` gesetzt
    ist, die Discovery über Hannah Core. So früh wie möglich aufrufen."""
    return hannah_logging.install(
        component="voiceid",
        version=version,
        hannah_address=hannah_address or None,
        secrets=list(config_secrets(cfg)) if cfg is not None else [],
        logger_categories={LOGGER_NAME: hannah_logging.METADATA},
    )


def hannah_address(cfg: dict) -> Optional[str]:
    """`hannah.address` aus der Config (host:port von Hannah Core), oder None."""
    hannah = cfg.get("hannah")
    if not isinstance(hannah, dict) or not hannah.get("address"):
        return None
    return str(hannah["address"]).strip() or None


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
