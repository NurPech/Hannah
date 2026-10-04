import logging
import re
from unittest.mock import MagicMock, patch

import requests

from hannah.llm import DummyLLM, LLMClient, OllamaLLM, OpenAICompatibleLLM, load


class _StubLLM(LLMClient):
    """LLMClient-Subklasse mit fest verdrahteter chat()-Antwort — testet nur die
    Parsing-Logik von classify(), ohne einen echten LLM-Call zu brauchen."""

    def __init__(self, response: str | None):
        self._response = response
        self.last_history = "unset"

    def chat(self, user_message, system_prompt="", history=None):
        self.last_history = history
        return self._response


class TestClassify:
    """#159 — classify() ist dreiwertig geworden (COMMAND/SMALLTALK/NOT_ADDRESSED),
    letzteres für Äußerungen im offenen Smalltalk-Follow-up-Mic-Fenster, die
    erkennbar nicht an Hannah gerichtet sind."""

    def test_command_detected(self):
        llm = _StubLLM("COMMAND")

        assert llm.classify("Mach das Licht an") == "COMMAND"

    def test_smalltalk_detected(self):
        llm = _StubLLM("SMALLTALK")

        assert llm.classify("Erzähl mir einen Witz") == "SMALLTALK"

    def test_not_addressed_detected(self):
        llm = _StubLLM("NOT_ADDRESSED")

        assert llm.classify("Und du so?") == "NOT_ADDRESSED"

    def test_lowercase_response_still_detected(self):
        llm = _StubLLM("not_addressed")

        assert llm.classify("...") == "NOT_ADDRESSED"

    def test_unexpected_response_defaults_to_smalltalk(self):
        llm = _StubLLM("keine Ahnung was das sein soll")

        assert llm.classify("...") == "SMALLTALK"

    def test_history_passed_through_to_chat(self):
        llm = _StubLLM("COMMAND")
        history = [{"role": "user", "content": "Hallo"}]

        llm.classify("Text", history=history)

        assert llm.last_history == history

    def test_history_defaults_to_none(self):
        llm = _StubLLM("COMMAND")

        llm.classify("Text")

        assert llm.last_history is None

    def test_failed_chat_defaults_to_command(self):
        """#215/#318 — chat() signalisiert einen Fehlschlag mit None; classify() darf
        dabei nicht crashen (.upper() auf None) und muss auf COMMAND zurückfallen —
        NLU funktioniert ohne LLM, SMALLTALK würde nur in einen zweiten, ebenso
        scheiternden chat()-Call laufen."""
        llm = _StubLLM(None)

        assert llm.classify("...") == "COMMAND"

    def test_empty_chat_response_defaults_to_command(self):
        """#318 — leere Antwort wie Fehlschlag behandeln."""
        llm = _StubLLM("")

        assert llm.classify("...") == "COMMAND"


class TestMatch:
    def test_failed_chat_defaults_to_false(self):
        """#215 — chat()==None darf match() nicht mit AttributeError crashen."""
        llm = _StubLLM(None)

        assert llm.match("...", "Zustimmung") is False


class TestDummyLLMClassify:
    """Kein LLM konfiguriert → immer COMMAND, unabhängig von History (#159)."""

    def test_always_returns_command(self):
        llm = DummyLLM()

        assert llm.classify("irgendwas") == "COMMAND"

    def test_always_command_even_with_history(self):
        llm = DummyLLM()

        assert llm.classify("irgendwas", history=[{"role": "user", "content": "x"}]) == "COMMAND"


class TestOpenAICompatibleLLMChatFailure:
    """#215 — chat() muss einen Fehlschlag erkennbar signalisieren (None) statt den
    Fallback-Text als valide Antwort zurückzugeben, sonst halten Aufrufer, die nur
    auf Truthy prüfen (z.B. process_notification()), einen fehlgeschlagenen Call
    für erfolgreich."""

    def test_timeout_returns_none(self):
        llm = OpenAICompatibleLLM(base_url="http://localhost:11434/v1", model="llama3.2")

        with patch("hannah.llm.requests.post", side_effect=requests.exceptions.Timeout):
            assert llm.chat("Hallo") is None

    def test_connection_error_returns_none(self):
        llm = OpenAICompatibleLLM(base_url="http://localhost:11434/v1", model="llama3.2")

        with patch("hannah.llm.requests.post", side_effect=requests.exceptions.ConnectionError):
            assert llm.chat("Hallo") is None


class TestOllamaLLMChatFailure:
    def test_timeout_returns_none(self):
        llm = OllamaLLM(base_url="http://localhost:11434", model="llama3.2")

        with patch("hannah.llm.requests.post", side_effect=requests.exceptions.Timeout):
            assert llm.chat("Hallo") is None

    def test_connection_error_returns_none(self):
        llm = OllamaLLM(base_url="http://localhost:11434", model="llama3.2")

        with patch("hannah.llm.requests.post", side_effect=requests.exceptions.ConnectionError):
            assert llm.chat("Hallo") is None


def _response(payload: dict):
    resp = MagicMock()
    resp.json.return_value = payload
    return resp


