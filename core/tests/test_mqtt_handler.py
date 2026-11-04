import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from paho.mqtt.client import MQTTErrorCode, convert_disconnect_error_code_to_reason_code

from hannah import mqtt_handler
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


class TestLedBrightness:
    """#453 — der Satellit meldet seine LED-Helligkeit auf led_brightness/state (Prozent als Text),
    Core hält sie als retained led_brightness/set fest."""

    def _raw(self, topic: str, payload: bytes, retain: bool = False) -> SimpleNamespace:
        return SimpleNamespace(topic=topic, payload=payload, retain=retain)

    def test_retained_snapshot_is_ignored(self):
        """Der retained Stand beim Abonnieren kann von einem Satelliten stammen, der gerade aus ist."""
        handler = MQTTHandler({}, {})
        results = []
        handler.set_led_brightness_handler(lambda *a: results.append(a))

        handler._on_message(None, None, self._raw(
            "hannah/satellite/wz-sat/led_brightness/state", b"30", retain=True,
        ))

        assert results == []

    def test_dispatches_reported_value(self):
        handler = MQTTHandler({}, {})
        results = []
        handler.set_led_brightness_handler(lambda device, percent: results.append((device, percent)))

        handler._on_message(None, None, self._raw("hannah/satellite/wz-sat/led_brightness/state", b"30"))

        assert results == [("wz-sat", 30)]

    def test_zero_is_dispatched(self):
        handler = MQTTHandler({}, {})
        results = []
        handler.set_led_brightness_handler(lambda device, percent: results.append((device, percent)))

        handler._on_message(None, None, self._raw("hannah/satellite/wz-sat/led_brightness/state", b"0"))

        assert results == [("wz-sat", 0)]

    def test_value_is_clamped(self):
        handler = MQTTHandler({}, {})
        results = []
        handler.set_led_brightness_handler(lambda device, percent: results.append(percent))

        handler._on_message(None, None, self._raw("hannah/satellite/wz-sat/led_brightness/state", b"180"))

        assert results == [100]

    def test_non_numeric_payload_is_ignored(self):
        handler = MQTTHandler({}, {})
        results = []
        handler.set_led_brightness_handler(lambda *a: results.append(a))

        handler._on_message(None, None, self._raw("hannah/satellite/wz-sat/led_brightness/state", b"hell"))

        assert results == []

    def test_no_handler_registered_does_not_raise(self):
        handler = MQTTHandler({}, {})

        handler._on_message(None, None, self._raw("hannah/satellite/wz-sat/led_brightness/state", b"30"))

    def test_set_is_published_retained(self):
        handler = MQTTHandler({}, {})
        handler._client = MagicMock()

        handler.publish_led_brightness_set("wz-sat", 30)

        handler._client.publish.assert_called_once_with(
            "hannah/satellite/wz-sat/led_brightness/set", "30", qos=1, retain=True,
        )

    def test_state_topic_is_subscribed_on_connect(self):
        handler = MQTTHandler({}, {})
        client = MagicMock()

        handler._on_connect(client, None, None, 0, None)

        topics = [call.args[0] for call in client.subscribe.call_args_list]
        assert "hannah/satellite/+/led_brightness/state" in topics


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


class TestPublishSyslogTarget:
    """#417: Syslog-Ziel der Satelliten, retained wie hannah/server."""

    def test_is_retained_on_the_syslog_topic(self):
        handler = MQTTHandler({}, {})
        handler._client = MagicMock()

        handler.publish_syslog_target('{"host": "10.0.0.5", "port": 5514}')

        handler._client.publish.assert_called_once_with(
            "hannah/syslog", '{"host": "10.0.0.5", "port": 5514}', qos=1, retain=True,
        )


