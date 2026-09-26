from dataclasses import dataclass
from unittest.mock import patch

from hannah_telegram import log_shipping
from hannah_telegram.config import Config


def test_config_secrets_finds_nested_secret_values_only():
    cfg = {
        "telegram_token": "bot-secret",
        "webui_url": "https://hannah.example.com",
        "grpc": {"host": "hannah-core", "port": 50051},
        "nested": {"api_key": "abc123", "keep": "public"},
    }
    assert list(log_shipping.config_secrets(cfg)) == ["bot-secret", "abc123"]


def test_config_secrets_finds_token_in_real_config_dataclass():
    """The real Config is a dataclass — its field names must be checked like dict keys (#353)."""
    cfg = Config(telegram_token="bot-secret", webui_url="https://hannah.example.com")
    assert list(log_shipping.config_secrets(cfg)) == ["bot-secret"]


def test_config_secrets_finds_secret_in_nested_dataclass():
    @dataclass
    class Inner:
        api_key: str = "abc123"
        host: str = "public"

    @dataclass
    class Outer:
        inner: Inner
        name: str = "public"

    assert list(log_shipping.config_secrets(Outer(inner=Inner()))) == ["abc123"]


def test_config_secrets_skips_empty_secret_values():
    assert list(log_shipping.config_secrets(Config())) == []
    assert list(log_shipping.config_secrets({"telegram_token": ""})) == []


def test_install_uses_hannah_discovery_and_masks_secrets():
    cfg = {"telegram_token": "bot-secret", "grpc": {"host": "hannah-core", "port": 50051}}

    with patch("hannah_logging.install") as install:
        log_shipping.install("dev", hannah_address="hannah-core:50051", cfg=cfg)

    install.assert_called_once()
    kwargs = install.call_args.kwargs
    assert kwargs["component"] == "telegram"
    assert kwargs["version"] == "dev"
    assert kwargs["hannah_address"] == "hannah-core:50051"
    assert "bot-secret" in kwargs["secrets"]