def _llm_call_line(caplog) -> str:
    lines = [r.getMessage() for r in caplog.records if r.name == "hannah.llm" and r.getMessage().startswith("llm_call ")]
    assert len(lines) == 1
    return lines[0]


class TestLlmCallLine:
    """#403 — eine llm_call-Zeile pro Aufruf, damit sich Wartezeit in Laden, Prefill, Decode
    und Denken aufteilen lässt."""

    def test_openai_chat_logs_usage(self, caplog):
        llm = OpenAICompatibleLLM(base_url="http://localhost:11434/v1", model="gemma4:e4b")
        payload = {
            "choices": [{"message": {"content": "Hallo Leonie."}}],
            "usage": {"prompt_tokens": 2100, "completion_tokens": 40},
        }

        with caplog.at_level(logging.INFO, logger="hannah.llm"), \
                patch("hannah.llm.requests.post", return_value=_response(payload)):
            assert llm.chat("Hallo") == "Hallo Leonie."

        line = _llm_call_line(caplog)
        assert re.fullmatch(
            r"llm_call model=gemma4:e4b kind=chat ms=\d+ prompt_tokens=2100 completion_tokens=40 "
            r"tok_s=[\d.]+ content_chars=13",
            line,
        )

    def test_openai_tools_logs_tool_calls_and_reasoning(self, caplog):
        llm = OpenAICompatibleLLM(base_url="http://localhost:11434/v1", model="gemma4:e4b")
        payload = {
            "choices": [{
                "message": {"content": "", "tool_calls": [{"id": "1"}, {"id": "2"}], "reasoning": "abc" * 10},
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 100, "completion_tokens": 500},
        }

        with caplog.at_level(logging.INFO, logger="hannah.llm"), \
                patch("hannah.llm.requests.post", return_value=_response(payload)):
            result = llm.chat_with_tools([{"role": "user", "content": "x"}], [])

        assert len(result["tool_calls"]) == 2
        line = _llm_call_line(caplog)
        assert "kind=tools" in line
        assert "tool_calls=2" in line
        assert "completion_tokens=500" in line
        assert "reasoning_chars=30" in line

    def test_openai_without_usage_still_logs_duration(self, caplog):
        llm = OpenAICompatibleLLM(base_url="http://localhost:11434/v1", model="m")
        payload = {"choices": [{"message": {"content": "Ok."}}]}

        with caplog.at_level(logging.INFO, logger="hannah.llm"), \
                patch("hannah.llm.requests.post", return_value=_response(payload)):
            assert llm.chat("Hallo") == "Ok."

        line = _llm_call_line(caplog)
        assert re.fullmatch(r"llm_call model=m kind=chat ms=\d+ content_chars=3", line)

    def test_ollama_native_logs_load_prefill_decode(self, caplog):
        llm = OllamaLLM(base_url="http://localhost:11434", model="gemma4:e4b")
        payload = {
            "message": {"content": "Hallo.", "thinking": "denk"},
            "prompt_eval_count": 2000, "eval_count": 50,
            "load_duration": 8_000_000_000, "prompt_eval_duration": 4_000_000_000,
            "eval_duration": 5_000_000_000,
        }

        with caplog.at_level(logging.INFO, logger="hannah.llm"), \
                patch("hannah.llm.requests.post", return_value=_response(payload)):
            assert llm.chat("Hallo") == "Hallo."

        line = _llm_call_line(caplog)
        assert "prompt_tokens=2000" in line
        assert "completion_tokens=50" in line
        assert "load_ms=8000" in line
        assert "prefill_ms=4000" in line
        assert "decode_ms=5000" in line
        assert "tok_s=10.0" in line
        assert "reasoning_chars=4" in line

    def test_failed_call_logs_no_line(self, caplog):
        llm = OpenAICompatibleLLM(base_url="http://localhost:11434/v1", model="m")

        with caplog.at_level(logging.INFO, logger="hannah.llm"), \
                patch("hannah.llm.requests.post", side_effect=requests.exceptions.Timeout):
            assert llm.chat("Hallo") is None

        assert not [r for r in caplog.records if r.getMessage().startswith("llm_call ")]


def _sent_payload(post: MagicMock) -> dict:
    return post.call_args.kwargs["json"]


_OPENAI_OK = {"choices": [{"message": {"content": "Ok."}}]}
_OLLAMA_OK = {"message": {"content": "Ok."}}


class TestReasoningEffort:
    """#406 — llm.reasoning_effort wird nur mitgeschickt, wenn es gesetzt ist."""

    def test_openai_chat_default_sends_no_parameter(self):
        llm = OpenAICompatibleLLM(base_url="http://localhost:11434/v1", model="m")

        with patch("hannah.llm.requests.post", return_value=_response(_OPENAI_OK)) as post:
            llm.chat("Hallo")

        assert "reasoning_effort" not in _sent_payload(post)

    def test_openai_chat_sends_parameter_when_set(self):
        llm = OpenAICompatibleLLM(base_url="http://localhost:11434/v1", model="m", reasoning_effort="none")

        with patch("hannah.llm.requests.post", return_value=_response(_OPENAI_OK)) as post:
            llm.chat("Hallo")

        assert _sent_payload(post)["reasoning_effort"] == "none"

    def test_openai_tools_default_sends_no_parameter(self):
        llm = OpenAICompatibleLLM(base_url="http://localhost:11434/v1", model="m")

        with patch("hannah.llm.requests.post", return_value=_response(_OPENAI_OK)) as post:
            llm.chat_with_tools([{"role": "user", "content": "x"}], [])

        assert "reasoning_effort" not in _sent_payload(post)

    def test_openai_tools_sends_parameter_when_set(self):
        llm = OpenAICompatibleLLM(base_url="http://localhost:11434/v1", model="m", reasoning_effort="low")

        with patch("hannah.llm.requests.post", return_value=_response(_OPENAI_OK)) as post:
            llm.chat_with_tools([{"role": "user", "content": "x"}], [])

        assert _sent_payload(post)["reasoning_effort"] == "low"

    def test_ollama_native_default_sends_no_think(self):
        llm = OllamaLLM(base_url="http://localhost:11434", model="m")

        with patch("hannah.llm.requests.post", return_value=_response(_OLLAMA_OK)) as post:
            llm.chat("Hallo")

        assert "think" not in _sent_payload(post)

    def test_ollama_native_none_turns_thinking_off(self):
        llm = OllamaLLM(base_url="http://localhost:11434", model="m", reasoning_effort="none")

        with patch("hannah.llm.requests.post", return_value=_response(_OLLAMA_OK)) as post:
            llm.chat("Hallo")

        assert _sent_payload(post)["think"] is False

    def test_ollama_native_other_value_is_ignored_with_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="hannah.llm"):
            llm = OllamaLLM(base_url="http://localhost:11434", model="m", reasoning_effort="high")

        with patch("hannah.llm.requests.post", return_value=_response(_OLLAMA_OK)) as post:
            llm.chat("Hallo")

        assert "think" not in _sent_payload(post)
        assert any("reasoning_effort=high" in r.getMessage() for r in caplog.records)

    def test_load_passes_option_to_openai_compat(self):
        llm = load({"enabled": True, "provider": "openai_compat", "base_url": "http://h:11434/v1",
                    "model": "m", "reasoning_effort": " none "})

        with patch("hannah.llm.requests.post", return_value=_response(_OPENAI_OK)) as post:
            llm.chat("Hallo")

        assert _sent_payload(post)["reasoning_effort"] == "none"

    def test_load_without_option_sends_nothing(self):
        llm = load({"enabled": True, "provider": "openai_compat", "base_url": "http://h:11434/v1", "model": "m"})

        with patch("hannah.llm.requests.post", return_value=_response(_OPENAI_OK)) as post:
            llm.chat("Hallo")

        assert "reasoning_effort" not in _sent_payload(post)

    def test_load_empty_option_sends_nothing(self):
        llm = load({"enabled": True, "provider": "openai_compat", "base_url": "http://h:11434/v1",
                    "model": "m", "reasoning_effort": None})

        with patch("hannah.llm.requests.post", return_value=_response(_OPENAI_OK)) as post:
            llm.chat("Hallo")

        assert "reasoning_effort" not in _sent_payload(post)

    def test_load_passes_option_to_native_ollama(self):
        llm = load({"enabled": True, "provider": "ollama", "base_url": "http://h:11434",
                    "model": "m", "reasoning_effort": "none"})

        with patch("hannah.llm.requests.post", return_value=_response(_OLLAMA_OK)) as post:
            llm.chat("Hallo")

        assert _sent_payload(post)["think"] is False


