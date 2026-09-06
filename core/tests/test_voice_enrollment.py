import threading

import numpy as np
import pytest

from hannah.voice_enrollment import VoiceEnrollmentManager, _OTHER_ENROLL_MIN_TRUST


class _FakeUser:
    def __init__(self, id, trust_level, display_name=""):
        self.id = id
        self.trust_level = trust_level
        self.display_name = display_name or f"user{id}"


class _FakeUserManager:
    """Stellt nur die eine Methode bereit, die VoiceEnrollmentManager tatsächlich
    braucht — kein echtes hannah.db-Setup nötig (gleiches Muster wie in test_activity_log.py)."""

    def __init__(self, users):
        self._users = {u.id: u for u in users}

    def get_user_by_id(self, user_id):
        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            return None
        return self._users.get(user_id)


class _FakeVoiceID:
    def __init__(self, ok=True, message="gespeichert"):
        self.ok = ok
        self.message = message
        self.calls: list[tuple[str, bytes, int]] = []

    def enroll(self, user_id, pcm, sample_rate=16000):
        self.calls.append((user_id, pcm, sample_rate))
        return self.ok, self.message


def _answer_audio(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * 16000), dtype=np.float32)


class _Recorder:
    """Sammelt ask_fn/confirm_fn-Aufrufe. confirm_fn ist immer der letzte Schritt in
    _run vor dem finally-Cleanup — done signalisiert daher zuverlässig 'Dialog fertig'."""

    def __init__(self):
        self.asked: list[tuple[str, str]] = []
        self.confirmed: list[tuple[str, str]] = []
        self.done = threading.Event()
        self.answer_provider = None  # (device_id, question) -> None, optional

    def ask_fn(self, device_id, question):
        self.asked.append((device_id, question))
        if self.answer_provider:
            self.answer_provider(device_id, question)

    def confirm_fn(self, device_id, text):
        self.confirmed.append((device_id, text))
        self.done.set()


@pytest.fixture
def recorder():
    return _Recorder()


def _make_manager(recorder, voice_id, *, users, questions=None,
                   target_speech_s=20.0, max_questions=10):
    return VoiceEnrollmentManager(
        _FakeUserManager(users), voice_id,
        questions=questions if questions is not None else [f"Frage {i}" for i in range(10)],
        target_speech_s=target_speech_s,
        max_questions=max_questions,
        ask_fn=recorder.ask_fn,
        confirm_fn=recorder.confirm_fn,
    )


class TestTrustGate:
    def test_self_enrollment_always_allowed_regardless_of_trust(self, recorder):
        recorder.answer_provider = lambda device_id, question: mgr.try_consume(
            device_id, _answer_audio(21.0), 16000
        )
        mgr = _make_manager(recorder, _FakeVoiceID(), users=[_FakeUser(1, trust_level=0)])

        ok, _msg = mgr.start(requestor_id=1, user_id=1, device_id="dev1")

        assert ok is True
        assert recorder.done.wait(timeout=2.0)

    def test_enrolling_other_below_threshold_rejected(self, recorder):
        mgr = _make_manager(
            recorder, _FakeVoiceID(),
            users=[_FakeUser(1, trust_level=_OTHER_ENROLL_MIN_TRUST - 1), _FakeUser(2, trust_level=0)],
        )

        ok, msg = mgr.start(requestor_id=1, user_id=2, device_id="dev1")

        assert ok is False
        assert "Trust-Level" in msg
        assert not recorder.asked  # Dialog wurde nie gestartet

    def test_enrolling_other_at_threshold_allowed(self, recorder):
        recorder.answer_provider = lambda device_id, question: mgr.try_consume(
            device_id, _answer_audio(21.0), 16000
        )
        mgr = _make_manager(
            recorder, _FakeVoiceID(),
            users=[_FakeUser(1, trust_level=_OTHER_ENROLL_MIN_TRUST), _FakeUser(2, trust_level=0)],
        )

        ok, _msg = mgr.start(requestor_id=1, user_id=2, device_id="dev1")

        assert ok is True
        assert recorder.done.wait(timeout=2.0)


