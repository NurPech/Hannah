from unittest.mock import MagicMock, patch

import requests

from hannah.voiceid import NullVoiceID, VoiceIDClient, load


class TestLoad:
    def test_disabled_returns_null(self):
        assert isinstance(load({"enabled": False}), NullVoiceID)

    def test_empty_config_returns_null(self):
        assert isinstance(load({}), NullVoiceID)
        assert isinstance(load(None), NullVoiceID)

    def test_enabled_without_base_url_returns_null(self):
        assert isinstance(load({"enabled": True}), NullVoiceID)

    def test_enabled_with_base_url_returns_client(self):
        client = load({"enabled": True, "base_url": "http://localhost:8765"})
        assert isinstance(client, VoiceIDClient)


class TestNullVoiceID:
    def test_identify_always_anonymous(self):
        assert NullVoiceID().identify(b"\x00\x00") == ""

    def test_enroll_always_fails(self):
        ok, msg = NullVoiceID().enroll("1", b"\x00\x00")
        assert ok is False
        assert msg


class TestVoiceIDClientIdentify:
    def _client(self):
        return VoiceIDClient(base_url="http://localhost:8765", timeout=1.0)

    def test_known_speaker(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"user_id": "1", "confidence": 0.91}
        with patch("hannah.voiceid.requests.post", return_value=resp):
            assert self._client().identify(b"\x00\x00") == "1"

    def test_unknown_speaker(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"user_id": "unknown", "confidence": 0.1}
        with patch("hannah.voiceid.requests.post", return_value=resp):
            assert self._client().identify(b"\x00\x00") == ""

    def test_empty_speaker(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"user_id": "", "confidence": 0.0}
        with patch("hannah.voiceid.requests.post", return_value=resp):
            assert self._client().identify(b"\x00\x00") == ""

    def test_timeout_treated_as_anonymous(self):
        with patch("hannah.voiceid.requests.post", side_effect=requests.exceptions.Timeout):
            assert self._client().identify(b"\x00\x00") == ""

    def test_connection_error_treated_as_anonymous(self):
        with patch("hannah.voiceid.requests.post", side_effect=requests.exceptions.ConnectionError):
            assert self._client().identify(b"\x00\x00") == ""


class TestVoiceIDClientEnroll:
    def _client(self):
        return VoiceIDClient(base_url="http://localhost:8765", timeout=1.0)

    def test_success(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"ok": True, "message": "enrolled"}
        with patch("hannah.voiceid.requests.post", return_value=resp):
            ok, msg = self._client().enroll("1", b"\x00\x00")
        assert ok is True
        assert msg == "enrolled"

    def test_failure_returns_error(self):
        with patch("hannah.voiceid.requests.post", side_effect=requests.exceptions.ConnectionError("down")):
            ok, msg = self._client().enroll("1", b"\x00\x00")
        assert ok is False
        assert msg
