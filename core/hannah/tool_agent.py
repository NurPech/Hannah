"""LLM Tool Agent — wertet komplexe Anfragen per Tool-Use-Loop aus.

Das NLU delegiert an den ToolAgent wenn kein Intent erkannt wurde.
Der Agent hat Zugriff auf Hannah-interne Daten (Gerätecache, Setter)
und gibt niemals direkt an ioBroker weiter — alles läuft über Hannah.
"""
from __future__ import annotations

import datetime
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .llm import LLMClient
    from .iobroker import IoBrokerClient
    from .user_manager import UserManager

log = logging.getLogger(__name__)

_MAX_ITERATIONS = 5

_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_all_devices",
            "description": (
                "Gibt alle bekannten Smart-Home-Geräte zurück (ID, Name, Raum, Kategorie). "
                "Nur zur Übersicht — keine Zustandswerte. "
                "Für aktive Geräte: get_active_devices. "
                "Für Geräte in einem Raum: get_devices_in_room. "
                "Für eine Kategorie: get_devices_by_category."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_active_devices",
            "description": (
                "Gibt alle Geräte zurück die gerade aktiv sind "
                "(on=true oder level>0), inklusive aktueller Zustandswerte. "
                "Ideal für Fragen wie 'Was läuft gerade?' oder 'Was ist eingeschaltet?'."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_devices_in_room",
            "description": (
                "Gibt alle Geräte in einem bestimmten Raum zurück "
                "(ID, Name, Kategorie, verfügbare State-Suffixe). "
                "Ideal wenn Fragen oder Befehle einen Raum betreffen."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "room": {
                        "type": "string",
                        "description": "Raumname, z.B. 'Wohnzimmer' oder 'Küche'",
                    }
                },
                "required": ["room"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_devices_by_category",
            "description": (
                "Gibt alle Geräte einer Kategorie zurück "
                "(ID, Name, Raum, verfügbare State-Suffixe). "
                "Ideal für Massen-Aktionen wie 'alle Lichter aus'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Kategoriename, z.B. 'Licht', 'Heizung', 'Steckdosen'",
                    }
                },
                "required": ["category"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_device_state",
            "description": "Gibt den aktuellen Zustand (alle State-Werte) eines Geräts zurück.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {
                        "type": "string",
                        "description": "Die Geräte-ID aus get_all_devices",
                    }
                },
                "required": ["device_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_device_state",
            "description": (
                "Setzt einen State eines Geräts. "
                "Die state_id ergibt sich aus device_id + '.' + State-Suffix "
                "(z.B. 'javascript.0.virtualDevice.Licht.EG.Wohnzimmer.Decke.on')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "state_id": {
                        "type": "string",
                        "description": "Vollständige State-ID (Device-ID + Punkt + State-Suffix)",
                    },
                    "value": {
                        "description": "Wert: true/false für on, 0–100 für level, Hex-String für color",
                    },
                },
                "required": ["state_id", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "speak",
            "description": (
                "Lässt Hannah einen Text sprechen (TTS). "
                "Nutze das für Rückmeldungen an den Nutzer — "
                "z.B. um zu berichten was getan wurde oder eine Frage zu stellen."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Der Text den Hannah sprechen soll",
                    }
                },
                "required": ["text"],
            },
        },
    },
]


