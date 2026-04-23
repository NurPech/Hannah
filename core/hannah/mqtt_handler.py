import json
import logging
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

import paho.mqtt.client as mqtt

from .nlu import Intent

log = logging.getLogger(__name__)


class MQTTHandler:
    def __init__(self, cfg: dict, audio_cfg: dict):
        """
        cfg        : mqtt-Abschnitt aus config.yaml
        audio_cfg  : audio-Abschnitt
        """
        self._cfg = cfg
        self._audio_cfg = audio_cfg

        self._topic_intent_out  = cfg.get("topic_intent_out",  "hannah/{device}/intent")
        self._topic_text_out    = cfg.get("topic_text_out",    "hannah/{device}/text")
        self._topic_error_out   = cfg.get("topic_error_out",   "hannah/{device}/error")
        self._topic_answer_out  = cfg.get("topic_answer_out",  "hannah/{device}/answer")
        self._topic_cmd_in        = cfg.get("topic_text_command_in",  "hannah/commands/textcommand")
        self._topic_cmd_answer    = cfg.get("topic_text_answer_out", "hannah/commands/answer")
        self._topic_sat_status    = cfg.get("topic_sat_status",      "hannah/satelite/{device}/status")
        self._topic_sat_online    = cfg.get("topic_sat_online",      "hannah/satelite/{device}/online")
        self._topic_announcement  = cfg.get("topic_announcement",    "hannah/satelite/+/announcement")
        self._topic_rooms         = cfg.get("topic_rooms",            "hannah/rooms")
        self._topic_announce_in         = cfg.get("topic_announce_in",        "hannah/announce")
        self._topic_announce_ssml_in    = cfg.get("topic_announce_ssml_in",   "hannah/announceSSML")
        self._topic_notification_in     = cfg.get("topic_notification_in",    "hannah/notification")

        # Satellit-Steuerung: hannah/volume, hannah/satelite/+/volume|mute|dnd
        self._topic_global_volume    = cfg.get("topic_global_volume", "hannah/volume")
        self._topic_sat_volume       = "hannah/satelite/+/volume"
        self._topic_sat_mute         = "hannah/satelite/+/mute"
        self._topic_sat_dnd          = "hannah/satelite/+/dnd"

        self._on_announcement: Optional[Callable[[str, str], None]] = None
        self._on_room_announce: Optional[Callable[[str, str], None]] = None
        self._on_room_announce_ssml: Optional[Callable[[str, str], None]] = None
        self._on_notification: Optional[Callable[[str, str], None]] = None
        # Callbacks: fn(device_or_None, value) — device=None bedeutet global
        self._on_volume: Optional[Callable[[Optional[str], int], None]] = None
        self._on_mute:   Optional[Callable[[str, bool], None]] = None
        self._on_dnd:    Optional[Callable[[str, bool], None]] = None

        # Optionaler Callback für eingehende State-Updates: fn(state_id, raw_payload)
        self._on_state_update: Optional[Callable[[str, str], None]] = None
        self._state_topic_prefix: Optional[str] = None  # z.B. "javascript/0/virtualDevice"

        # Optionaler Callback für Wetter-Updates: fn(topic, raw_payload)
        self._on_weather_update: Optional[Callable[[str, str], None]] = None
        self._weather_topic_prefix: Optional[str] = None  # z.B. "openweathermap/0/forecast"

        # Optionaler Callback für Residents-Updates: fn(topic, raw_payload)
        self._on_residents_update: Optional[Callable[[str, str], None]] = None
        self._residents_topic_prefix: Optional[str] = None  # z.B. "residents/0/roomie"

        # Auto-Status-Updates: Liste von (prefix, callback) — ein Eintrag pro Auto
        self._car_handlers: list[tuple[str, Callable[[str, str], None]]] = []

        # Globale DND/Mute-Updates von ioBroker: fn(bool)
        self._on_global_dnd:  Optional[Callable[[bool], None]] = None
        self._on_global_mute: Optional[Callable[[bool], None]] = None

        # Optionaler Callback für Text-Commands: fn(text)
        self._on_text_command: Optional[Callable[[str], None]] = None

        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.will_set("hannah/status", "offline", qos=1, retain=True)

        username = cfg.get("username", "")
        password = cfg.get("password", "")
        if username:
            self._client.username_pw_set(username, password or None)

    def set_room_announce_handler(self, callback: Callable[[str, str], None]):
        """
        Registriert einen Callback für raumbasierte Announcements.
        callback(room, text) — room ist ein Raumname oder "all"
        Topic: hannah/announce  Payload: {"text": "...", "room": "wohnzimmer"}
        """
        self._on_room_announce = callback

    def set_notification_handler(self, callback: Callable[[str, str], None]):
        """
        Registriert einen Callback für eingehende System-Notifications.
        callback(text, severity) — severity: "alert" | "notify" | "info" (ioBroker-Werte)
        Topic: hannah/notification  Payload: {"text": "...", "severity": "..."}
        """
        self._on_notification = callback

    def set_room_announce_ssml_handler(self, callback: Callable[[str, str], None]):
        """
        Registriert einen Callback für SSML-Announcements.
        callback(room, ssml) — ssml kann ein Fragment oder vollständiger <speak>-Block sein.
        Topic: hannah/announceSSML  Payload: {"ssml": "...", "room": "wohnzimmer"}
        """
        self._on_room_announce_ssml = callback

    def set_volume_handler(self, callback: Callable[[Optional[str], int], None]):
        """callback(device_or_None, level 0-100) — None = globale Lautstärke."""
        self._on_volume = callback

    def set_mute_handler(self, callback: Callable[[str, bool], None]):
        """callback(device, muted) — Mikrofon-Mute eines Satelliten."""
        self._on_mute = callback

    def set_dnd_handler(self, callback: Callable[[str, bool], None]):
        """callback(device, dnd_active) — Do-Not-Disturb eines Satelliten."""
        self._on_dnd = callback

    def publish_volume_state(self, level: int, device: Optional[str] = None):
        """Publiziert aktuelle Lautstärke als retained Message."""
        if device:
            topic = f"hannah/satelite/{device}/volume/state"
        else:
            topic = f"{self._topic_global_volume}/state"
        self._client.publish(topic, str(level), qos=1, retain=True)

    def publish_mute_state(self, device: str, muted: bool):
        """Publiziert Mute-Status eines Satelliten (retained)."""
        self._client.publish(f"hannah/satelite/{device}/mute/state",
                             "true" if muted else "false", qos=1, retain=True)

    def publish_dnd_state(self, device: str, active: bool):
        """Publiziert DND-Status eines Satelliten (retained)."""
        self._client.publish(f"hannah/satelite/{device}/dnd/state",
                             "true" if active else "false", qos=1, retain=True)

    def publish_global_dnd(self, active: bool):
        """Publiziert globalen DND-Status (retained) — ioBroker + Satelliten reagieren darauf."""
        self._client.publish("hannah/dnd", "true" if active else "false", qos=1, retain=True)

    def publish_global_mute(self, active: bool):
        """Publiziert globales Mute (retained) — ioBroker + Satelliten reagieren darauf."""
        self._client.publish("hannah/mute", "true" if active else "false", qos=1, retain=True)

    def set_global_dnd_handler(self, callback: Callable[[bool], None]):
        """Callback wenn ioBroker hannah/dnd ändert: fn(active: bool)."""
        self._on_global_dnd = callback

    def set_global_mute_handler(self, callback: Callable[[bool], None]):
        """Callback wenn ioBroker hannah/mute ändert: fn(active: bool)."""
        self._on_global_mute = callback

    def publish_transcript(self, room: str, text: str):
        """Publiziert das letzte Transkript eines Raums (retained)."""
        self._client.publish(f"hannah/rooms/{room}/transcript", text, qos=1, retain=True)

    def publish_speaking(self, room: str, speaking: bool):
        """Publiziert ob Hannah gerade in einem Raum spricht (retained)."""
        self._client.publish(f"hannah/rooms/{room}/speaking",
                             "true" if speaking else "false", qos=1, retain=True)

    def publish_speaker(self, roomie_id: str):
        """Publiziert den aktuell erkannten Sprecher via Voice-ID (retained)."""
        self._client.publish("hannah/speaker", roomie_id or "", qos=1, retain=True)

    def publish_tts_backend(self, name: str):
        """Publiziert das aktive TTS-Backend (retained) — z.B. 'azure_KatjaNeural' oder 'piper'."""
        self._client.publish("hannah/tts/backend", name, qos=1, retain=True)

    def set_announcement_handler(self, callback: Callable[[str, str], None]):
        """
        Registriert einen Callback für Announcements.
        callback(device, text) — device ist der Satellit-Name oder "all"
        Topic: hannah/satelite/{device}/announcement
        """
        self._on_announcement = callback

    def set_weather_handler(self, prefix: str, callback: Callable[[str, str], None]):
        """
        Abonniert Wetter-Topics und leitet Updates weiter.
        prefix   : z.B. "openweathermap/0/forecast"
        callback : fn(topic, raw_value)
        """
        self._weather_topic_prefix = prefix
        self._on_weather_update = callback

    def set_residents_handler(self, prefix: str, callback: Callable[[str, str], None]):
        """
        Abonniert Residents-Topics und leitet Updates weiter.
        prefix   : z.B. "residents/0/roomie"
        callback : fn(topic, raw_value)
        """
        self._residents_topic_prefix = prefix
        self._on_residents_update = callback

    def add_car_handler(self, prefix: str, callback: Callable[[str, str], None]):
        """
        Registriert ein Auto. Kann mehrfach aufgerufen werden (ein Aufruf pro Auto).
        prefix   : z.B. "javascript/0/virtualDevice/Auto/Leonie/Auto1"
        callback : fn(topic, raw_value)
        """
        self._car_handlers.append((prefix, callback))

    def set_text_command_handler(self, callback: Callable[[str], None]):
        """Registriert einen Callback für Text-Commands auf hannah/commands/textcommand."""
        self._on_text_command = callback

    def publish_text_answer(self, text: str):
        """Publiziert eine Antwort auf hannah/answer (für Text-Command-Tests)."""
        self._client.publish(self._topic_cmd_answer, text, qos=1)
        log.info(f"Text-Antwort: {text!r}")

    def set_state_subscriber(self, prefix: str, callback: Callable[[str, str], None]):
        """
        Registriert einen Callback für State-Updates aus ioBroker.

        prefix   : MQTT-Topic-Prefix, z.B. "javascript/0/virtualDevice"
                   Hannah abonniert <prefix>/# und leitet alle Nachrichten
                   als (state_id, raw_payload) an den Callback weiter.
        callback : fn(state_id: str, raw_payload: str)
                   state_id ist der ioBroker-Pfad (Slashes → Punkte),
                   z.B. "javascript.0.virtualDevice.Licht.EG.Wohnzimmer.DeckeSeite.on"
        """
        self._state_topic_prefix = prefix
        self._on_state_update = callback

    def connect(self):
        host = self._cfg.get("host", "localhost")
        port = self._cfg.get("port", 1883)
        log.info(f"Verbinde mit MQTT-Broker {host}:{port} ...")
        self._client.connect(host, port, keepalive=60)
        self._client.loop_start()

    def publish_discovery(self, udp_host: str, udp_port: int, topic: str = "hannah/server"):
        """Publiziert Hannah's UDP-Adresse als retained Message, damit Satelliten sie abfragen können."""
        import json, socket
        if not udp_host:
            # Eigene IP ermitteln
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                udp_host = s.getsockname()[0]
        payload = json.dumps({"host": udp_host, "port": udp_port})
        self._client.publish(topic, payload, qos=1, retain=True)
        log.info(f"Discovery publiziert: {topic} → {payload} (retain=True)")

    def disconnect(self):
        self._client.loop_stop()
        self._client.disconnect()

    # ------------------------------------------------------------------
    # Publish helpers

    def publish_intent(self, device: str, intent: Intent):
        topic = self._topic_intent_out.format(device=device)
        payload = {
            "device": device,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **intent.to_dict(),
        }
        self._publish(topic, payload)

    def publish_raw(self, topic: str, payload: str):
        """Sendet einen rohen String-Payload auf ein beliebiges Topic (QoS 1)."""
        self._client.publish(topic, payload, qos=1)
        log.debug(f"→ {topic}: {payload!r}")

    def publish_satellite_status(self, device: str, state: str):
        """
        Publiziert den Satellit-Status als retained Message.
        Zustände: idle | listening | processing | speaking
        """
        topic = self._topic_sat_status.format(device=device)
        self._client.publish(topic, state, qos=1, retain=True)
        log.debug(f"Satellit-Status → {device}: {state}")

    def publish_satellite_online(self, device: str, online: bool):
        """Publiziert den Online-Status eines Satelliten (retained)."""
        topic = self._topic_sat_online.format(device=device)
        self._client.publish(topic, "true" if online else "false", qos=1, retain=True)

    def publish_rooms(self, satellite_map: dict[str, str]):
        """
        Publiziert die aktuelle Raum-Karte als retained JSON.
        satellite_map: {device_name: room_name}
        Ausgabe: {"wohnzimmer": ["sat-1"], "schlafzimmer": ["sat-2"]}
        """
        rooms: dict[str, list[str]] = {}
        for device, room in satellite_map.items():
            rooms.setdefault(room.lower(), []).append(device)
        payload = json.dumps(rooms, ensure_ascii=False)
        self._client.publish(self._topic_rooms, payload, qos=1, retain=True)
        log.info(f"Raum-Registry publiziert: {payload}")

    def publish_answer(self, device: str, text: str):
        """Publiziert eine Sprachantwort auf hannah/{device}/answer."""
        topic = self._topic_answer_out.format(device=device)
        self._publish(topic, {"device": device, "answer": text})
        log.info(f"[{device}] Antwort: {text!r}")

    def publish_text(self, device: str, text: str):
        topic = self._topic_text_out.format(device=device)
        self._publish(topic, {"device": device, "text": text})

    def publish_error(self, device: str, message: str):
        topic = self._topic_error_out.format(device=device)
        self._publish(topic, {"device": device, "error": message})

    # ------------------------------------------------------------------
    # Internal

    def _publish(self, topic: str, payload: dict):
        msg = json.dumps(payload, ensure_ascii=False)
        self._client.publish(topic, msg, qos=1)
        log.debug(f"→ {topic}: {msg}")

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            log.error(f"MQTT Verbindungsfehler: {reason_code}")
            return

        client.publish("hannah/status", "online", qos=1, retain=True)

        client.subscribe(self._topic_cmd_in, qos=1)
        log.info(f"Text-Commands abonniert: '{self._topic_cmd_in}'")

        client.subscribe(self._topic_announcement, qos=1)
        log.info(f"Announcements abonniert: '{self._topic_announcement}'")

        client.subscribe(self._topic_announce_in, qos=1)
        log.info(f"Raum-Announcements abonniert: '{self._topic_announce_in}'")

        client.subscribe(self._topic_notification_in, qos=1)
        log.info(f"System-Notifications abonniert: '{self._topic_notification_in}'")

        client.subscribe(self._topic_announce_ssml_in, qos=1)
        log.info(f"SSML-Announcements abonniert: '{self._topic_announce_ssml_in}'")

        client.subscribe(self._topic_global_volume, qos=1)
        client.subscribe(self._topic_sat_volume, qos=1)
        client.subscribe(self._topic_sat_mute, qos=1)
        client.subscribe(self._topic_sat_dnd, qos=1)
        log.info("Satellit-Steuerung abonniert: volume / mute / dnd")

        client.subscribe("hannah/dnd", qos=1)
        client.subscribe("hannah/mute", qos=1)
        log.info("Globales DND/Mute abonniert")

        if self._state_topic_prefix:
            sub = f"{self._state_topic_prefix}/#"
            client.subscribe(sub, qos=0)
            log.info(f"Abonniere State-Updates: '{sub}'")

        if self._weather_topic_prefix:
            sub = f"{self._weather_topic_prefix}/#"
            client.subscribe(sub, qos=0)
            log.info(f"Abonniere Wetter-Updates: '{sub}'")

        if self._residents_topic_prefix:
            sub = f"{self._residents_topic_prefix}/#"
            client.subscribe(sub, qos=0)
            log.info(f"Abonniere Residents-Updates: '{sub}'")

        for prefix, _ in self._car_handlers:
            sub = f"{prefix}/#"
            client.subscribe(sub, qos=0)
            log.info(f"Abonniere Auto-Status: '{sub}'")

    def _on_message(self, client, userdata, msg):
        topic = msg.topic

        # Wetter-Update aus ioBroker?
        if self._weather_topic_prefix and topic.startswith(self._weather_topic_prefix + "/"):
            if self._on_weather_update:
                self._on_weather_update(topic, msg.payload.decode("utf-8", errors="replace"))
            return

        # Residents-Update aus ioBroker?
        if self._residents_topic_prefix and topic.startswith(self._residents_topic_prefix + "/"):
            if self._on_residents_update:
                self._on_residents_update(topic, msg.payload.decode("utf-8", errors="replace"))
            return

        # Auto-Status-Update?
        for prefix, callback in self._car_handlers:
            if topic.startswith(prefix + "/"):
                callback(topic, msg.payload.decode("utf-8", errors="replace"))
                return

        # State-Update aus ioBroker?
        if self._state_topic_prefix and topic.startswith(self._state_topic_prefix + "/"):
            state_id = topic.replace("/", ".")
            if self._on_state_update:
                self._on_state_update(state_id, msg.payload.decode("utf-8", errors="replace"))
            return

        # Satellit-Steuerung: volume / mute / dnd
        if topic == self._topic_global_volume:
            try:
                level = max(0, min(100, int(msg.payload.decode().strip())))
                if self._on_volume:
                    self._on_volume(None, level)
            except ValueError:
                pass
            return

        for ctrl, callback_attr in (
            ("volume", "_on_volume"),
            ("mute",   "_on_mute"),
            ("dnd",    "_on_dnd"),
        ):
            prefix = f"hannah/satelite/"
            suffix = f"/{ctrl}"
            if topic.startswith(prefix) and topic.endswith(suffix) and not topic.endswith("/state"):
                device = topic[len(prefix):-len(suffix)]
                raw = msg.payload.decode().strip().lower()
                cb = getattr(self, callback_attr)
                if not cb:
                    return
                if ctrl == "volume":
                    try:
                        cb(device, max(0, min(100, int(raw))))
                    except ValueError:
                        pass
                else:
                    cb(device, raw in ("true", "1", "yes", "on"))
                return

        # System-Notification (hannah/notification)?
        if topic == self._topic_notification_in:
            raw = msg.payload.decode("utf-8", errors="replace").strip()
            try:
                data = json.loads(raw)
                text     = data.get("text", "").strip()
                severity = data.get("severity", "notify")
            except (json.JSONDecodeError, AttributeError):
                text, severity = raw, "notify"
            if text and self._on_notification:
                log.info(f"System-Notification [{severity}] empfangen: {text!r}")
                threading.Thread(target=self._on_notification, args=(text, severity), daemon=True).start()
            return

        # SSML-Raum-Announcement (hannah/announceSSML)?
        if topic == self._topic_announce_ssml_in:
            raw = msg.payload.decode("utf-8", errors="replace").strip()
            try:
                data = json.loads(raw)
                ssml = data.get("ssml", "").strip()
                room = data.get("room", "all")
            except (json.JSONDecodeError, AttributeError):
                ssml, room = raw, "all"
            if ssml and self._on_room_announce_ssml:
                log.info(f"SSML-Announcement → {room!r}: {ssml[:80]!r}{'…' if len(ssml) > 80 else ''}")
                threading.Thread(
                    target=self._on_room_announce_ssml, args=(room, ssml), daemon=True
                ).start()
            return

        # Raum-Announcement (hannah/announce)?
        if topic == self._topic_announce_in:
            raw = msg.payload.decode("utf-8", errors="replace").strip()
            try:
                data = json.loads(raw)
                text = data.get("text", "").strip()
                room = data.get("room", "all")
            except (json.JSONDecodeError, AttributeError):
                text, room = raw, "all"
            if text and self._on_room_announce:
                log.info(f"Raum-Announcement → {room!r}: {text!r}")
                threading.Thread(
                    target=self._on_room_announce, args=(room, text), daemon=True
                ).start()
            return

        # Announcement?
        ann_parts = self._topic_announcement.split("+")
        if len(ann_parts) == 2 and topic.startswith(ann_parts[0]) and topic.endswith(ann_parts[1]):
            device = topic[len(ann_parts[0]):-len(ann_parts[1])] if ann_parts[1] else topic[len(ann_parts[0]):]
            text   = msg.payload.decode("utf-8", errors="replace").strip()
            if text and self._on_announcement:
                log.info(f"Announcement → {device}: {text!r}")
                threading.Thread(
                    target=self._on_announcement,
                    args=(device, text),
                    daemon=True,
                ).start()
            return

        # Globales DND / Mute von ioBroker?
        if topic == "hannah/dnd":
            val = msg.payload.decode("utf-8", errors="replace").strip().lower() == "true"
            if self._on_global_dnd:
                self._on_global_dnd(val)
            return
        if topic == "hannah/mute":
            val = msg.payload.decode("utf-8", errors="replace").strip().lower() == "true"
            if self._on_global_mute:
                self._on_global_mute(val)
            return

        # Text-Command?
        if topic == self._topic_cmd_in:
            text = msg.payload.decode("utf-8", errors="replace").strip()
            if text and self._on_text_command:
                log.info(f"Text-Command empfangen: {text!r}")
                threading.Thread(
                    target=self._on_text_command,
                    args=(text,),
                    daemon=True,
                ).start()
            return

        log.debug(f"Unbekanntes Topic: {topic}")
