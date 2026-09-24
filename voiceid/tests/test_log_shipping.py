from unittest.mock import patch

import hannah_logging

import log_shipping


def test_config_secrets_finds_nested_secret_values_only():
    cfg = {
        "server": {"host": "0.0.0.0", "port": 8080},
        "nested": {"api_key": "abc123", "keep": "public"},
        "items": [{"password": "pw"}],
    }
    assert list(log_shipping.config_secrets(cfg)) == ["abc123", "pw"]


def test_hannah_address_from_config():
    assert log_shipping.hannah_address({"hannah": {"address": " core:50051 "}}) == "core:50051"
    assert log_shipping.hannah_address({"hannah": {"address": ""}}) is None
    assert log_shipping.hannah_address({"hannah": None}) is None
    assert log_shipping.hannah_address({}) is None


def test_install_passes_component_address_and_metadata_category():
    cfg = {"hannah": {"address": "core:50051"}, "api_key": "abc123"}

    with patch("hannah_logging.install") as install:
        log_shipping.install("0.88.0", hannah_address="core:50051", cfg=cfg)

    kwargs = install.call_args.kwargs
    assert kwargs["component"] == "voiceid"
    assert kwargs["version"] == "0.88.0"
    assert kwargs["hannah_address"] == "core:50051"
    assert kwargs["secrets"] == ["abc123"]
    assert kwargs["logger_categories"] == {"voiceid": hannah_logging.METADATA}


def test_install_without_address_only_buffers():
    with patch("hannah_logging.install") as install:
        log_shipping.install("dev", hannah_address=None, cfg={})

    assert install.call_args.kwargs["hannah_address"] is None