class ToolAgent:
    """
    Führt einen Tool-Use-Loop gegen ein LLMClient-Backend durch.

    Hannah-interne Tools werden direkt dispatcht; das LLM kennt ioBroker nicht.
    """

    def __init__(
        self,
        llm: "LLMClient",
        iobroker: "IoBrokerClient",
        user_manager: "UserManager | None" = None,
        automations: dict[str, list[str]] | None = None,
        push_automation_update=None,  # (user_id: int, automation: str, enabled: bool) -> None
    ) -> None:
        self._llm = llm
        self._iobroker = iobroker
        self._user_manager = user_manager
        self._automations = automations or {}
        self._push_automation_update = push_automation_update or (lambda *_: None)
        self._tools = list(_TOOLS)
        if self._automations:
            self._tools.append(self._build_automation_tool())

    def _build_automation_tool(self) -> dict:
        """Baut das set_automation-Tool mit den tatsächlich bekannten Automation-Keys +
        Beispielphrasen aus der Settings-Kategorie "automations" — das LLM sieht so, welche
        Automationen es überhaupt gibt und wie sie umgangssprachlich heißen, im Gegensatz
        zur NLU, die nur exakte Wortlisten-Substrings matcht (kein Fallback aufeinander nötig,
        beide Pfade nutzen dieselbe Quelle)."""
        known = "; ".join(
            f"{key} (z.B. {', '.join(words)})" for key, words in self._automations.items()
        )
        return {
            "type": "function",
            "function": {
                "name": "set_automation",
                "description": (
                    "Aktiviert oder deaktiviert eine externe Automation (z.B. einen Auto-Responder) "
                    "für den aktuellen Nutzer. Bekannte Automationen: " + known
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "automation": {
                            "type": "string",
                            "description": "Interner Automation-Key aus der Beschreibung, z.B. 'telegram_autoresponder' — nicht die gesprochene Phrase selbst.",
                        },
                        "enabled": {
                            "type": "boolean",
                            "description": "true = aktivieren, false = deaktivieren",
                        },
                    },
                    "required": ["automation", "enabled"],
                },
            },
        }

    def run(
        self,
        text: str,
        system_prompt: str = "",
        history: list[dict] | None = None,
        user_id: str = "",
    ) -> str:
        """
        Startet den Tool-Loop für `text`.
        Gibt den endgültigen Antworttext zurück (wird vom Aufrufer per TTS gesprochen).
        """
        spoken: list[str] = []
        called: set[tuple[str, str]] = set()

        _now = datetime.datetime.now()
        _WEEKDAYS_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
        _TOOL_RULES = (
            f"\n\nAktuelles Datum/Uhrzeit: {_WEEKDAYS_DE[_now.weekday()]}, {_now.strftime('%d.%m.%Y')}, {_now.strftime('%H:%M')} Uhr"
            "\n\nRegeln für Tool-Nutzung:"
            "\n- Nutze das speak-Tool um deine Antwort auszugeben."
            "\n- Rufe nie dasselbe Tool zweimal hintereinander auf."
            "\n- Nach dem Sammeln aller nötigen Informationen: speak aufrufen und danach stoppen."
        )

        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt + _TOOL_RULES})
        else:
            messages.append({"role": "system", "content": _TOOL_RULES.lstrip()})
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": text})

        for i in range(_MAX_ITERATIONS):
            payload_chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
            log.info("[tool_agent] Iteration %d/%d — payload %d msgs / ~%d chars", i + 1, _MAX_ITERATIONS, len(messages), payload_chars)
            response = self._llm.chat_with_tools(messages, self._tools)
            log.info("[tool_agent] finish_reason=%s tool_calls=%d", response.get("finish_reason"), len(response.get("tool_calls") or []))
            tool_calls: list[dict] = response.get("tool_calls") or []

            if not tool_calls:
                final = response.get("content", "")
                return "\n".join(spoken) if spoken else final

            # Assistent-Nachricht mit tool_calls in History aufnehmen
            messages.append(
                {
                    "role": "assistant",
                    "content": response.get("content") or "",
                    "tool_calls": tool_calls,
                }
            )

            for call in tool_calls:
                call_id: str = call.get("id", "")
                func_name: str = call.get("function", {}).get("name", "")
                try:
                    args: dict = json.loads(call.get("function", {}).get("arguments", "{}"))
                except json.JSONDecodeError:
                    args = {}

                call_key = (func_name, call.get("function", {}).get("arguments", "{}"))
                if call_key in called:
                    result = "Dieses Tool wurde bereits mit denselben Argumenten aufgerufen. Nutze jetzt speak um zu antworten."
                    log.warning("[tool_agent] Duplikat-Aufruf blockiert: %s(%s)", func_name, args)
                else:
                    called.add(call_key)
                    result = self._dispatch(func_name, args, spoken, user_id)
                result_chars = len(result) if isinstance(result, str) else len(json.dumps(result, ensure_ascii=False))
                log.info("[tool_agent] %s(%s) → %d chars", func_name, args, result_chars)
                log.debug("[tool_agent] %s result: %s", func_name, result)

                if func_name != "speak":
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": result if isinstance(result, str) else json.dumps(result, ensure_ascii=False),
                        }
                    )

            # Terminal — after processing all tool calls in this batch, return if speak was used
            if spoken:
                return "\n".join(spoken)

        log.warning("[tool_agent] Max Iterationen (%d) erreicht ohne finale Antwort", _MAX_ITERATIONS)
        return "\n".join(spoken) if spoken else ""

    # ------------------------------------------------------------------
    # Tool-Dispatch

    def _dispatch(self, name: str, args: dict, spoken: list[str], user_id: str = "") -> object:
        if name == "get_all_devices":
            return self._get_all_devices()
        if name == "get_active_devices":
            return self._get_active_devices()
        if name == "get_devices_in_room":
            return self._get_devices_in_room(args.get("room", ""))
        if name == "get_devices_by_category":
            return self._get_devices_by_category(args.get("category", ""))
        if name == "get_device_state":
            return self._get_device_state(args.get("device_id", ""))
        if name == "set_device_state":
            return self._set_device_state(args.get("state_id", ""), args.get("value"))
        if name == "set_automation":
            return self._set_automation(args.get("automation", ""), bool(args.get("enabled", True)), user_id)
        if name == "speak":
            text = str(args.get("text", ""))
            spoken.append(text)
            return {"ok": True}
        return {"error": f"Unbekanntes Tool: {name}"}

    # ------------------------------------------------------------------
    # Tool-Implementierungen

    def _get_all_devices(self) -> str:
        lines: list[str] = []
        for room_key, devs in self._iobroker.devices.items():
            room_name = self._iobroker.rooms.get(room_key, room_key)
            for dev in devs.values():
                lines.append(f"- {room_name}: {dev.name} ({dev.category}) [ID: {dev.id}]")
        if not lines:
            return "Keine Geräte bekannt."
        return f"Bekannte Geräte ({len(lines)}):\n" + "\n".join(lines)

    def _get_active_devices(self) -> str:
        total = sum(len(devs) for devs in self._iobroker.devices.values())
        lines: list[str] = []
        for room_key, devs in self._iobroker.devices.items():
            room_name = self._iobroker.rooms.get(room_key, room_key)
            for dev in devs.values():
                if self._is_active(dev):
                    state = self._format_active_state(dev.current or {})
                    lines.append(f"- {room_name}: {dev.name} ({dev.category}) — {state} [ID: {dev.id}]")
        if not lines:
            return "Keine Geräte sind aktuell aktiv."
        return f"Aktive Geräte ({len(lines)} von {total}):\n" + "\n".join(lines)

    def _get_devices_in_room(self, room: str) -> str:
        room_lower = room.lower()
        lines: list[str] = []
        matched_room = room
        for room_key, devs in self._iobroker.devices.items():
            room_name = self._iobroker.rooms.get(room_key, room_key)
            if room_lower not in room_name.lower():
                continue
            matched_room = room_name
            for dev in devs.values():
                states = ", ".join(dev.states.keys())
                lines.append(f"- {dev.name} ({dev.category}) [ID: {dev.id}, States: {states}]")
        if not lines:
            return f"Kein Raum '{room}' gefunden."
        return f"Geräte im {matched_room} ({len(lines)}):\n" + "\n".join(lines)

    def _get_devices_by_category(self, category: str) -> str:
        category_lower = category.lower()
        lines: list[str] = []
        for room_key, devs in self._iobroker.devices.items():
            room_name = self._iobroker.rooms.get(room_key, room_key)
            for dev in devs.values():
                if category_lower not in dev.category.lower():
                    continue
                states = ", ".join(dev.states.keys())
                lines.append(f"- {room_name}: {dev.name} [ID: {dev.id}, States: {states}]")
        if not lines:
            return f"Keine Geräte in Kategorie '{category}' gefunden."
        return f"{category}-Geräte ({len(lines)}):\n" + "\n".join(lines)

    def _get_device_state(self, device_id: str) -> str:
        dev = self._iobroker._devices_by_id.get(device_id)
        if not dev:
            return f"Gerät '{device_id}' nicht gefunden."
        current = dev.current or {}
        state_str = ", ".join(f"{k}={v}" for k, v in current.items()) or "keine Zustandswerte"
        return f"Gerät: {dev.name} ({dev.room}, {dev.category})\nZustand: {state_str}"

    def _set_device_state(self, state_id: str, value: object) -> dict:
        ok = self._iobroker.set_state(state_id, value)
        return {"ok": ok}

    def _set_automation(self, automation: str, enabled: bool, user_id: str) -> dict:
        if not user_id:
            return {"error": "Kein Nutzer erkannt, kann Automation nicht umschalten."}
        if automation not in self._automations:
            return {"error": f"Unbekannte Automation: {automation!r}. Bekannt: {list(self._automations)}"}
        user = self._user_manager.get_user_by_id(user_id) if self._user_manager else None
        if not user:
            return {"error": "Nutzer nicht gefunden."}
        user.enable_automation(automation) if enabled else user.disable_automation(automation)
        self._push_automation_update(user.id, automation, enabled)
        return {"ok": True}

    @staticmethod
    def _is_active(dev) -> bool:
        current = dev.current or {}
        if "on" in current:
            return bool(current["on"])
        level = current.get("level")
        return bool(level and int(level) > 0)

    @staticmethod
    def _format_active_state(current: dict) -> str:
        parts = []
        if current.get("on"):
            parts.append("eingeschaltet")
        level = current.get("level")
        if level is not None and int(level) > 0:
            parts.append(f"{level}%")
        color = current.get("color")
        if color:
            parts.append(f"Farbe {color}")
        return ", ".join(parts) if parts else "aktiv"
