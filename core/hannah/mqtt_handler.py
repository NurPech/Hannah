import json
import logging
import threading
from typing import Callable, Optional

import paho.mqtt.client as mqtt

log = logging.getLogger(__name__)


class MQTTHandler:
    def __init__(self, cfg: dict, audio_cfg: dict):
        self._cfg = cfg
        self._audio_cfg = audio_cfg

        self._topic_announcement        = cfg.get("topic_announcement",     "hannah/satellite/+/announcement")
        self._topic_announce_in         = cfg.get("topic_announce_in",      "hannah/announce")
        self._topic_announce_ssml_in    = cfg.get("topic_announce_ssml_in", "hannah/announceSSML")
        self._topic_notification_in     = cfg.get("topic_notification_in",  "hannah/notification")
        self._topic_message_in          = cfg.get("topic_message_in",       "hannah/message")

        self._topic_global_volume    = cfg.get("topic_global_volume", "hannah/volume")
        self._topic_sat_volume_state = "hannah/satellite/+/volume/state"
        self._topic_sat_mute_state   = "hannah/satellite/+/mute/state"
        self._topic_sat_dnd          = "hannah/satellite/+/dnd"

        self._on_announcement:       Optional[Callable[[str, str], None]] = None
        self._on_room_announce:      Optional[Callable[[str, str], None]] = None
        self._on_room_announce_ssml: Optional[Callable[[str, str], None]] = None
        self._on_notification:       Optional[Callable[[str, str], None]] = None
        self._on_message_create:     Optional[Callable[[int, str, str], None]] = None
        self._on_volume: Optional[Callable[[Optional[str], int], None]] = None
        self._on_mute:   Optional[Callable[[str, bool], None]] = None
        self._on_dnd:    Optional[Callable[[str, bool], None]] = None

        self._on_ota_pending: Optional[Callable[[str, str], None]] = None
        self._on_firmware:    Optional[Callable[[str, str, str, int], None]] = None
        self._on_ble_report:  Optional[Callable[[str, str, int], None]] = None
        self._on_sensor:      Optional[Callable[[str, float, float, float, float, int, float, float], None]] = None
        self._on_play_asset_result: Optional[Callable[[str, str, bool], None]] = None

        self._playback_done_events: dict[str, threading.Event] = {}
        self._playback_done_lock = threading.Lock()

        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.will_set("hannah/status", "offline", qos=1, retain=True)

        username = cfg.get("username", "")
        password = cfg.get("password", "")
        if username:
            self._client.username_pw_set(username, password or None)

    # ------------------------------------------------------------------
    # Announcement / Notification

    def set_announcement_handler(self, callback: Callable[[str, str], None]):
        self._on_announcement = callback

    def set_room_announce_handler(self, callback: Callable[[str, str], None]):
        self._on_room_announce = callback

    def set_room_announce_ssml_handler(self, callback: Callable[[str, str], None]):
        self._on_room_announce_ssml = callback

    def set_notification_handler(self, callback: Callable[[str, str], None]):
        self._on_notification = callback

    def set_message_handler(self, callback: Callable[[int, str, str], None]):
        """callback(user_id, content, source) — hannah/message (#234), Vorbild
        hannah/notification. Anders als Notify/Announce ausschließlich für die
        passive Mailbox gedacht, kein Broadcast/Sofort-TTS."""
        self._on_message_create = callback

    # ------------------------------------------------------------------
    # Satellite volume / mute / DND

    def set_volume_handler(self, callback: Callable[[Optional[str], int], None]):
        self._on_volume = callback

    def set_mute_handler(self, callback: Callable[[str, bool], None]):
        self._on_mute = callback

    def set_dnd_handler(self, callback: Callable[[str, bool], None]):
        self._on_dnd = callback

    def publish_volume_state(self, level: int, device: Optional[str] = None):
        topic = (f"hannah/satellite/{device}/volume/state" if device
                 else f"{self._topic_global_volume}/state")
        self._client.publish(topic, str(level), qos=1, retain=True)

    def publish_mute_state(self, device: str, muted: bool):
        self._client.publish(f"hannah/satellite/{device}/mute/state",
                             "true" if muted else "false", qos=1, retain=True)

    def publish_mute_set(self, device: str, muted: bool):
        self._client.publish(f"hannah/satellite/{device}/mute/set",
                             "true" if muted else "false", qos=1)

    def publish_volume_set(self, device: str, level: int):
        self._client.publish(f"hannah/satellite/{device}/volume/set",
                             str(level), qos=1)

    def publish_dnd_state(self, device: str, active: bool):
        self._client.publish(f"hannah/satellite/{device}/dnd/state",
                             "true" if active else "false", qos=1, retain=True)

    # ------------------------------------------------------------------
    # OTA / firmware / sensors / BLE

    def set_ota_pending_handler(self, callback: Callable[[str, str], None]):
        self._on_ota_pending = callback

    def set_firmware_handler(self, callback: Callable[[str, str, str, int], None]):
        self._on_firmware = callback

    def set_sensor_handler(self, callback: Callable[[str, float, float, float, float], None]):
        self._on_sensor = callback

    def set_ble_report_handler(self, callback: Callable[[str, str, int], None]):
        self._on_ble_report = callback

    def set_play_asset_result_handler(self, callback: Callable[[str, str, bool], None]):
        """callback(device, asset_id, ok) — Ack/Nack vom Satelliten für einen
        play_asset-Versuch (#116, vorher komplett Fire-and-Forget)."""
        self._on_play_asset_result = callback

    def publish_ble_watchlist(self, device: str, macs: list[str]):
        topic = f"hannah/satellite/{device}/ble/watchlist"
        self._client.publish(topic, json.dumps({"macs": macs}), qos=1, retain=True)
        log.debug(f"BLE-Watchlist → {device}: {len(macs)} MAC(s)")

    def publish_asset_relevant(self, device: str, asset_ids: list[str]):
        """Retained Relevanzliste für hannah_asset (#170) — ersetzt das vorherige
        blinde "alles im Manifest laden" auf dem Satelliten durch eine von Core
        gepflegte Liste der tatsächlich benötigten Asset-IDs."""
        topic = f"hannah/satellite/{device}/assets/relevant"
        self._client.publish(topic, json.dumps(asset_ids), qos=1, retain=True)
        log.debug(f"Asset-Relevanzliste → {device}: {asset_ids}")

    def publish_ota_ok(self, device: str):
        self._client.publish(f"hannah/satellite/{device}/ota/ok", "", qos=1)
        log.info(f"OTA-OK → hannah/satellite/{device}/ota/ok")

    def publish_restart(self, device: str):
        """Remote-Neustart (#161) — Diagnose-/Rejuvenation-Werkzeug für den
        Ressourcenerschöpfungs-Verdacht aus #150. Funktioniert auch, wenn OTA/Webserver
        auf dem Satelliten bereits nicht mehr reagieren, da MQTT in diesem Fehlerfall
        nachweislich weiterläuft."""
        self._client.publish(f"hannah/satellite/{device}/restart", "", qos=1)
        log.info(f"Remote-Neustart → hannah/satellite/{device}/restart")

    def publish_virtual_ptt(self, device: str, active: bool):
        self._client.publish(
            f"hannah/satellite/{device}/ptt",
            "true" if active else "false",
            qos=1,
        )
        log.debug(f"Virtual PTT {'AN' if active else 'AUS'} → {device}")

    def publish_sampling_mode(self, device: str, enabled: bool, sample_type: str = "noise"):
        import json as _json
        payload = _json.dumps({"enabled": enabled, "type": sample_type})
        self._client.publish(f"hannah/satellite/{device}/sampling", payload, qos=1, retain=True)
        log.info(f"Sampling-Mode {'an' if enabled else 'aus'} (type={sample_type}) → hannah/satellite/{device}/sampling")

    def publish_listen(self, device: str):
        self._client.publish(f"hannah/satellite/{device}/listen", "", qos=1)
        log.debug(f"start_listening → {device}")

    def publish_play_asset(self, device: str, asset_id: str):
        import json as _json
        payload = _json.dumps({"asset_id": asset_id})
        self._client.publish(f"hannah/satellite/{device}/play_asset", payload, qos=1)
        log.info(f"PlayAsset '{asset_id}' → hannah/satellite/{device}/play_asset")

    def publish_notifications_pending(self, device: str, pending: bool):
        """Persistenter Zustand (retain) — solange True, zeigt der Satellit im
        Idle-Zustand ein gedimmtes gelbes LED statt aus (#234)."""
        self._client.publish(f"hannah/satellite/{device}/notifications_pending",
                             "true" if pending else "false", qos=1, retain=True)
        log.debug(f"NotificationsPending={pending} → hannah/satellite/{device}/notifications_pending")

    # ------------------------------------------------------------------
    # Playback-Done-Ack (generisch — jede beendete Wiedergabe auf dem
    # Satelliten, nicht nur TTS/Plink-spezifisch; siehe #155)

    def reset_playback_done(self, device: str):
        """Vor dem Senden einer Wiedergabe aufrufen, um auf deren Ende zu warten."""
        with self._playback_done_lock:
            self._playback_done_events.setdefault(device, threading.Event()).clear()

    def wait_for_playback_done(self, device: str, timeout: float) -> bool:
        """Blockiert bis der Satellit `playback_done` meldet oder timeout abläuft.
        False = kein Ack angekommen (z.B. ältere Firmware ohne Publish) — Aufrufer
        sollte auf eine feste Wartezeit zurückfallen."""
        with self._playback_done_lock:
            event = self._playback_done_events.setdefault(device, threading.Event())
        return event.wait(timeout)

    # ------------------------------------------------------------------
    # Discovery / raw

    def publish_discovery(self, udp_host: str, udp_port: int, topic: str = "hannah/server"):
        import socket
        if not udp_host:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                udp_host = s.getsockname()[0]
        payload = json.dumps({"host": udp_host, "port": udp_port})
        self._client.publish(topic, payload, qos=1, retain=True)
        log.info(f"Discovery → {topic}: {payload}")

    def publish_raw(self, topic: str, payload: str):
        self._client.publish(topic, payload, qos=1)
        log.debug(f"→ {topic}: {payload!r}")

    # ------------------------------------------------------------------
    # Connect / disconnect

    def connect(self):
        host = self._cfg.get("host", "localhost")
        port = self._cfg.get("port", 1883)
        log.info(f"Verbinde mit MQTT-Broker {host}:{port} ...")
        self._client.connect(host, port, keepalive=60)
        self._client.loop_start()

    def disconnect(self):
        self._client.loop_stop()
        self._client.disconnect()

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

        client.subscribe(self._topic_announcement, qos=1)
        log.info(f"Announcements abonniert: '{self._topic_announcement}'")
        client.subscribe(self._topic_announce_in, qos=1)
        log.info(f"Raum-Announcements abonniert: '{self._topic_announce_in}'")
        client.subscribe(self._topic_announce_ssml_in, qos=1)
        log.info(f"SSML-Announcements abonniert: '{self._topic_announce_ssml_in}'")
        client.subscribe(self._topic_notification_in, qos=1)
        log.info(f"System-Notifications abonniert: '{self._topic_notification_in}'")
        client.subscribe(self._topic_message_in, qos=1)
        log.info(f"Messages abonniert: '{self._topic_message_in}'")

        client.subscribe(self._topic_global_volume, qos=1)
        client.subscribe(self._topic_sat_volume_state, qos=1)
        client.subscribe(self._topic_sat_mute_state, qos=1)
        client.subscribe(self._topic_sat_dnd, qos=1)
        log.info("Satellit-Steuerung abonniert: volume/state / mute/state / dnd")

        client.subscribe("hannah/satellite/+/ota/pending", qos=1)
        client.subscribe("hannah/satellite/+/firmware", qos=1)
        client.subscribe("hannah/satellite/+/ble/report", qos=0)
        client.subscribe("hannah/satellite/+/sensors", qos=0)
        client.subscribe("hannah/satellite/+/play_asset/result", qos=1)
        client.subscribe("hannah/satellite/+/playback_done", qos=1)
        log.info("OTA / firmware / BLE / sensors / play_asset-Ergebnis / playback_done abonniert")

    def _on_message(self, client, userdata, msg):
        topic = msg.topic

        if topic.startswith("hannah/satellite/") and topic.endswith("/sensors"):
            parts = topic.split("/")
            if len(parts) == 4 and self._on_sensor:
                try:
                    data = json.loads(msg.payload.decode())
                    self._on_sensor(
                        parts[2],
                        float(data.get("temperature", 0.0)),
                        float(data.get("pressure", 0.0)),
                        float(data.get("humidity", 0.0)),
                        float(data.get("iaq", 0.0)),
                        int(data.get("iaq_accuracy", 0)),
                        float(data.get("co2_equiv", 0.0)),
                        float(data.get("voc_equiv", 0.0)),
                    )
                except Exception:
                    pass
            return

        if topic.startswith("hannah/satellite/") and topic.endswith("/ble/report"):
            parts = topic.split("/")
            if len(parts) == 5 and self._on_ble_report:
                try:
                    data = json.loads(msg.payload.decode())
                    mac = data.get("mac", "")
                    rssi = int(data.get("rssi", 0))
                    if mac:
                        self._on_ble_report(parts[2], mac, rssi)
                except Exception:
                    pass
            return

        if topic.startswith("hannah/satellite/") and topic.endswith("/firmware"):
            parts = topic.split("/")
            if len(parts) == 4 and self._on_firmware:
                try:
                    data = json.loads(msg.payload.decode())
                    version = data.get("version", "")
                    if version:
                        # restart_reason/restart_count fehlen bei älterer Firmware ohne #165 —
                        # dann Default ""/0, main.py's _on_firmware_version überspringt das
                        # Speichern der Neustart-Historie in diesem Fall.
                        self._on_firmware(
                            parts[2], version,
                            data.get("restart_reason", ""), int(data.get("restart_count", 0)),
                        )
                except Exception:
                    pass
            return

        if topic.startswith("hannah/satellite/") and topic.endswith("/playback_done"):
            parts = topic.split("/")
            if len(parts) == 4:
                device = parts[2]
                with self._playback_done_lock:
                    self._playback_done_events.setdefault(device, threading.Event()).set()
            return

        if topic.startswith("hannah/satellite/") and topic.endswith("/play_asset/result"):
            parts = topic.split("/")
            if len(parts) == 5 and self._on_play_asset_result:
                try:
                    data = json.loads(msg.payload.decode())
                    asset_id = data.get("asset_id", "")
                    if asset_id:
                        self._on_play_asset_result(parts[2], asset_id, bool(data.get("ok")))
                except Exception:
                    pass
            return

        if topic.startswith("hannah/satellite/") and topic.endswith("/ota/pending"):
            parts = topic.split("/")
            if len(parts) == 5 and self._on_ota_pending:
                try:
                    data = json.loads(msg.payload.decode())
                    version = data.get("version", "")
                    if data.get("pending") and version:
                        self._on_ota_pending(parts[2], version)
                except Exception:
                    pass
            return

        if topic == self._topic_global_volume:
            try:
                level = max(0, min(100, int(msg.payload.decode().strip())))
                if self._on_volume:
                    self._on_volume(None, level)
            except ValueError:
                pass
            return

        prefix = "hannah/satellite/"
        if topic.startswith(prefix):
            raw = msg.payload.decode().strip()
            if topic.endswith("/volume/state"):
                device = topic[len(prefix):-len("/volume/state")]
                if self._on_volume:
                    try:
                        self._on_volume(device, max(0, min(100, int(raw))))
                    except ValueError:
                        pass
                return
            if topic.endswith("/mute/state"):
                device = topic[len(prefix):-len("/mute/state")]
                if self._on_mute:
                    self._on_mute(device, raw.lower() in ("true", "1", "yes", "on"))
                return
            if topic.endswith("/dnd"):
                device = topic[len(prefix):-len("/dnd")]
                if self._on_dnd:
                    self._on_dnd(device, raw.lower() in ("true", "1", "yes", "on"))
                return

        if topic == self._topic_notification_in:
            raw = msg.payload.decode("utf-8", errors="replace").strip()
            try:
                data = json.loads(raw)
                text     = data.get("text", "").strip()
                severity = "direct" if data.get("type") == "direct" else data.get("severity", "notify")
            except (json.JSONDecodeError, AttributeError):
                text, severity = raw, "notify"
            if text and self._on_notification:
                log.info(f"System-Notification [{severity}]: {text!r}")
                threading.Thread(target=self._on_notification, args=(text, severity), daemon=True).start()
            return

        if topic == self._topic_message_in:
            raw = msg.payload.decode("utf-8", errors="replace").strip()
            try:
                data = json.loads(raw)
                user_id = int(data.get("user_id", 0))
                content = (data.get("content") or "").strip()
                source  = (data.get("source") or "").strip()
            except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
                user_id, content, source = 0, "", ""
            if user_id and content and self._on_message_create:
                log.info(f"Message für user_id={user_id}: {content!r} (source={source!r})")
                threading.Thread(target=self._on_message_create, args=(user_id, content, source), daemon=True).start()
            else:
                log.warning(f"Message-Payload ungültig oder unvollständig, verworfen: {raw!r}")
            return

        if topic == self._topic_announce_ssml_in:
            raw = msg.payload.decode("utf-8", errors="replace").strip()
            try:
                data = json.loads(raw)
                ssml = data.get("ssml", "").strip()
                room = data.get("room", "all")
            except (json.JSONDecodeError, AttributeError):
                ssml, room = raw, "all"
            if ssml and self._on_room_announce_ssml:
                log.info(f"SSML-Announcement → {room!r}")
                threading.Thread(target=self._on_room_announce_ssml, args=(room, ssml), daemon=True).start()
            return

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
                threading.Thread(target=self._on_room_announce, args=(room, text), daemon=True).start()
            return

        ann_parts = self._topic_announcement.split("+")
        if len(ann_parts) == 2 and topic.startswith(ann_parts[0]) and topic.endswith(ann_parts[1]):
            device = topic[len(ann_parts[0]):-len(ann_parts[1])] if ann_parts[1] else topic[len(ann_parts[0]):]
            text   = msg.payload.decode("utf-8", errors="replace").strip()
            if text and self._on_announcement:
                log.info(f"Announcement → {device}: {text!r}")
                threading.Thread(target=self._on_announcement, args=(device, text), daemon=True).start()
            return

        log.debug(f"Unbekanntes Topic: {topic}")