class TestSupportsTools:
    """#407 — nur der OpenAI-kompatible Client kann Function-Calling, der Rest delegiert an chat()."""

    def test_only_the_openai_compatible_client_supports_tools(self):
        assert OpenAICompatibleLLM(base_url="http://h:11434/v1", model="m").supports_tools is True
        assert OllamaLLM(base_url="http://h:11434", model="m").supports_tools is False
        assert DummyLLM().supports_tools is False


class TestToolCallFailure:
    def test_a_timeout_is_marked_as_error(self):
        llm = OpenAICompatibleLLM(base_url="http://h:11434/v1", model="m")

        with patch("hannah.llm.requests.post", side_effect=requests.exceptions.Timeout):
            result = llm.chat_with_tools([{"role": "user", "content": "x"}], [])

        assert result["error"] is True
        assert result["tool_calls"] == []

    def test_another_failure_is_marked_as_error(self):
        llm = OpenAICompatibleLLM(base_url="http://h:11434/v1", model="m")

        with patch("hannah.llm.requests.post", side_effect=requests.exceptions.ConnectionError):
            result = llm.chat_with_tools([{"role": "user", "content": "x"}], [])

        assert result["error"] is True

    def test_a_normal_answer_has_no_error_flag(self):
        llm = OpenAICompatibleLLM(base_url="http://h:11434/v1", model="m")

        with patch("hannah.llm.requests.post", return_value=_response(_OPENAI_OK)):
            result = llm.chat_with_tools([{"role": "user", "content": "x"}], [])

        assert "error" not in result
