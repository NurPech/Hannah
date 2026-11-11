"""Import smoke test for the runtime packages that the other tests never load (#505).

app.py imports speechbrain lazily inside _load_model(), so a plain pytest run
never touches it, and a renamed module or keyword in a minor update stays green
until VoiceID fails to start. This test imports the symbol VoiceID really uses
and checks the keywords it passes. No model download, no network.

In CI (the CI variable is set) a missing package fails the test. On a developer
machine without the runtime requirements installed the test is skipped.
"""
import importlib
import inspect
import os

import pytest


def _import(name):
    if os.environ.get("CI"):
        return importlib.import_module(name)
    return pytest.importorskip(name)


def test_speechbrain_encoder_classifier_api():
    # app.py::_load_model calls EncoderClassifier.from_hparams(source=..., run_opts=...);
    # run_opts goes through **kwargs
    EncoderClassifier = _import("speechbrain.inference.speaker").EncoderClassifier
    params = inspect.signature(EncoderClassifier.from_hparams).parameters
    assert "source" in params
    assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
