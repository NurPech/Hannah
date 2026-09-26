"""Telegram-Logs zusätzlich an den Log-Collector senden (#341)."""
from __future__ import annotations

import dataclasses
import logging
import re
from typing import Any, Iterable, Iterator, Optional

import hannah_logging

log = logging.getLogger(__name__)

# extra= für Log-Aufrufe, die Nutzertext enthalten: log.info(..., extra=TRANSCRIPT)
TRANSCRIPT = {hannah_logging.CATEGORY_ATTR: hannah_logging.TRANSCRIPT}

_SECRET_KEY = re.compile(r"(password|passwd|secret|secret_key|api_key|azure_key|access_key|token|psk)$", re.I)


def install(version: str, *, hannah_address: Optional[str] = None, cfg: Any = None) -> hannah_logging.LogShipping:
    """Hängt den Handler an den Root-Logger und aktiviert Discovery via Hannah Core."""
    secrets: list[str] = []
    if cfg is not None:
        secrets.extend(config_secrets(cfg))

    shipping = hannah_logging.install(
        component="telegram",
        version=version,
        hannah_address=hannah_address,
        secrets=secrets,
        logger_categories={"hannah_telegram": hannah_logging.METADATA},
    )
    return shipping


def config_secrets(cfg: Any) -> Iterator[str]:
    """Alle Secret-Werte aus einer Config-Struktur extrahieren."""
    if dataclasses.is_dataclass(cfg) and not isinstance(cfg, type):
        # Feldnamen wie Dict-Keys prüfen — sonst bleibt z.B. telegram_token unerkannt (#353)
        yield from _named_secrets((f.name, getattr(cfg, f.name)) for f in dataclasses.fields(cfg))
        return

    if isinstance(cfg, dict):
        yield from _named_secrets(cfg.items())
        return

    if isinstance(cfg, list):
        for item in cfg:
            yield from config_secrets(item)
        return

    if isinstance(cfg, tuple):
        for item in cfg:
            yield from config_secrets(item)


def _named_secrets(items: Iterable[tuple[Any, Any]]) -> Iterator[str]:
    """String-Werte, deren Name nach Secret aussieht; alles andere rekursiv durchsuchen.
    Leere Werte (z.B. nicht gesetzter Token) sind kein Secret — nichts zu maskieren."""
    for key, value in items:
        if isinstance(value, str) and _SECRET_KEY.search(str(key)):
            if value:
                yield value
        else:
            yield from config_secrets(value)