class TestConnectionLogging:
    """#460: seit connect_async() blieb ein nicht erreichbarer Broker und jeder Abbruch
    einer bestehenden Verbindung komplett still im Log."""

    @pytest.fixture
    def handler(self, caplog):
        caplog.set_level(logging.INFO, logger="hannah.mqtt_handler")
        return MQTTHandler({"host": "192.0.2.10", "port": 1888}, {})

    @pytest.fixture
    def clock(self, monkeypatch):
        now = {"t": 1000.0}
        monkeypatch.setattr(mqtt_handler.time, "monotonic", lambda: now["t"])
        return now

    @staticmethod
    def _records(caplog, level):
        return [r.getMessage() for r in caplog.records if r.levelno == level]

    def test_callbacks_are_registered(self, handler):
        assert handler._client.on_connect_fail == handler._on_connect_fail
        assert handler._client.on_disconnect == handler._on_disconnect

    def test_first_connect_failure_names_host_and_port(self, handler, clock, caplog):
        handler._on_connect_fail(handler._client, None)

        warnings = self._records(caplog, logging.WARNING)
        assert len(warnings) == 1
        assert "192.0.2.10:1888" in warnings[0]

    def test_repeated_connect_failures_are_throttled(self, handler, clock, caplog):
        for _ in range(5):
            handler._on_connect_fail(handler._client, None)
            clock["t"] += 10.0  # 50 s in total, below the interval

        assert len(self._records(caplog, logging.WARNING)) == 1

    def test_connect_failure_is_logged_again_after_the_interval(self, handler, clock, caplog):
        handler._on_connect_fail(handler._client, None)
        clock["t"] += mqtt_handler.CONNECT_FAIL_LOG_INTERVAL_S
        handler._on_connect_fail(handler._client, None)

        warnings = self._records(caplog, logging.WARNING)
        assert len(warnings) == 2
        assert "2. Versuch" in warnings[1]

    def test_lost_connection_is_a_warning_with_reason_code(self, handler, caplog):
        reason = convert_disconnect_error_code_to_reason_code(MQTTErrorCode.MQTT_ERR_CONN_LOST)

        handler._on_disconnect(handler._client, None, None, reason, None)

        warnings = self._records(caplog, logging.WARNING)
        assert len(warnings) == 1
        assert "192.0.2.10:1888" in warnings[0]
        assert str(reason) in warnings[0]

    def test_requested_disconnect_is_not_a_warning(self, handler, caplog):
        reason = convert_disconnect_error_code_to_reason_code(MQTTErrorCode.MQTT_ERR_SUCCESS)

        handler._on_disconnect(handler._client, None, None, reason, None)

        assert self._records(caplog, logging.WARNING) == []
        assert handler._had_connection_problem is False

    def test_first_failure_after_a_lost_connection_is_logged_immediately(self, handler, clock, caplog):
        handler._on_connect_fail(handler._client, None)
        reason = convert_disconnect_error_code_to_reason_code(MQTTErrorCode.MQTT_ERR_CONN_LOST)
        handler._on_disconnect(handler._client, None, None, reason, None)
        caplog.clear()

        handler._on_connect_fail(handler._client, None)  # no clock advance

        assert len(self._records(caplog, logging.WARNING)) == 1

    def test_recovery_is_logged_with_the_number_of_failed_attempts(self, handler, clock, caplog):
        handler._on_connect_fail(handler._client, None)
        handler._on_connect_fail(handler._client, None)
        caplog.clear()

        handler._on_connect(MagicMock(), None, None, 0, None)

        infos = self._records(caplog, logging.INFO)
        assert any("wiederhergestellt nach 2 fehlgeschlagenen Versuchen" in m for m in infos)

    def test_first_successful_connect_is_not_reported_as_recovery(self, handler, caplog):
        handler._on_connect(MagicMock(), None, None, 0, None)

        infos = self._records(caplog, logging.INFO)
        assert any("192.0.2.10:1888 verbunden" in m for m in infos)
        assert not any("wiederhergestellt" in m for m in infos)

    def test_state_is_reset_after_a_successful_connect(self, handler, clock):
        handler._on_connect_fail(handler._client, None)

        handler._on_connect(MagicMock(), None, None, 0, None)

        assert handler._connect_fail_count == 0
        assert handler._had_connection_problem is False
        assert handler._last_connect_fail_log is None

    def test_refused_connection_names_the_broker(self, handler, caplog):
        handler._on_connect(MagicMock(), None, None, 5, None)

        errors = self._records(caplog, logging.ERROR)
        assert len(errors) == 1
        assert "192.0.2.10:1888" in errors[0]

    def test_discovery_says_when_the_broker_is_not_connected(self, handler, caplog):
        handler._client = MagicMock()
        handler._client.is_connected.return_value = False

        handler.publish_discovery("10.0.0.5", 7775)

        message = self._records(caplog, logging.INFO)[-1]
        assert "wird nach dem Connect gesendet" in message

    def test_discovery_is_plain_when_the_broker_is_connected(self, handler, caplog):
        handler._client = MagicMock()
        handler._client.is_connected.return_value = True

        handler.publish_discovery("10.0.0.5", 7775)

        message = self._records(caplog, logging.INFO)[-1]
        assert message.startswith("Discovery → hannah/server")
        assert "Connect" not in message
