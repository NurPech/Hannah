import datetime
import json
from unittest.mock import MagicMock, patch

import pytest

from hannah_proto.v2 import hannah_pb2 as pb

from hannah.device_control import DeviceController
from hannah.llm import DummyLLM
from hannah.tool_agent import ToolAgent
from hannah.typed_devices import DeviceRegistry, Slot, TypedDevice

C = pb.DeviceClass
K = pb.SlotKind
ROOMS = {"wohnzimmer": "Wohnzimmer"}


def _make_iobroker() -> MagicMock:
    """Helper: IoBrokerClient-Mock (Geräte stehen seit #387 in der Registry)."""
    iobroker = MagicMock()
    iobroker.rooms = ROOMS
    return iobroker


def _slot(kind, value=None, *, writable=True, trust=None, state_id="", options=None) -> Slot:
    return Slot(slot_id=pb.SlotKind.Name(kind)[len("SLOT_KIND_"):].lower(), kind=kind, value=value, writable=writable,
                required_trust_level=trust, state_id=state_id, options=list(options or []))


def _typed(device_id, name, *slots, device_class=C.DEVICE_CLASS_LIGHT, room="wohnzimmer", origin="v2") -> TypedDevice:
    return TypedDevice(device_id=device_id, name=name, room=room, device_class=device_class,
                       slots={s.slot_id: s for s in slots}, origin=origin)


def _registry(*devices: TypedDevice) -> DeviceRegistry:
    registry = DeviceRegistry()
    registry.replace("v2", [d for d in devices if d.origin == "v2"])
    registry.replace("v1", [d for d in devices if d.origin == "v1"])
    return registry


def _llm_response(content: str = "", tool_calls: list | None = None) -> dict:
    return {
        "content": content,
        "tool_calls": tool_calls or [],
        "finish_reason": "tool_calls" if tool_calls else "stop",
    }


def _tool_call(name: str, args: dict, call_id: str = "call_1") -> dict:
    return {
        "id": call_id,
        "function": {"name": name, "arguments": json.dumps(args)},
    }


# ──────────────────────────────────────────────────────────────────────────────
# ToolAgent.run()


class TestToolAgentRun:
    def test_no_tool_calls_returns_content(self):
        llm = MagicMock()
        llm.chat_with_tools.return_value = _llm_response(content="Kein Problem.")
        agent = ToolAgent(llm, _make_iobroker())

        result = agent.run("test")

        assert result == "Kein Problem."

    def test_speak_tool_result_returned_over_final_content(self):
        llm = MagicMock()
        llm.chat_with_tools.side_effect = [
            _llm_response(tool_calls=[_tool_call("speak", {"text": "Licht ist aus."})]),
            _llm_response(content="Fertig."),
        ]
        agent = ToolAgent(llm, _make_iobroker())

        result = agent.run("Mach das Licht aus.")

        assert result == "Licht ist aus."

    def test_multiple_speak_calls_joined(self):
        llm = MagicMock()
        llm.chat_with_tools.side_effect = [
            _llm_response(
                tool_calls=[
                    _tool_call("speak", {"text": "Wohnzimmer aus."}, call_id="c1"),
                    _tool_call("speak", {"text": "Küche aus."}, call_id="c2"),
                ]
            ),
            _llm_response(content=""),
        ]
        agent = ToolAgent(llm, _make_iobroker())

        result = agent.run("Alles aus.")

        assert result == "Wohnzimmer aus.\nKüche aus."

    def test_final_content_used_when_no_speak(self):
        llm = MagicMock()
        llm.chat_with_tools.side_effect = [
            _llm_response(tool_calls=[_tool_call("get_all_devices", {})]),
            _llm_response(content="Es gibt keine Geräte."),
        ]
        agent = ToolAgent(llm, _make_iobroker())

        result = agent.run("Was gibt es?")

        assert result == "Es gibt keine Geräte."

    def test_max_iterations_returns_empty_without_speak(self):
        llm = MagicMock()
        llm.chat_with_tools.return_value = _llm_response(
            tool_calls=[_tool_call("get_all_devices", {})]
        )
        agent = ToolAgent(llm, _make_iobroker())

        result = agent.run("endlosschleife")

        assert result == ""

    def test_max_iterations_returns_spoken_parts(self):
        responses = [
            _llm_response(tool_calls=[_tool_call("speak", {"text": f"Teil {i}"})])
            for i in range(10)
        ]
        llm = MagicMock()
        llm.chat_with_tools.side_effect = responses
        agent = ToolAgent(llm, _make_iobroker())

        result = agent.run("endlosschleife")

        assert "Teil 0" in result

    def test_system_prompt_and_history_passed_to_llm(self):
        llm = MagicMock()
        llm.chat_with_tools.return_value = _llm_response(content="OK")
        agent = ToolAgent(llm, _make_iobroker())

        agent.run(
            "text",
            system_prompt="Du bist Hannah.",
            history=[{"role": "user", "content": "Hallo"}, {"role": "assistant", "content": "Hi"}],
        )

        messages = llm.chat_with_tools.call_args[0][0]
        assert messages[0]["role"] == "system"
        assert messages[0]["content"].startswith("Du bist Hannah.")
        assert messages[1]["content"] == "Hallo"
        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"].startswith("text")

    def test_tool_result_appended_to_messages(self):
        dev = _typed("dev-decke", "Decke", _slot(K.SLOT_KIND_ON, True))
        iobroker = _make_iobroker()
        llm = MagicMock()
        llm.chat_with_tools.side_effect = [
            _llm_response(tool_calls=[_tool_call("get_all_devices", {})]),
            _llm_response(content="Fertig."),
        ]
        agent = ToolAgent(llm, iobroker, registry=_registry(dev), room_names=lambda: ROOMS)

        agent.run("Zeig Geräte")

        second_call_messages = llm.chat_with_tools.call_args_list[1][0][0]
        tool_result_msg = next(m for m in second_call_messages if m.get("role") == "tool")
        content = tool_result_msg["content"]
        assert isinstance(content, str)
        assert dev.device_id in content