class TestDoubleSessionGuard:
    def test_rejects_second_session_on_same_device_while_first_runs(self, recorder):
        # _active_devices.add() passiert synchron in start(), bevor der Hintergrund-Thread
        # überhaupt startet — kein Timing-Race, kein Sleep/Polling nötig.
        block = threading.Event()

        def _provider(device_id, question):
            block.wait(timeout=2.0)
            mgr.try_consume(device_id, _answer_audio(21.0), 16000)

        recorder.answer_provider = _provider
        mgr = _make_manager(recorder, _FakeVoiceID(), users=[_FakeUser(1, trust_level=0)])

        ok1, _ = mgr.start(requestor_id=1, user_id=1, device_id="dev1")
        assert ok1 is True

        ok2, msg2 = mgr.start(requestor_id=1, user_id=1, device_id="dev1")
        assert ok2 is False
        assert "läuft bereits" in msg2

        block.set()
        assert recorder.done.wait(timeout=2.0)


class TestDialogLoop:
    def test_accumulates_speech_and_enrolls_once_target_reached(self, recorder):
        def _provider(device_id, question):
            mgr.try_consume(device_id, _answer_audio(3.0), 16000)

        recorder.answer_provider = _provider
        voice_id = _FakeVoiceID()
        mgr = _make_manager(
            recorder, voice_id, users=[_FakeUser(1, trust_level=0, display_name="Leonie")],
            questions=[f"Frage {i}" for i in range(10)], target_speech_s=5.0, max_questions=10,
        )

        ok, _msg = mgr.start(requestor_id=1, user_id=1, device_id="dev1")
        assert ok is True
        assert recorder.done.wait(timeout=2.0)

        # Intro + genau 2 Fragen (3s + 3s = 6s >= 5s Ziel)
        assert len(recorder.asked) == 3
        assert len(voice_id.calls) == 1
        user_id, pcm, sample_rate = voice_id.calls[0]
        assert user_id == "1"
        assert sample_rate == 16000
        assert len(pcm) == 2 * int(3.0 * 16000) * 2  # 2 Antworten x int16-Bytes
        assert recorder.confirmed[-1] == ("dev1", "Stimmabdruck für Leonie gespeichert.")

    def test_skips_question_on_timeout_and_continues(self, recorder, monkeypatch):
        monkeypatch.setattr("hannah.voice_enrollment._ANSWER_TIMEOUT_S", 0.05)
        calls = {"n": 0}

        def _provider(device_id, question):
            calls["n"] += 1
            if calls["n"] == 1:
                return  # keine Antwort -> Timeout, Frage wird übersprungen
            mgr.try_consume(device_id, _answer_audio(25.0), 16000)  # reicht allein schon fürs Ziel

        recorder.answer_provider = _provider
        voice_id = _FakeVoiceID()
        mgr = _make_manager(recorder, voice_id, users=[_FakeUser(1, trust_level=0)],
                             questions=["Q1", "Q2", "Q3"], target_speech_s=20.0, max_questions=10)

        ok, _msg = mgr.start(requestor_id=1, user_id=1, device_id="dev1")
        assert ok is True
        assert recorder.done.wait(timeout=2.0)

        assert calls["n"] == 2
        assert len(voice_id.calls) == 1

    def test_aborts_with_no_enroll_call_if_no_answers_at_all(self, recorder, monkeypatch):
        monkeypatch.setattr("hannah.voice_enrollment._ANSWER_TIMEOUT_S", 0.05)
        recorder.answer_provider = lambda device_id, question: None  # nie eine Antwort
        voice_id = _FakeVoiceID()
        mgr = _make_manager(recorder, voice_id, users=[_FakeUser(1, trust_level=0)],
                             questions=["Q1", "Q2"], target_speech_s=20.0, max_questions=2)

        ok, _msg = mgr.start(requestor_id=1, user_id=1, device_id="dev1")
        assert ok is True
        assert recorder.done.wait(timeout=2.0)

        assert voice_id.calls == []
        assert recorder.confirmed[-1] == (
            "dev1", "Ich habe leider keine Antworten bekommen — Enrollment abgebrochen.",
        )


class TestTryConsume:
    def test_returns_false_for_device_without_pending_question(self, recorder):
        mgr = _make_manager(recorder, _FakeVoiceID(), users=[_FakeUser(1, trust_level=0)])

        assert mgr.try_consume("unknown-device", _answer_audio(1.0), 16000) is False
