"""Import smoke test for the runtime packages that the other tests never load (#505).

Core imports faster-whisper, piper and miniaudio lazily inside functions (stt.py,
tts.py), so a plain pytest run never touches them, and a renamed module or
keyword in a minor update stays green until it fails on the Pi. This test
imports the symbols Core really uses and checks the keywords it passes. No
model download, no network.

In CI (the CI variable is set) a missing package fails the test. On a developer
machine without the runtime requirements installed the tests are skipped.
"""
import importlib
import inspect
import math
import os
import struct
import wave

import pytest


def _import(name):
    if os.environ.get("CI"):
        return importlib.import_module(name)
    return pytest.importorskip(name)


def _params(func):
    return set(inspect.signature(func).parameters)


def test_faster_whisper_api():
    # stt.py::_LocalSTT
    WhisperModel = _import("faster_whisper").WhisperModel
    assert {"device", "compute_type"} <= _params(WhisperModel.__init__)
    assert {"language", "beam_size", "vad_filter", "vad_parameters"} <= _params(WhisperModel.transcribe)


def test_piper_api():
    # tts.py::_PiperBackend
    PiperVoice = _import("piper").PiperVoice
    SynthesisConfig = _import("piper.config").SynthesisConfig
    download_voice = _import("piper.download_voices").download_voice
    assert callable(PiperVoice.load)
    assert "syn_config" in _params(PiperVoice.synthesize_wav)
    assert callable(download_voice)
    SynthesisConfig(speaker_id=None, length_scale=1.0, noise_scale=0.667, noise_w_scale=0.8)


def test_miniaudio_decodes_a_wav(tmp_path):
    # tts.py::_load_audio decodes the confirmation tones with these arguments
    miniaudio = _import("miniaudio")
    path = tmp_path / "tone.wav"
    rate = 16000
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
            for i in range(rate // 10)
        ))
    decoded = miniaudio.decode_file(
        str(path), output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1,
    )
    # the sample rate is decoded.sample_rate (miniaudio resamples to its default)
    assert decoded.nchannels == 1
    frames = len(bytes(decoded.samples)) // 2
    assert abs(frames - decoded.sample_rate // 10) <= 2


def test_mysql_connector_import():
    # pyorm's Database.mysql() (utils/activity_db.py) needs the driver
    mysql_connector = _import("mysql.connector")
    assert callable(mysql_connector.connect)