# ──────────────────────────────────────────────────────────────────────────────
# Tool dispatch


class TestToolDispatch:
    DEVICE_ID = "dev-decke"

    def setup_method(self):
        self.dev = _typed(self.DEVICE_ID, "Decke", _slot(K.SLOT_KIND_ON, False), _slot(K.SLOT_KIND_BRIGHTNESS, 40.0))
        self.registry = _registry(self.dev)
        self.iobroker = _make_iobroker()
        self.iobroker.rooms = ROOMS
        self.agent = ToolAgent(MagicMock(), self.iobroker, registry=self.registry, room_names=lambda: ROOMS)
        self.sent: list = []
        self.set_state = MagicMock(return_value=True)
        self.agent.set_device_control(DeviceController(
            self.registry,
            send_set_slot=lambda device_id, slot_id, value: self.sent.append((device_id, slot_id, value)) or True,
            send_set_state=self.set_state,
        ))

    def test_get_all_devices_structure(self):
        result = self.agent._get_all_devices()

        assert isinstance(result, str)
        assert self.DEVICE_ID in result
        assert "Decke" in result
        assert "Wohnzimmer" in result
        assert "Licht" in result

    def test_get_device_state_lists_the_slots_with_their_values(self):
        result = self.agent._get_device_state(self.DEVICE_ID)

        assert "Decke" in result
        assert "on=False" in result and "brightness=40.0" in result

    def test_get_device_state_marks_read_only_slots_and_lists_options(self):
        self.registry.replace("v2", [_typed(
            "klima", "Klima", _slot(K.SLOT_KIND_MODE, "cool", options=["cool", "dry"]),
            _slot(K.SLOT_KIND_TEMPERATURE, 24.5, writable=False), device_class=C.DEVICE_CLASS_CLIMATE,
        )])

        result = self.agent._get_device_state("klima")

        assert "mode=cool (Werte: cool, dry)" in result
        assert "temperature=24.5 (nur lesen)" in result

    def test_get_device_state_not_found(self):
        result = self.agent._get_device_state("nicht.vorhanden")

        assert isinstance(result, str)
        assert "nicht.vorhanden" in result

    def test_get_devices_in_room_lists_slots(self):
        result = self.agent._get_devices_in_room("wohn")

        assert "Geräte im Wohnzimmer (1)" in result
        assert f"[ID: {self.DEVICE_ID}, Slots: on=False, brightness=40.0]" in result

    def test_an_unknown_room_is_reported(self):
        assert self.agent._get_devices_in_room("Keller") == "Kein Raum 'Keller' gefunden."

    @pytest.mark.parametrize("category", ["Licht", "licht", "Lampen", "light", "Leuchte"])
    def test_a_light_is_found_by_german_and_english_category_words(self, category):
        assert self.DEVICE_ID in self.agent._get_devices_by_category(category)

    @pytest.mark.parametrize("category", ["Steckdosen", "Heizung", "Rollladen"])
    def test_other_categories_do_not_match_a_light(self, category):
        assert "Keine Geräte in Kategorie" in self.agent._get_devices_by_category(category)

    def test_the_active_devices_name_their_state(self):
        self.registry.set_slot_value(self.DEVICE_ID, "on", True)

        result = self.agent._get_active_devices()

        assert "Aktive Geräte (1 von 1)" in result
        assert "eingeschaltet, 40%" in result

    def test_set_device_state_sends_the_slot(self):
        result = self.agent._set_device_state(self.DEVICE_ID, "on", True, None, [])

        assert result == {"ok": True}
        assert [(d, s, v.boolean) for d, s, v in self.sent] == [(self.DEVICE_ID, "on", True)]

    def test_set_device_state_reports_an_unreachable_adapter(self):
        self.agent._controller._send_set_slot = lambda *_: False

        assert self.agent._set_device_state(self.DEVICE_ID, "on", True, None, []) == {"ok": False}

    def test_set_device_state_refuses_a_read_only_slot(self):
        self.registry.replace("v2", [_typed("m", "Messer", _slot(K.SLOT_KIND_POWER, 5.0, writable=False))])

        result = self.agent._set_device_state("m", "power", 10, None, [])

        assert result["ok"] is False and "nur lesbar" in result["error"]
        assert self.sent == []

    def test_set_device_state_names_the_known_slots_for_an_unknown_one(self):
        result = self.agent._set_device_state(self.DEVICE_ID, "farbe", "#FF0000", None, [])

        assert result["ok"] is False
        assert "on, brightness" in result["error"]

    def test_set_device_state_tells_the_llm_about_a_wrong_value(self):
        result = self.agent._set_device_state(self.DEVICE_ID, "brightness", "hell", None, [])

        assert result["ok"] is False and "BRIGHTNESS" in result["error"]
        assert self.sent == []

    def test_an_unknown_device_is_reported(self):
        result = self.agent._set_device_state("gibt-es-nicht", "on", True, None, [])

        assert result["ok"] is False and "nicht gefunden" in result["error"]

    def test_a_llm_that_still_names_the_state_id_reaches_a_v1_device(self):
        self.registry.replace("v1", [_typed(
            "x.Lampe", "Lampe", _slot(K.SLOT_KIND_ON, False, state_id="x.Lampe.on"), origin="v1",
        )])

        result = self.agent._dispatch("set_device_state", {"state_id": "x.Lampe.on", "value": True}, [], "", None)

        assert result == {"ok": True}
        self.set_state.assert_called_once_with("x.Lampe.on", True)

    def test_without_a_device_control_nothing_is_written(self):
        agent = ToolAgent(MagicMock(), self.iobroker, registry=self.registry)

        assert agent._set_device_state(self.DEVICE_ID, "on", True, None, [])["ok"] is False

    def test_speak_appends_to_spoken(self):
        spoken: list[str] = []

        result = self.agent._dispatch("speak", {"text": "Hallo!"}, spoken)

        assert result == {"ok": True}
        assert spoken == ["Hallo!"]

    def test_unknown_tool_returns_error(self):
        result = self.agent._dispatch("fly_to_moon", {}, [])

        assert "error" in result
        assert "fly_to_moon" in result["error"]

    def test_is_active_on_true(self):
        assert ToolAgent._is_active(_typed("a", "A", _slot(K.SLOT_KIND_ON, True), _slot(K.SLOT_KIND_BRIGHTNESS, 80.0))) is True

    def test_is_active_on_false_with_level(self):
        assert ToolAgent._is_active(_typed("a", "A", _slot(K.SLOT_KIND_ON, False), _slot(K.SLOT_KIND_BRIGHTNESS, 80.0))) is False

    def test_is_active_no_on_slot_level_positive(self):
        assert ToolAgent._is_active(_typed("a", "A", _slot(K.SLOT_KIND_BRIGHTNESS, 50.0))) is True

    def test_is_active_no_on_slot_level_zero(self):
        assert ToolAgent._is_active(_typed("a", "A", _slot(K.SLOT_KIND_BRIGHTNESS, 0.0))) is False

    def test_is_active_without_values(self):
        assert ToolAgent._is_active(_typed("a", "A")) is False


