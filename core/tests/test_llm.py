from hannah.llm import DummyLLM, LLMClient


class _StubLLM(LLMClient):
    """LLMClient-Subklasse mit fest verdrahteter chat()-Antwort — testet nur die
    Parsing-Logik von classify(), ohne einen echten LLM-Call zu brauchen."""

    def __init__(self, response: str):
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


class TestDummyLLMClassify:
    """Kein LLM konfiguriert → immer COMMAND, unabhängig von History (#159)."""

    def test_always_returns_command(self):
        llm = DummyLLM()

        assert llm.classify("irgendwas") == "COMMAND"

    def test_always_command_even_with_history(self):
        llm = DummyLLM()

        assert llm.classify("irgendwas", history=[{"role": "user", "content": "x"}]) == "COMMAND"
