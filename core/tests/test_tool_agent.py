import json
from unittest.mock import MagicMock, patch

import pytest

from hannah.iobroker import Device
from hannah.llm import DummyLLM
from hannah.tool_agent import ToolAgent


def _make_iobroker(devices: list[Device] | None = None) -> MagicMock:
    """Helper: IoBrokerClient-Mock mit optionaler Device-Liste."""
    iobroker = MagicMock()
    devs = devices or []
    iobroker.rooms = {d.room: d.room_display_name for d in devs}
    by_key = {d.key: d for d in devs}
    iobroker.devices = {d.room: by_key for d in devs}
    iobroker._devices_by_id = {d.id: d for d in devs}
    return iobroker


def _make_device(
    device_id: str = "javascript.0.virtualDevice.Licht.EG.Wohnzimmer.Decke",
    name: str = "Decke",
    room: str = "wohnzimmer",
    room_display_name: str = "Wohnzimmer",
    floor: str = "EG",
    category: str = "Licht",
    states: dict | None = None,
    current: dict | None = None,
) -> Device:
    return Device(
        id=device_id,
        name=name,
        key=name.lower(),
        room=room,
        room_display_name=room_display_name,
        floor=floor,
        category=category,
        states=states if states is not None else {"on": f"{device_id}.on"},
        current=current if current is not None else {"on": True},
    )


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
        assert messages[-1] == {"role": "user", "content": "text"}

    def test_tool_result_appended_to_messages(self):
        dev = _make_device()
        iobroker = _make_iobroker([dev])
        llm = MagicMock()
        llm.chat_with_tools.side_effect = [
            _llm_response(tool_calls=[_tool_call("get_all_devices", {})]),
            _llm_response(content="Fertig."),
        ]
        agent = ToolAgent(llm, iobroker)

        agent.run("Zeig Geräte")

        second_call_messages = llm.chat_with_tools.call_args_list[1][0][0]
        tool_result_msg = next(m for m in second_call_messages if m.get("role") == "tool")
        content = tool_result_msg["content"]
        assert isinstance(content, str)
        assert dev.id in content


# ──────────────────────────────────────────────────────────────────────────────
# Tool dispatch


class TestToolDispatch:
    def setup_method(self):
        self.dev = _make_device(
            device_id="javascript.0.virtualDevice.Licht.EG.Wohnzimmer.Decke",
            name="Decke",
            room="Wohnzimmer",
            states={"on": "javascript.0.virtualDevice.Licht.EG.Wohnzimmer.Decke.on"},
            current={"on": False},
        )
        self.iobroker = _make_iobroker([self.dev])
        self.agent = ToolAgent(MagicMock(), self.iobroker)

    def test_get_all_devices_structure(self):
        result = self.agent._get_all_devices()

        assert isinstance(result, str)
        assert self.dev.id in result
        assert "Decke" in result
        assert "Wohnzimmer" in result
        assert "Licht" in result

    def test_get_device_state_found(self):
        result = self.agent._get_device_state(self.dev.id)

        assert isinstance(result, str)
        assert "Decke" in result
        assert "on" in result

    def test_get_device_state_not_found(self):
        result = self.agent._get_device_state("nicht.vorhanden")

        assert isinstance(result, str)
        assert "nicht.vorhanden" in result

    def test_set_device_state_calls_setter(self):
        self.iobroker.set_state.return_value = True

        result = self.agent._set_device_state(
            "javascript.0.virtualDevice.Licht.EG.Wohnzimmer.Decke.on", True, None, []
        )

        self.iobroker.set_state.assert_called_once_with(
            "javascript.0.virtualDevice.Licht.EG.Wohnzimmer.Decke.on", True
        )
        assert result == {"ok": True}

    def test_set_device_state_returns_setter_result(self):
        self.iobroker.set_state.return_value = False

        result = self.agent._set_device_state("some.state", 42, None, [])

        assert result == {"ok": False}

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
        dev = _make_device(current={"on": True, "level": 80})
        assert ToolAgent._is_active(dev) is True

    def test_is_active_on_false_with_level(self):
        dev = _make_device(current={"on": False, "level": 80})
        assert ToolAgent._is_active(dev) is False

    def test_is_active_no_on_state_level_positive(self):
        dev = _make_device(current={"level": 50})
        assert ToolAgent._is_active(dev) is True

    def test_is_active_no_on_state_level_zero(self):
        dev = _make_device(current={"level": 0})
        assert ToolAgent._is_active(dev) is False

    def test_is_active_empty_current(self):
        dev = _make_device(current={})
        assert ToolAgent._is_active(dev) is False


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
    STATE = "javascript.0.virtualDevice.Schloss.EG.Flur.Haustuer.on"

    def _agent(self, allowed: bool) -> tuple[ToolAgent, MagicMock]:
        iobroker = _make_iobroker()
        iobroker.may_set.return_value = allowed
        iobroker.set_state.return_value = True
        return ToolAgent(MagicMock(), iobroker), iobroker

    def test_denied_set_is_not_executed_and_spoken_directly(self):
        agent, iobroker = self._agent(allowed=False)
        spoken: list[str] = []

        result = agent._dispatch("set_device_state", {"state_id": self.STATE, "value": True}, spoken, "", 0)

        iobroker.may_set.assert_called_once_with(self.STATE, 0)
        iobroker.set_state.assert_not_called()
        assert result["ok"] is False
        assert spoken == ["Das darfst du leider nicht steuern."]

    def test_allowed_set_is_executed(self):
        agent, iobroker = self._agent(allowed=True)
        spoken: list[str] = []

        result = agent._dispatch("set_device_state", {"state_id": self.STATE, "value": True}, spoken, "", 8)

        iobroker.set_state.assert_called_once_with(self.STATE, True)
        assert result == {"ok": True}
        assert spoken == []

    def test_run_ends_with_denial_even_if_llm_never_speaks(self):
        llm = MagicMock()
        llm.chat_with_tools.return_value = _llm_response(
            tool_calls=[_tool_call("set_device_state", {"state_id": self.STATE, "value": True})]
        )
        iobroker = _make_iobroker()
        iobroker.may_set.return_value = False
        agent = ToolAgent(llm, iobroker)

        assert agent.run("schließ die Haustür auf", trust_level=0) == "Das darfst du leider nicht steuern."
        iobroker.set_state.assert_not_called()

    def test_default_trust_level_is_guest(self):
        llm = MagicMock()
        llm.chat_with_tools.return_value = _llm_response(
            tool_calls=[_tool_call("set_device_state", {"state_id": self.STATE, "value": True})]
        )
        iobroker = _make_iobroker()
        iobroker.may_set.return_value = False
        ToolAgent(llm, iobroker).run("schließ die Haustür auf")

        iobroker.may_set.assert_called_once_with(self.STATE, 0)