class TestSetAutomationDispatch:
    def setup_method(self):
        self.iobroker = _make_iobroker()
        self.user_manager = MagicMock()
        self.push = MagicMock()
        self.agent = ToolAgent(
            MagicMock(), self.iobroker,
            user_manager=self.user_manager,
            automations={"telegram_autoresponder": ["autoresponder"]},
            push_automation_update=self.push,
        )

    def test_enables_automation_and_pushes_update(self):
        user = MagicMock(id=3)
        self.user_manager.get_user_by_id.return_value = user

        result = self.agent._set_automation("telegram_autoresponder", True, "3")

        assert result == {"ok": True}
        user.enable_automation.assert_called_once_with("telegram_autoresponder")
        user.disable_automation.assert_not_called()
        self.push.assert_called_once_with(3, "telegram_autoresponder", True)

    def test_disables_automation_and_pushes_update(self):
        user = MagicMock(id=3)
        self.user_manager.get_user_by_id.return_value = user

        result = self.agent._set_automation("telegram_autoresponder", False, "3")

        assert result == {"ok": True}
        user.disable_automation.assert_called_once_with("telegram_autoresponder")
        self.push.assert_called_once_with(3, "telegram_autoresponder", False)

    def test_unknown_automation_returns_error(self):
        result = self.agent._set_automation("teams_autoresponder", True, "3")

        assert "error" in result
        self.user_manager.get_user_by_id.assert_not_called()

    def test_no_user_id_returns_error(self):
        result = self.agent._set_automation("telegram_autoresponder", True, "")

        assert "error" in result
        self.user_manager.get_user_by_id.assert_not_called()

    def test_unknown_user_returns_error(self):
        self.user_manager.get_user_by_id.return_value = None

        result = self.agent._set_automation("telegram_autoresponder", True, "99")

        assert "error" in result

    def test_tool_registered_only_when_automations_configured(self):
        without = ToolAgent(MagicMock(), self.iobroker)
        with_automations = self.agent

        assert not any(t["function"]["name"] == "set_automation" for t in without._tools)
        assert any(t["function"]["name"] == "set_automation" for t in with_automations._tools)


