"""LLM Tool Agent — wertet komplexe Anfragen per Tool-Use-Loop aus.

Das NLU delegiert an den ToolAgent wenn kein Intent erkannt wurde.
Der Agent hat Zugriff auf Hannah-interne Daten (Gerätecache, Setter)
und gibt niemals direkt an ioBroker weiter — alles läuft über Hannah.
"""
from __future__ import annotations

import datetime
import json
import logging
import re
from typing import TYPE_CHECKING

from hannah_proto.v2 import hannah_pb2 as pb

from hannah.iobroker import GUEST_TRUST_LEVEL, TRUST_DENIED_TEXT, TrustLevelDenied
from hannah.llm import CLASSIFY_PROMPT, parse_classification
from hannah.log_shipping import TRANSCRIPT
from hannah.nlu_devices import categories_of
from hannah.typed_devices import DeviceRegistry, Slot, TypedDevice

if TYPE_CHECKING:
    from .llm import LLMClient
    from .iobroker import IoBrokerClient
    from .user_manager import UserManager

log = logging.getLogger(__name__)

_MAX_ITERATIONS = 5

# Manche Modelle (gemma4 ohne Denken) schreiben den speak-Aufruf als Text, z.B. speak("Hallo.")
# oder speak(text="Hallo."), statt das Tool aufzurufen.
_TEXT_SPEAK = re.compile(r"""^\s*speak\s*\(\s*(?:text\s*=\s*)?(["'])(.*)\1\s*\)\s*$""", re.DOTALL)


def _unwrap_text_speak(content: str) -> str:
    """Gibt den Text aus einem als Text geschriebenen speak-Aufruf zurück. Ohne das würde Hannah
    den Aufruf samt Klammern und Anführungszeichen vorlesen."""
    match = _TEXT_SPEAK.match(content or "")
    return match.group(2).strip() if match else content


# Letzte Nachricht des Classifier-Aufrufs (#407). Dieselben Kriterien wie CLASSIFY_PROMPT, nur
# als Anweisung hinter dem Prompt-Anfang des Agents statt als eigener System-Prompt.
_CLASSIFY_INSTRUCTION = "[Anweisung an dich, keine Äußerung des Nutzers] Ruf KEIN Tool auf.\n" + CLASSIFY_PROMPT

C = pb.DeviceClass
K = pb.SlotKind

# Was das LLM als "Kategorie" lesen soll (#387): die Klasse des Geräts in Alltagssprache
_CLASS_NAMES = {
    C.DEVICE_CLASS_LIGHT: "Licht", C.DEVICE_CLASS_SOCKET: "Steckdose", C.DEVICE_CLASS_GENERIC_BINARY_SWITCH: "Schalter",
    C.DEVICE_CLASS_THERMOSTAT: "Thermostat", C.DEVICE_CLASS_COVER: "Rollladen", C.DEVICE_CLASS_SENSOR: "Sensor",
    C.DEVICE_CLASS_CONTACT: "Kontakt", C.DEVICE_CLASS_CLIMATE: "Klimaanlage", C.DEVICE_CLASS_GENERIC: "Sonstiges",
}

# Wörter, unter denen das LLM eine Kategorie nennen darf (deutsch oder englisch), je Kategorie-Code
_CATEGORY_ALIASES = {
    "light": ("licht", "lampe", "leuchte", "light"),
    "socket": ("steckdose", "stecker", "socket"),
    "blind": ("rollladen", "rollo", "jalousie", "markise", "blind", "cover"),
    "climate": ("klima", "climate"),
    "thermostat": ("heizung", "thermostat"),
    "window": ("fenster", "window"),
    "door": ("tür", "tuer", "door"),
    "temperature_sensor": ("temperatur", "temperature"),
    "humidity_sensor": ("feuchte", "humidity"),
    "illuminance_sensor": ("helligkeit", "illuminance", "lux"),
    "air_quality_sensor": ("luftqualität", "luftqualitaet", "luft", "air"),
}

