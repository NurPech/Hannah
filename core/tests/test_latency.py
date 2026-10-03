import logging
import re

from hannah import latency


def _line(caplog) -> str:
    lines = [r.getMessage() for r in caplog.records if r.name == latency.__name__]
    assert len(lines) == 1
    return lines[0]


def _fields(line: str) -> dict[str, str]:
    assert line.startswith("latency ")
    return dict(part.split("=", 1) for part in line.removeprefix("latency ").split(" "))


class TestStage:
    def test_noop_without_timer(self):
        with latency.stage("stt"):
            pass

    def test_accumulates_same_stage(self, monkeypatch, caplog):
        clock = iter([0.0, 0.0, 1.0, 1.0, 1.5, 2.0])  # start, stage1 enter/exit, stage2 enter/exit, finish
        monkeypatch.setattr(latency.time, "monotonic", lambda: next(clock))
        timer = latency.start("dev", 0)
        with latency.stage("llm"):
            pass
        with latency.stage("llm"):
            pass
        with caplog.at_level(logging.INFO, logger=latency.__name__):
            timer.finish("Smalltalk")
        assert _fields(_line(caplog))["llm_ms"] == "1500"

    def test_stage_recorded_when_body_raises(self, monkeypatch, caplog):
        clock = iter([0.0, 0.0, 2.0, 2.0])
        monkeypatch.setattr(latency.time, "monotonic", lambda: next(clock))
        timer = latency.start("dev", 0)
        try:
            with latency.stage("exec"):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        with caplog.at_level(logging.INFO, logger=latency.__name__):
            timer.finish("TurnOn")
        assert _fields(_line(caplog))["exec_ms"] == "2000"


class TestFinish:
    def test_line_fields(self, monkeypatch, caplog):
        clock = iter([10.0, 10.0, 10.5, 10.5, 12.0, 13.0])  # start, voiceid, stt, finish
        monkeypatch.setattr(latency.time, "monotonic", lambda: next(clock))
        timer = latency.start("9070690de424", 111680)
        with latency.stage("voiceid"):
            pass
        with latency.stage("stt"):
            pass
        with caplog.at_level(logging.INFO, logger=latency.__name__):
            timer.finish("TurnOn")
        line = _line(caplog)
        assert re.fullmatch(
            r"latency device=9070690de424 intent=TurnOn audio_ms=3490 total_ms=3000 "
            r"voiceid_ms=500 stt_ms=1500 other_ms=1000",
            line,
        )

    def test_stages_in_fixed_order_and_only_those_that_ran(self, caplog):
        timer = latency.start("dev", 0)
        for name in ("tts", "nlu", "voiceid"):
            with latency.stage(name):
                pass
        with caplog.at_level(logging.INFO, logger=latency.__name__):
            timer.finish("Query")
        keys = list(_fields(_line(caplog)))
        assert keys == ["device", "intent", "audio_ms", "total_ms", "voiceid_ms", "nlu_ms", "tts_ms", "other_ms"]

    def test_finish_releases_timer(self, caplog):
        timer = latency.start("dev", 0)
        with caplog.at_level(logging.INFO, logger=latency.__name__):
            timer.finish("Ignored")
        # Nach finish() darf eine Stufe keinen Timer mehr finden und nirgends mitzählen.
        with latency.stage("stt"):
            pass
        assert "stt" not in timer._stages
