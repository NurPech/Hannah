"""Configuration loader for hannah-telegram."""
from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_ENV_PREFIX = "HANNAH_TELEGRAM_"


@dataclass
class GrpcConfig:
    host: str = "127.0.0.1"
    port: int = 50051


@dataclass
class Config:
    # Telegram Bot Token (from @BotFather)
    telegram_token: str = ""
    webui_url: str = ""
    grpc: GrpcConfig = field(default_factory=GrpcConfig)


def load(path: str | Path = "config.yaml") -> Config:
    file_path = Path(path)
    raw = yaml.safe_load(file_path.read_text(encoding="utf-8")) if file_path.exists() else {}
    raw = raw or {}

    def _section(cls, key: str):
        data = raw.get(key, {}) or {}
        fields = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in fields})

    cfg = Config(
        telegram_token=raw.get("telegram_token", ""),
        webui_url=raw.get("webui_url", ""),
        grpc=_section(GrpcConfig, "grpc"),
    )
    _apply_env_overrides(cfg, [])
    return cfg


def _apply_env_overrides(obj, path_prefix: list[str]) -> None:
    """Jedes Dataclass-Feld ist per Env überschreibbar, ohne Allowlist. Namensschema:
    HANNAH_TELEGRAM_<PATH> — komponentenspezifisches Präfix (nicht bloß "HANNAH_"), sonst
    Kollision mit anderen, unabhängigen HANNAH_*-Variablen im Projekt (siehe Core, #327).
    "." zwischen Verschachtelungsebenen wird IMMER zu "__" (auch ohne Not), einfache "_"
    innerhalb eines Feldnamens bleiben erhalten (grpc.host -> HANNAH_TELEGRAM_GRPC__HOST)."""
    for f in dataclasses.fields(obj):
        value = getattr(obj, f.name)
        path = path_prefix + [f.name]
        if dataclasses.is_dataclass(value):
            _apply_env_overrides(value, path)
            continue
        env_name = _ENV_PREFIX + "__".join(segment.upper() for segment in path)
        if env_name in os.environ:
            setattr(obj, f.name, _coerce(os.environ[env_name], type(value)))


def _coerce(value: str, target_type: type):
    """Env-Werte sind immer Strings — passend zum aktuellen (Default- oder YAML-)Wert
    des Feldes konvertiert."""
    if target_type is bool:
        return value.lower() == "true"
    if target_type is int:
        return int(value)
    if target_type is float:
        return float(value)
    return value
