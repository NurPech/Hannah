"""Configuration loader for hannah-telegram."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class GrpcConfig:
    host: str = "127.0.0.1"
    port: int = 50051


@dataclass
class Config:
    # Telegram Bot Token (from @BotFather)
    telegram_token: str = ""
    grpc: GrpcConfig = field(default_factory=GrpcConfig)


def load(path: str | Path = "config.yaml") -> Config:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

    def _section(cls, key: str):
        data = raw.get(key, {}) or {}
        fields = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in fields})

    return Config(
        telegram_token=raw.get("telegram_token", ""),
        grpc=_section(GrpcConfig, "grpc"),
    )
