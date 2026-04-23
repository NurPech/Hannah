import yaml
from pathlib import Path


def load(path: str = "config.yaml") -> dict:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config nicht gefunden: {config_path.absolute()}")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get(cfg: dict, *keys, default=None):
    """Sicher verschachtelte Werte auslesen: get(cfg, 'mqtt', 'host')"""
    val = cfg
    for key in keys:
        if not isinstance(val, dict):
            return default
        val = val.get(key, default)
    return val