# ──────────────────────────────────────────────────────────────────────────────
# LLMClient default chat_with_tools


class TestDefaultChatWithTools:
    def test_dummy_llm_returns_no_tool_calls(self):
        llm = DummyLLM("Fallback.")

        result = llm.chat_with_tools(
            messages=[{"role": "user", "content": "test"}],
            tools=[],
        )

        assert result["tool_calls"] == []
        assert result["finish_reason"] == "stop"
        assert result["content"] == "Fallback."

    def test_default_extracts_last_user_message(self):
        llm = DummyLLM("antwort")

        result = llm.chat_with_tools(
            messages=[
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "erste frage"},
                {"role": "assistant", "content": "erste antwort"},
                {"role": "user", "content": "zweite frage"},
            ],
            tools=[],
        )

        assert result["content"] == "antwort"
        assert result["tool_calls"] == []


# ──────────────────────────────────────────────────────────────────────────────
# Trust-Level pro State (#366)


class TestToolAgentTrustLevel:
    DEVICE_ID = "haustuer"

    def _agent(self, llm=None) -> tuple[ToolAgent, list]:
        registry = _registry(_typed(self.DEVICE_ID, "Haustür", _slot(K.SLOT_KIND_ON, False, trust=8)))
        sent: list = []
        agent = ToolAgent(llm or MagicMock(), _make_iobroker(), registry=registry, room_names=lambda: ROOMS)
        agent.set_device_control(DeviceController(
            registry, send_set_slot=lambda *args: sent.append(args) or True, send_set_state=MagicMock(return_value=True),
        ))
        return agent, sent

    def test_denied_set_is_not_executed_and_spoken_directly(self):
        agent, sent = self._agent()
        spoken: list[str] = []

        result = agent._dispatch("set_device_state", {"device_id": self.DEVICE_ID, "slot_id": "on", "value": True}, spoken, "", 0)

        assert sent == []
        assert result["ok"] is False
        assert spoken == ["Das darfst du leider nicht steuern."]

    def test_allowed_set_is_executed(self):
        agent, sent = self._agent()
        spoken: list[str] = []

        result = agent._dispatch("set_device_state", {"device_id": self.DEVICE_ID, "slot_id": "on", "value": True}, spoken, "", 8)

        assert len(sent) == 1
        assert result == {"ok": True}
        assert spoken == []

    def test_run_ends_with_denial_even_if_llm_never_speaks(self):
        llm = MagicMock()
        llm.chat_with_tools.return_value = _llm_response(
            tool_calls=[_tool_call("set_device_state", {"device_id": self.DEVICE_ID, "slot_id": "on", "value": True})]
        )
        agent, sent = self._agent(llm)

        assert agent.run("schließ die Haustür auf", trust_level=0) == "Das darfst du leider nicht steuern."
        assert sent == []

    def test_default_trust_level_is_guest(self):
        llm = MagicMock()
        llm.chat_with_tools.return_value = _llm_response(
            tool_calls=[_tool_call("set_device_state", {"device_id": self.DEVICE_ID, "slot_id": "on", "value": True})]
        )
        agent, sent = self._agent(llm)

        agent.run("schließ die Haustür auf")

        assert sent == []


