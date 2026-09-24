import logging

import hannah_logging

from hannah.component_registry import ComponentRegistry, KIND_CHANNEL, KIND_LOG_COLLECTOR
from hannah.log_shipping import TRANSCRIPT, config_secrets, follow_registry


class _Collector:
    def __init__(self, host, port):
        self.host = host
        self.port = port


class _FakeShipping:
    def __init__(self):
        self.addresses = []

    def set_collector_address(self, address):
        self.addresses.append(address)


def test_config_secrets_finds_nested_secret_values_only():
    cfg = {
        "mqtt": {"host": "broker", "password": "mqtt-pw"},
        "stt": {"azure_key": "az-key", "language": "de-DE"},
        "tts": {"polly_secret_key": "polly"},
        "llm": [{"api_key": "llm-key", "max_tokens": 512}],
        "iobroker": {"state_key": "presence"},
        "grpc": {"psk": "the-psk", "port": 50051},
    }
    assert sorted(config_secrets(cfg)) == sorted(["mqtt-pw", "az-key", "polly", "llm-key", "the-psk"])


def test_follow_registry_tracks_log_collector():
    registry = ComponentRegistry()
    shipping = _FakeShipping()
    follow_registry(shipping, registry)
    assert shipping.addresses == []  # nothing registered yet, nothing set

    first = _Collector("10.0.0.5", 50060)
    registry.register(KIND_LOG_COLLECTOR, "main", first)
    registry.register(KIND_CHANNEL, "telegram", object())  # other kinds are ignored
    registry.register(KIND_LOG_COLLECTOR, "main", _Collector("fd00::5", 50061))  # re-register, IPv6
    registry.unregister(KIND_LOG_COLLECTOR, "main", first)  # displaced handle: no effect
    current = registry.get(KIND_LOG_COLLECTOR, "main")
    registry.unregister(KIND_LOG_COLLECTOR, "main", current)

    assert shipping.addresses == ["10.0.0.5:50060", "[fd00::5]:50061", None]


def test_follow_registry_picks_up_already_registered_collector():
    registry = ComponentRegistry()
    registry.register(KIND_LOG_COLLECTOR, "main", _Collector("10.0.0.5", 50060))
    shipping = _FakeShipping()
    follow_registry(shipping, registry)
    assert shipping.addresses == ["10.0.0.5:50060"]


def test_transcript_extra_tags_the_record():
    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger("test.log_shipping")
    logger.addHandler(_Capture())
    logger.setLevel(logging.INFO)
    logger.info("heard something", extra=TRANSCRIPT)
    assert getattr(records[0], hannah_logging.CATEGORY_ATTR) == hannah_logging.TRANSCRIPT
