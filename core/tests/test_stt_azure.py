from unittest.mock import MagicMock

import numpy as np
import pytest

from hannah import stt as stt_mod
from hannah.stt import STT, _AzureSTT


def _azure() -> _AzureSTT:
    return _AzureSTT({"azure_key": "k", "azure_region": "germanywestcentral", "language": "de"})


def _answer(monkeypatch, payload: dict):
    resp = MagicMock()
    resp.json.return_value = payload
    monkeypatch.setattr(stt_mod.requests, "post", lambda *a, **kw: resp)


def test_azure_uses_interactive_mode_for_commands():
    """Der Modus 'conversation' transkribiert Hintergrundgespräche hinter den Befehl (#432)."""
    stt = _azure()

    assert "/speech/recognition/interactive/" in stt._url
    assert "/conversation/" not in stt._url


def test_azure_initial_silence_is_no_speech_not_an_error(monkeypatch):
    _answer(monkeypatch, {"RecognitionStatus": "InitialSilenceTimeout"})

    text, no_speech = _azure().transcribe(np.zeros(16000, dtype=np.float32))

    assert text == ""
    assert no_speech == 1.0


def test_azure_other_failure_status_still_raises(monkeypatch):
    _answer(monkeypatch, {"RecognitionStatus": "NoMatch"})

    with pytest.raises(ValueError):
        _azure().transcribe(np.zeros(16000, dtype=np.float32))


def test_initial_silence_does_not_fall_back_to_remote_or_local(monkeypatch):
    """Ohne diese Regel wartete jede Stille-Aufnahme auf den Remote-Timeout (#433)."""
    _answer(monkeypatch, {"RecognitionStatus": "InitialSilenceTimeout"})
    chain = STT.__new__(STT)
    chain._azure, chain._remote, chain._local = _azure(), MagicMock(), MagicMock()

    text, _ = chain.transcribe(np.zeros(16000, dtype=np.float32))

    assert text == ""
    chain._remote.transcribe.assert_not_called()
    chain._local.transcribe.assert_not_called()