# ──────────────────────────────────────────────────────────────────────────────
# ToolAgent.classify() (#407)


class TestToolAgentClassify:
    @staticmethod
    def _agent(content="SMALLTALK", tool_calls=None, error=False):
        llm = MagicMock()
        llm.supports_tools = True
        response = _llm_response(content=content, tool_calls=tool_calls)
        if error:
            response["error"] = True
        llm.chat_with_tools.return_value = response
        return llm, ToolAgent(llm, _make_iobroker())

    def test_shares_the_prompt_start_with_run(self):
        llm, agent = self._agent()
        history = [{"role": "user", "content": "Hallo"}, {"role": "assistant", "content": "Hi"}]

        with patch("hannah.tool_agent.datetime") as fake:
            fake.datetime.now.return_value = datetime.datetime(2026, 10, 4, 1, 30)
            agent.run("Was läuft?", system_prompt="Du bist Hannah.", history=history)
            agent.classify("Was läuft?", system_prompt="Du bist Hannah.", history=history)

        run_messages, run_tools = llm.chat_with_tools.call_args_list[0][0]
        classify_messages, classify_tools = llm.chat_with_tools.call_args_list[1][0]
        assert classify_messages[:-1] == run_messages
        assert classify_tools == run_tools
        assert classify_messages[-1]["role"] == "user"
        assert "COMMAND" in classify_messages[-1]["content"]
        assert "NOT_ADDRESSED" in classify_messages[-1]["content"]

    @pytest.mark.parametrize("content,expected", [
        ("COMMAND", "COMMAND"),
        ("SMALLTALK", "SMALLTALK"),
        ("NOT_ADDRESSED", "NOT_ADDRESSED"),
        (" not_addressed\n", "NOT_ADDRESSED"),
        ("Das weiß ich nicht.", "SMALLTALK"),
    ])
    def test_the_verdict_comes_from_the_answer(self, content, expected):
        _, agent = self._agent(content=content)

        assert agent.classify("Text") == expected

    def test_an_empty_answer_means_command(self):
        _, agent = self._agent(content="")

        assert agent.classify("Text") == "COMMAND"

    def test_a_tool_call_means_command(self):
        _, agent = self._agent(content="", tool_calls=[_tool_call("get_active_devices", {})])

        assert agent.classify("Text") == "COMMAND"

    def test_a_failed_request_means_command(self):
        # chat_with_tools() liefert bei einem Fehlschlag den Fallback-Text; der darf nicht als
        # SMALLTALK-Antwort durchgehen, sonst läuft der Befehl in einen ebenso scheiternden zweiten Aufruf.
        _, agent = self._agent(content="Das kann ich leider nicht beantworten.", error=True)

        assert agent.classify("Text") == "COMMAND"

    def test_an_llm_without_function_calling_uses_the_old_classifier(self):
        llm = MagicMock()
        llm.supports_tools = False
        llm.classify.return_value = "NOT_ADDRESSED"
        agent = ToolAgent(llm, _make_iobroker())
        history = [{"role": "user", "content": "Hallo"}]

        assert agent.classify("Text", system_prompt="Du bist Hannah.", history=history) == "NOT_ADDRESSED"
        llm.classify.assert_called_once_with("Text", history=history)
        llm.chat_with_tools.assert_not_called()