_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_all_devices",
            "description": (
                "Gibt alle bekannten Smart-Home-Geräte zurück (ID, Name, Raum, Art). "
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
                "(ID, Name, Art, Slots mit Werten). "
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
                "(ID, Name, Raum, Slots mit Werten). "
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
            "description": "Gibt den aktuellen Zustand (alle Slots mit Werten) eines Geräts zurück.",
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
                "Setzt einen Slot eines Geräts. Geräte und ihre Slots (z.B. 'on', 'brightness', 'color', "
                "'color_temperature', 'target_temperature', 'position', 'mode', 'fan_speed') stehen in den "
                "get_*-Tools. Nur Slots, die nicht 'nur lesen' sind, lassen sich setzen."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {
                        "type": "string",
                        "description": "Die Geräte-ID aus get_all_devices",
                    },
                    "slot_id": {
                        "type": "string",
                        "description": "Der Slot des Geräts, z.B. 'on' oder 'brightness'",
                    },
                    "value": {
                        "description": (
                            "Wert: true/false für on, 0–100 für brightness und position (100 = offen), "
                            "Hex-String für color, Kelvin für color_temperature, Grad für target_temperature, "
                            "Text aus den angegebenen Werten für mode und fan_speed"
                        ),
                    },
                },
                "required": ["device_id", "slot_id", "value"],
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
        registry: "DeviceRegistry | None" = None,
        room_names=None,  # () -> {room_id: Anzeigename}
    ) -> None:
        self._llm = llm
        self._iobroker = iobroker
        # Geräte liest und schreibt der Agent über die typisierte Registry (#387). Der Controller
        # kommt nachträglich (set_device_control), er entsteht in main.py erst später.
        self._registry = registry if registry is not None else DeviceRegistry()
        self._controller = None
        self._room_names = room_names or (lambda: getattr(iobroker, "rooms", {}))
        self._user_manager = user_manager
        self._automations = automations or {}
        self._push_automation_update = push_automation_update or (lambda *_: None)
        self._tools = list(_TOOLS)
        if self._automations:
            self._tools.append(self._build_automation_tool())

    def set_device_control(self, controller) -> None:
        """Der DeviceController (hannah.device_control), über den set_device_state schreibt."""
        self._controller = controller

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

    @staticmethod
    def _messages(text: str, system_prompt: str, history: list[dict] | None) -> list[dict]:
        """Die Nachrichten eines Agent-Aufrufs: System-Prompt plus Tool-Regeln, Historie, Nutzertext.
        run() und classify() bauen sie gleich auf, damit Ollama den Anfang des Prompts aus dem
        Cache nimmt (#407). Datum und Uhrzeit hängen deshalb am Ende der letzten Nachricht und
        nicht im System-Prompt: Ein Text, der sich jede Minute ändert, würde sonst alles dahinter
        (Tools, Historie) aus dem Cache werfen."""
        _now = datetime.datetime.now()
        _WEEKDAYS_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
        _TOOL_RULES = (
            "\n\nRegeln für Tool-Nutzung:"
            "\n- Nutze das speak-Tool um deine Antwort auszugeben."
            "\n- Rufe nie dasselbe Tool zweimal hintereinander auf."
            "\n- Nach dem Sammeln aller nötigen Informationen: speak aufrufen und danach stoppen."
        )
        _NOW_NOTE = (
            f"\n\n[Aktuelles Datum/Uhrzeit: {_WEEKDAYS_DE[_now.weekday()]}, {_now.strftime('%d.%m.%Y')}, "
            f"{_now.strftime('%H:%M')} Uhr]"
        )

        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt + _TOOL_RULES})
        else:
            messages.append({"role": "system", "content": _TOOL_RULES.lstrip()})
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": text + _NOW_NOTE})
        return messages

    def classify(self, text: str, system_prompt: str = "", history: list[dict] | None = None) -> str:
        """Wie LLMClient.classify ("COMMAND" / "SMALLTALK" / "NOT_ADDRESSED"), aber mit demselben
        Prompt-Anfang wie run() (#407): gleiche System-Nachricht, gleiche Tools, gleiche Historie;
        die Klassifizier-Anweisung hängt als letzte Nachricht dahinter. Ollama merkt sich nur den
        Anfang des zuletzt gelesenen Prompts, ein eigener Classifier-Prompt würde ihn bei jedem
        Wechsel zwischen Classifier und Agent wegwerfen und den großen Agent-Prompt jedes Mal
        neu einlesen lassen. Ein LLM ohne Function-Calling nimmt den alten Classifier-Weg."""
        if not self._llm.supports_tools:
            return self._llm.classify(text, history=history)
        messages = self._messages(text, system_prompt, history)
        messages.append({"role": "user", "content": _CLASSIFY_INSTRUCTION})
        response = self._llm.chat_with_tools(messages, self._tools)
        if response.get("error"):
            return parse_classification(None)
        if response.get("tool_calls"):
            return "COMMAND"  # wollte ein Tool nutzen, also geht es um Geräte
        return parse_classification(response.get("content"))

    def run(
        self,
        text: str,
        system_prompt: str = "",
        history: list[dict] | None = None,
        user_id: str = "",
        trust_level: int | None = GUEST_TRUST_LEVEL,
    ) -> str:
        """
        Startet den Tool-Loop für `text`.
        Gibt den endgültigen Antworttext zurück (wird vom Aufrufer per TTS gesprochen).
        trust_level: Trust-Level des anfragenden Users für set_device_state (#366), siehe
          IoBrokerClient.may_set(). Default Gast, damit ein Aufrufer ohne Angabe nicht
          versehentlich alles darf.
        """
        spoken: list[str] = []
        called: set[tuple[str, str]] = set()

        messages = self._messages(text, system_prompt, history)

        for i in range(_MAX_ITERATIONS):
            payload_chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
            log.info("[tool_agent] Iteration %d/%d — payload %d msgs / ~%d chars", i + 1, _MAX_ITERATIONS, len(messages), payload_chars)
            response = self._llm.chat_with_tools(messages, self._tools)
            log.info("[tool_agent] finish_reason=%s tool_calls=%d", response.get("finish_reason"), len(response.get("tool_calls") or []))
            tool_calls: list[dict] = response.get("tool_calls") or []

            if not tool_calls:
                final = _unwrap_text_speak(response.get("content", ""))
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
                    log.warning("[tool_agent] Duplikat-Aufruf blockiert: %s(%s)", func_name, args, extra=TRANSCRIPT)
                else:
                    called.add(call_key)
                    result = self._dispatch(func_name, args, spoken, user_id, trust_level)
                result_chars = len(result) if isinstance(result, str) else len(json.dumps(result, ensure_ascii=False))
                log.info("[tool_agent] %s → %d chars", func_name, result_chars)
                # Die Argumente tragen Nutzertext, z.B. den gesprochenen Text bei speak.
                log.info("[tool_agent] %s(%s)", func_name, args, extra=TRANSCRIPT)
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

    def _dispatch(
        self, name: str, args: dict, spoken: list[str], user_id: str = "",
        trust_level: int | None = GUEST_TRUST_LEVEL,
    ) -> object:
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
            return self._set_device_state(
                args.get("device_id", ""), args.get("slot_id", ""), args.get("value"), trust_level, spoken,
                state_id=args.get("state_id", ""),
            )
        if name == "set_automation":
            return self._set_automation(args.get("automation", ""), bool(args.get("enabled", True)), user_id)
        if name == "speak":
            text = str(args.get("text", ""))
            spoken.append(text)
            return {"ok": True}
        return {"error": f"Unbekanntes Tool: {name}"}

    # ------------------------------------------------------------------
    # Tool-Implementierungen

    def _room_name(self, room_id: str) -> str:
        return self._room_names().get(room_id, room_id)

    @staticmethod
    def _class_name(device: TypedDevice) -> str:
        return _CLASS_NAMES.get(device.device_class, "Gerät")

    @staticmethod
    def _value_text(slot: Slot) -> str:
        value = slot.value
        if value is None:
            return "unbekannt"
        if slot.kind == K.SLOT_KIND_COLOR and isinstance(value, int) and not isinstance(value, bool):
            return f"#{value:06X}"
        return str(value)

    def _slot_text(self, slot: Slot) -> str:
        text = f"{slot.slot_id}={self._value_text(slot)}"
        if not slot.writable:
            text += " (nur lesen)"
        if slot.options:
            text += f" (Werte: {', '.join(slot.options)})"
        return text

    def _slots_text(self, device: TypedDevice) -> str:
        return ", ".join(self._slot_text(s) for s in device.slots.values()) or "keine Slots"

    def _get_all_devices(self) -> str:
        lines = [
            f"- {self._room_name(d.room)}: {d.name} ({self._class_name(d)}) [ID: {d.device_id}]"
            for d in self._registry.devices()
        ]
        if not lines:
            return "Keine Geräte bekannt."
        return f"Bekannte Geräte ({len(lines)}):\n" + "\n".join(lines)

    def _get_active_devices(self) -> str:
        devices = self._registry.devices()
        lines = [
            f"- {self._room_name(d.room)}: {d.name} ({self._class_name(d)}) — {self._format_active_state(d)} [ID: {d.device_id}]"
            for d in devices if self._is_active(d)
        ]
        if not lines:
            return "Keine Geräte sind aktuell aktiv."
        return f"Aktive Geräte ({len(lines)} von {len(devices)}):\n" + "\n".join(lines)

    def _get_devices_in_room(self, room: str) -> str:
        room_lower = room.lower()
        lines: list[str] = []
        matched_room = room
        for room_id in sorted({d.room for d in self._registry.devices()}):
            room_name = self._room_name(room_id)
            if room_lower not in room_name.lower():
                continue
            matched_room = room_name
            for dev in self._registry.devices_in_room(room_id):
                lines.append(f"- {dev.name} ({self._class_name(dev)}) [ID: {dev.device_id}, Slots: {self._slots_text(dev)}]")
        if not lines:
            return f"Kein Raum '{room}' gefunden."
        return f"Geräte im {matched_room} ({len(lines)}):\n" + "\n".join(lines)

    @staticmethod
    def _matches_category(device: TypedDevice, wanted: str) -> bool:
        """Ob eine vom LLM genannte Kategorie (deutsch oder englisch, Teilwort genügt) zum Gerät passt."""
        words = {_CLASS_NAMES.get(device.device_class, "").lower()}
        for code in categories_of(device):
            words.add(code)
            words.update(_CATEGORY_ALIASES.get(code, ()))
        return any(w and (w in wanted or wanted in w) for w in words)

    def _get_devices_by_category(self, category: str) -> str:
        wanted = category.lower().strip()
        lines = [
            f"- {self._room_name(d.room)}: {d.name} [ID: {d.device_id}, Slots: {self._slots_text(d)}]"
            for d in self._registry.devices() if wanted and self._matches_category(d, wanted)
        ]
        if not lines:
            return f"Keine Geräte in Kategorie '{category}' gefunden."
        return f"{category}-Geräte ({len(lines)}):\n" + "\n".join(lines)

    def _get_device_state(self, device_id: str) -> str:
        dev = self._registry.get(device_id)
        if not dev:
            return f"Gerät '{device_id}' nicht gefunden."
        return (
            f"Gerät: {dev.name} ({self._room_name(dev.room)}, {self._class_name(dev)})\n"
            f"Zustand: {self._slots_text(dev)}"
        )

    def _set_device_state(
        self, device_id: str, slot_id: str, value: object, trust_level: int | None, spoken: list[str],
        state_id: str = "",
    ) -> dict:
        if self._controller is None:
            return {"ok": False, "error": "Die Gerätesteuerung ist nicht verfügbar."}
        if not device_id and state_id:
            # Ein LLM, das noch die ioBroker-State-ID nennt (frühere Fassung des Tools): auf Gerät und
            # Slot der Registry abbilden, geht nur für Geräte eines v1-Adapters.
            target = self._registry.lookup_state(state_id)
            if target is not None:
                device_id, slot_id = target
        device = self._registry.get(device_id)
        if device is None:
            return {"ok": False, "error": f"Gerät '{device_id}' nicht gefunden."}
        slot = device.slots.get(slot_id)
        if slot is None:
            return {"ok": False, "error": f"{device.name} hat keinen Slot '{slot_id}'. Bekannt: {', '.join(device.slots)}."}
        if not slot.writable:
            return {"ok": False, "error": f"Der Slot '{slot_id}' von {device.name} ist nur lesbar."}
        try:
            result = self._controller.set_slot(device_id, slot_id, value, trust_level=trust_level)
        except TrustLevelDenied:
            # #366: Absage direkt in die gesprochene Antwort — das LLM kann sie so weder
            # übergehen noch einen Erfolg erfinden; run() endet nach diesem Batch.
            log.info("[tool_agent] set_device_state(%s/%s) abgelehnt: Trust-Level %s reicht nicht", device_id, slot_id, trust_level)
            if TRUST_DENIED_TEXT not in spoken:
                spoken.append(TRUST_DENIED_TEXT)
            return {"ok": False, "error": "Keine Berechtigung: der Nutzer darf dieses Gerät nicht steuern."}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": result.sent}

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
    def _slot_value(dev: TypedDevice, kind: int):
        slot = dev.slot_of_kind(kind)
        return slot.value if slot is not None else None

    @staticmethod
    def _is_active(dev: TypedDevice) -> bool:
        on = ToolAgent._slot_value(dev, K.SLOT_KIND_ON)
        if on is not None:
            return bool(on)
        if dev.has_slot(K.SLOT_KIND_ON):
            return False
        for kind in (K.SLOT_KIND_BRIGHTNESS, K.SLOT_KIND_POSITION):
            level = ToolAgent._slot_value(dev, kind)
            if level is not None:
                return bool(level and int(level) > 0)
        return False

    @staticmethod
    def _format_active_state(dev: TypedDevice) -> str:
        parts = []
        if ToolAgent._slot_value(dev, K.SLOT_KIND_ON):
            parts.append("eingeschaltet")
        for kind in (K.SLOT_KIND_BRIGHTNESS, K.SLOT_KIND_POSITION):
            level = ToolAgent._slot_value(dev, kind)
            if level is not None and int(level) > 0:
                parts.append(f"{int(level)}%")
        color = ToolAgent._slot_value(dev, K.SLOT_KIND_COLOR)
        if color:
            parts.append(f"Farbe #{int(color):06X}")
        power = ToolAgent._slot_value(dev, K.SLOT_KIND_POWER)
        if power is not None:
            parts.append(f"{int(power) if float(power) == int(power) else power}W")
        return ", ".join(parts) if parts else "aktiv"
