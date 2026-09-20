import copy
import os
from pathlib import Path

import yaml

_ENV_PREFIX = "HANNAH_CORE_"


def load(path: str = "config.yaml") -> dict:
    config_path = Path(path)
    if not config_path.exists():
        # Config-Datei ist optional, sofern genug per Env kommt (siehe _apply_env_overrides) —
        # anders als bei einer vorhandenen, aber kaputten Datei (siehe Fehler unten) ist das kein
        # Nutzerfehler, sondern der Normalfall für rein env-basierte Deployments (z.B. Docker).
        return _apply_env_overrides({})
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            parsed = yaml.safe_load(f)
    except UnicodeDecodeError as e:
        raise ValueError(
            f"Config '{config_path.absolute()}' ist nicht UTF-8-kodiert ({e}) — Datei mit einem "
            "Editor öffnen, der explizit UTF-8 speichert, und neu speichern."
        ) from e
    if not isinstance(parsed, dict):
        raise ValueError(
            f"Config '{config_path.absolute()}' enthält kein gültiges YAML-Mapping "
            f"(gelesen: {type(parsed).__name__}) — Datei prüfen, evtl. wurde versehentlich "
            "ein Shell-Befehl statt der eigentlichen Config hineinkopiert."
        )
    return _apply_env_overrides(parsed)


def _apply_env_overrides(cfg: dict) -> dict:
    """Jeder Config-Pfad ist per Env überschreibbar, ohne Allowlist. Namensschema:
    HANNAH_CORE_<PATH> — komponentenspezifisches Präfix (nicht bloß "HANNAH_"), weil das
    Projekt weitere HANNAH_*-Variablen für ganz andere Zwecke kennt (Asset-/Update-Server-
    Tokens, Satellite-NVS — siehe CI-Pipeline-Variablen), die sonst versehentlich als
    Config-Keys eingesammelt würden. "." zwischen Verschachtelungsebenen wird IMMER zu "__"
    (auch ohne Not), einfache "_" innerhalb eines Key-Namens bleiben erhalten — sonst ist das
    Rückwärts-Parsen mehrdeutig (mqtt.host -> HANNAH_CORE_MQTT__HOST,
    llm.fallback_response -> HANNAH_CORE_LLM__FALLBACK_RESPONSE)."""
    result = copy.deepcopy(cfg)
    for env_key, raw_value in os.environ.items():
        if not env_key.startswith(_ENV_PREFIX):
            continue
        path = [segment.lower() for segment in env_key[len(_ENV_PREFIX):].split("__")]
        if not path or not all(path):
            continue
        node = result
        for segment in path[:-1]:
            child = node.get(segment)
            if not isinstance(child, dict):
                child = {}
                node[segment] = child
            node = child
        node[path[-1]] = _coerce(raw_value)
    return result


def _coerce(value: str):
    """Env-Werte sind immer Strings — ohne Schema die beste Annäherung an den Typ,
    den derselbe Wert in YAML hätte (int/float/bool vor String-Fallback)."""
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def get(cfg: dict, *keys, default=None):
    """Sicher verschachtelte Werte auslesen: get(cfg, 'mqtt', 'host')"""
    val = cfg
    for key in keys:
        if not isinstance(val, dict):
            return default
        val = val.get(key, default)
    return val