class TestToolAgentPromptStart:
    """#407 — der Anfang des Prompts bleibt von Minute zu Minute gleich, sonst nützt Ollama der Cache nichts."""

    def test_the_date_is_not_part_of_the_system_prompt(self):
        llm = MagicMock()
        llm.chat_with_tools.return_value = _llm_response(content="OK")
        agent = ToolAgent(llm, _make_iobroker())

        with patch("hannah.tool_agent.datetime") as fake:
            fake.datetime.now.return_value = datetime.datetime(2026, 10, 4, 1, 30)  # ein Sonntag
            agent.run("Wie spät ist es?", system_prompt="Du bist Hannah.")

        messages = llm.chat_with_tools.call_args[0][0]
        assert "Datum" not in messages[0]["content"]
        assert messages[-1]["content"].startswith("Wie spät ist es?")
        assert "Sonntag, 04.10.2026, 01:30 Uhr" in messages[-1]["content"]

    def test_everything_but_the_last_message_is_the_same_a_minute_later(self):
        llm = MagicMock()
        llm.chat_with_tools.return_value = _llm_response(content="OK")
        agent = ToolAgent(llm, _make_iobroker())
        history = [{"role": "user", "content": "Hallo"}, {"role": "assistant", "content": "Hi"}]

        with patch("hannah.tool_agent.datetime") as fake:
            fake.datetime.now.return_value = datetime.datetime(2026, 10, 4, 1, 30)
            agent.run("Frage", system_prompt="Du bist Hannah.", history=history)
            fake.datetime.now.return_value = datetime.datetime(2026, 10, 4, 1, 31)
            agent.run("Frage", system_prompt="Du bist Hannah.", history=history)

        first, second = (c[0][0] for c in llm.chat_with_tools.call_args_list)
        assert first[:-1] == second[:-1]
        assert first[-1] != second[-1]


class TestSpeakWrittenAsText:
    """#407 — gemma4 schreibt ohne Denken den speak-Aufruf teils als Text statt ihn aufzurufen."""

    @pytest.mark.parametrize("content,expected", [
        ('speak("Hallo Leonie.")', "Hallo Leonie."),
        ("speak('Es ist zwölf Uhr.')", "Es ist zwölf Uhr."),
        ('speak(text="Das Licht ist an.")', "Das Licht ist an."),
        ('  speak( text = "Mit Leerzeichen." )\n', "Mit Leerzeichen."),
        ('speak("Sie sagte: \\"Hallo\\".")', 'Sie sagte: \\"Hallo\\".'),
        ('speak("Zwei Sätze. Der zweite auch.")', "Zwei Sätze. Der zweite auch."),
    ])
    def test_the_text_is_taken_out_of_the_call(self, content, expected):
        llm = MagicMock()
        llm.chat_with_tools.return_value = _llm_response(content=content)

        assert ToolAgent(llm, _make_iobroker()).run("Hi") == expected

    @pytest.mark.parametrize("content", [
        "Ich kann speak nicht benutzen.",
        'Er sagte speak("so") und ging.',
        "speak ist ein Tool.",
        "",
    ])
    def test_other_answers_stay_as_they_are(self, content):
        llm = MagicMock()
        llm.chat_with_tools.return_value = _llm_response(content=content)

        assert ToolAgent(llm, _make_iobroker()).run("Hi") == content
