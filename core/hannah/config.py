import yaml
from pathlib import Path


def load(path: str = "config.yaml") -> dict:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config nicht gefunden: {config_path.absolute()}")
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
    return parsed


def get(cfg: dict, *keys, default=None):
    """Sicher verschachtelte Werte auslesen: get(cfg, 'mqtt', 'host')"""
    val = cfg
    for key in keys:
        if not isinstance(val, dict):
            return default
        val = val.get(key, default)
    return val
