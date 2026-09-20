import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from hannah.mqtt_handler import MQTTHandler


def _msg(topic: str, payload: dict) -> SimpleNamespace:
    return SimpleNamespace(topic=topic, payload=json.dumps(payload).encode())


class TestPlayAssetResult:
    """#116: play_asset war Fire-and-Forget — der Satellit meldet jetzt per
    hannah/satellite/{device}/play_asset/result ein Ack/Nack zurück."""

    def test_dispatches_ok_result(self):
        handler = MQTTHandler({}, {})
        results = []
        handler.set_play_asset_result_handler(lambda device, asset_id, ok: results.append((device, asset_id, ok)))

        handler._on_message(None, None, _msg(
            "hannah/satellite/wz-sat/play_asset/result", {"asset_id": "alarm_ring", "ok": True},
        ))

        assert results == [("wz-sat", "alarm_ring", True)]

    def test_dispatches_nack_result(self):
        handler = MQTTHandler({}, {})
        results = []
        handler.set_play_asset_result_handler(lambda device, asset_id, ok: results.append((device, asset_id, ok)))

        handler._on_message(None, None, _msg(
            "hannah/satellite/wz-sat/play_asset/result", {"asset_id": "alarm_ring", "ok": False},
        ))

        assert results == [("wz-sat", "alarm_ring", False)]

    def test_missing_asset_id_is_ignored(self):
        handler = MQTTHandler({}, {})
        results = []
        handler.set_play_asset_result_handler(lambda *a: results.append(a))

        handler._on_message(None, None, _msg("hannah/satellite/wz-sat/play_asset/result", {"ok": True}))

        assert results == []

    def test_malformed_payload_does_not_raise(self):
        handler = MQTTHandler({}, {})
        handler.set_play_asset_result_handler(lambda *a: None)

        handler._on_message(None, None, SimpleNamespace(
            topic="hannah/satellite/wz-sat/play_asset/result", payload=b"not json",
        ))

    def test_no_handler_registered_does_not_raise(self):
        handler = MQTTHandler({}, {})

        handler._on_message(None, None, _msg(
            "hannah/satellite/wz-sat/play_asset/result", {"asset_id": "alarm_ring", "ok": True},
        ))


class TestFirmwareReport:
    """#165 — firmware-Topic-Payload trägt seit der Neustart-Metrik zusätzlich
    restart_reason/restart_count; ältere Firmware ohne diese Felder muss weiter
    funktionieren (Default ""/0)."""

    def test_dispatches_version_with_restart_info(self):
        handler = MQTTHandler({}, {})
        results = []
        handler.set_firmware_handler(lambda *a: results.append(a))

        handler._on_message(None, None, _msg(
            "hannah/satellite/wz-sat/firmware",
            {"version": "0.65.0", "restart_reason": "watchdog", "restart_count": 12},
        ))

        assert results == [("wz-sat", "0.65.0", "watchdog", 12)]

    def test_missing_restart_fields_default_to_empty(self):
        handler = MQTTHandler({}, {})
        results = []
        handler.set_firmware_handler(lambda *a: results.append(a))

        handler._on_message(None, None, _msg(
            "hannah/satellite/wz-sat/firmware", {"version": "0.60.0"},
        ))

        assert results == [("wz-sat", "0.60.0", "", 0)]

    def test_missing_version_is_ignored(self):
        handler = MQTTHandler({}, {})
        results = []
        handler.set_firmware_handler(lambda *a: results.append(a))

        handler._on_message(None, None, _msg(
            "hannah/satellite/wz-sat/firmware", {"restart_reason": "ota", "restart_count": 3},
        ))

        assert results == []


class TestCoredumpPendingReport:
    """#280 — Satellit meldet per retained hannah/satellite/{device}/coredump_pending,
    ob nach einem Crash ein per GET /debug/coredump abrufbarer Dump vorliegt."""

    def test_dispatches_pending_true(self):
        handler = MQTTHandler({}, {})
        results = []
        handler.set_coredump_pending_handler(lambda *a: results.append(a))

        handler._on_message(None, None, _msg(
            "hannah/satellite/wz-sat/coredump_pending", {"pending": True},
        ))

        assert results == [("wz-sat", True)]

    def test_dispatches_pending_false(self):
        handler = MQTTHandler({}, {})
        results = []
        handler.set_coredump_pending_handler(lambda *a: results.append(a))

        handler._on_message(None, None, _msg(
            "hannah/satellite/wz-sat/coredump_pending", {"pending": False},
        ))

        assert results == [("wz-sat", False)]

    def test_missing_pending_field_defaults_to_false(self):
        handler = MQTTHandler({}, {})
        results = []
        handler.set_coredump_pending_handler(lambda *a: results.append(a))

        handler._on_message(None, None, _msg("hannah/satellite/wz-sat/coredump_pending", {}))

        assert results == [("wz-sat", False)]

    def test_malformed_payload_does_not_raise(self):
        handler = MQTTHandler({}, {})
        handler.set_coredump_pending_handler(lambda *a: None)

        handler._on_message(None, None, SimpleNamespace(
            topic="hannah/satellite/wz-sat/coredump_pending", payload=b"not json",
        ))

    def test_no_handler_registered_does_not_raise(self):
        handler = MQTTHandler({}, {})

        handler._on_message(None, None, _msg(
            "hannah/satellite/wz-sat/coredump_pending", {"pending": True},
        ))


class TestPlaybackBusy:
    """#304 — Busy-Flag pro Satellit, unabhängig vom playback_done-Ack, damit
    überlappende TTS/Announcement-Sends auf denselben Satelliten verworfen statt
    überlagert werden können."""

    def test_not_busy_by_default(self):
        handler = MQTTHandler({}, {})
        assert handler.is_busy("wz-sat") is False

    def test_mark_busy_sets_flag(self):
        handler = MQTTHandler({}, {})
        handler.mark_busy("wz-sat")
        assert handler.is_busy("wz-sat") is True

    def test_clear_busy_resets_flag(self):
        handler = MQTTHandler({}, {})
        handler.mark_busy("wz-sat")
        handler.clear_busy("wz-sat")
        assert handler.is_busy("wz-sat") is False

    def test_busy_flag_is_per_device(self):
        handler = MQTTHandler({}, {})
        handler.mark_busy("wz-sat")
        assert handler.is_busy("ku-sat") is False

    def test_clear_busy_without_prior_mark_does_not_raise(self):
        handler = MQTTHandler({}, {})
        handler.clear_busy("wz-sat")
        assert handler.is_busy("wz-sat") is False


class TestConnect:
    """#326: connect() must not crash Core when the broker is unreachable at startup."""

    def test_uses_async_connect_and_does_not_raise_when_broker_unreachable(self):
        handler = MQTTHandler({"host": "127.0.0.1", "port": 1}, {})

        def _raise(*args, **kwargs):
            raise ConnectionRefusedError("refused")

        handler._client.connect = _raise  # the blocking call must never be used
        handler._client.connect_async = MagicMock()
        handler._client.loop_start = MagicMock()

        handler.connect()  # must not raise

        handler._client.connect_async.assert_called_once_with("127.0.0.1", 1, keepalive=60)
        handler._client.loop_start.assert_called_once()
