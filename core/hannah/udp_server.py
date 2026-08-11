"""
UDP-Transport für Audio-Streaming zwischen Satelliten und Hannah-Server.

Protokoll (ein Port, 1-Byte Type-Prefix):
  0x01 + JSON  = Control-Nachricht  (beide Richtungen)
  0x02 + PCM   = Audio-Daten        (Satellit → Server, raw 16kHz 16-bit mono)
  0x03 + PCM   = TTS-Audio          (Server → Satellit, gleiches Format)

Control-Nachrichten (Satellit → Server):
  {"type": "register",   "device": "rpi-test"}  -- Raum kommt aus RoomManager, nicht vom Satelliten
  {"type": "audio_end",  "device": "rpi-test"}
  {"type": "heartbeat",  "device": "rpi-test"}

Control-Antworten (Server → Satellit):
  {"type": "registered", "ok": true}
"""

import json
import logging
import socket
import threading
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

TYPE_CONTROL = 0x01
TYPE_AUDIO   = 0x02
TYPE_TTS     = 0x03

# Maximale Größe eines eingehenden UDP-Pakets
_MAX_PACKET = 65535
# Max. Wartezeit auf audio_end nach letztem Audio-Chunk (Sekunden)
_SESSION_TIMEOUT = 10.0


class _AudioSession:
    """Sammelt Audio-Chunks einer laufenden Aufnahme."""

    def __init__(self, device: str, addr: tuple):
        self.device = device
        self.addr = addr
        self.chunks: list[bytes] = []

    def add(self, data: bytes):
        self.chunks.append(data)

    def get_audio(self) -> bytes:
        return b"".join(self.chunks)


class UDPServer:
    """
    UDP-Server der Satelliten-Registrierungen, Audio-Streams und
    TTS-Rücksendungen verwaltet.
    """

    def __init__(
        self,
        cfg: dict,
        on_audio: Callable[[str, bytes], None],
        on_session_start: Optional[Callable[[str], None]] = None,
        on_satellite_change: Optional[Callable[[dict], None]] = None,
        resolve_satellite_room: Optional[Callable[[str], Optional[str]]] = None,
        upsert_satellite: Optional[Callable[[str], None]] = None,
    ):
        """
        cfg                    : udp-Abschnitt aus config.yaml
        on_audio               : Callback(device, raw_pcm_bytes) — aufgerufen wenn eine
                                 Aufnahme vollständig ist (nach audio_end)
        on_satellite_change    : Callback({device: room, ...}) — bei Register/Abmeldung
        resolve_satellite_room : Callback(device_id) → room_id|None — RoomManager-Lookup.
                                 Ohne zugewiesenen Raum gilt ein Satellit als nicht
                                 funktional: kein Tracking, keine Weiterleitung an den Adapter.
        upsert_satellite       : Callback(device_id) — trägt den Satelliten in die
                                 RoomManager-DB ein (auch ohne Raum), damit er in der
                                 Raum-Zuweisung in der Web UI auftaucht.
        """
        self._host = cfg.get("host", "0.0.0.0")
        self._port = cfg.get("port", 7775)
        self._on_audio = on_audio
        self._on_session_start = on_session_start
        self._on_satellite_change = on_satellite_change
        self._resolve_satellite_room = resolve_satellite_room or (lambda *_: None)
        self._upsert_satellite = upsert_satellite or (lambda *_: None)

        # { device_name: {"addr": (ip, port), "room": str, "last_heartbeat": float} }
        self._satellites: dict[str, dict] = {}
        # { device_name: _AudioSession }
        self._sessions: dict[str, _AudioSession] = {}
        self._lock = threading.Lock()

        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle

    def start(self):
        if self._running:
            log.debug("UDP-Server läuft bereits — start() ignoriert.")
            return
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.settimeout(1.0)
        try:
            self._sock.bind((self._host, self._port))
        except OSError as e:
            self._sock.close()
            self._sock = None
            log.warning(
                f"UDP-Server: Port {self._port} belegt ({e}) — "
                f"kein UDP-Start. Proxy läuft vermutlich bereits und meldet sich gleich."
            )
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="hannah-udp"
        )
        self._thread.start()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, daemon=True, name="hannah-udp-watchdog"
        )
        self._watchdog_thread.start()
        log.info(f"UDP-Server lauscht auf {self._host}:{self._port}")

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        if self._watchdog_thread:
            self._watchdog_thread.join(timeout=2)
            self._watchdog_thread = None
        # _watchdog_loop stirbt mit self._running=False sofort mit — _check_heartbeats()
        # läuft danach nie wieder, damit würde jeder zu diesem Zeitpunkt registrierte
        # Satellit für immer als "verbunden" hängen bleiben (GetSatellites() mischt
        # registered_devices_full() unbesehen in den Live-Status). Das passiert praktisch
        # dauerhaft: sobald irgendein Proxy verbunden ist, wird der UDP-Server disabled
        # (s. disable_udp in main.py) und bleibt es i.d.R. auch — jeder Satellit, der
        # jemals auch nur kurz direkt per UDP registriert war, würde sonst als
        # unsterblicher Geist-Eintrag bestehen bleiben, unabhängig davon, was der
        # Proxy-Pfad für ihn später korrekt meldet (beobachtet 2026-08-11: ein Satellit
        # zeigte Stunden nach einer sauber geloggten Proxy-Abmeldung weiterhin als
        # "online", weil dieser alte Direkt-UDP-Eintrag nie geräumt wurde).
        had_satellites = False
        with self._lock:
            had_satellites = bool(self._satellites)
            self._satellites.clear()
        if had_satellites and self._on_satellite_change:
            threading.Thread(
                target=self._on_satellite_change, args=({},), daemon=True
            ).start()
        log.info("UDP-Server gestoppt.")

    # ------------------------------------------------------------------
    # TTS senden

    def send_status(self, device: str, state: str):
        """
        Sendet eine Status-Nachricht an einen registrierten Satelliten.

        Bekannte Zustände:
          idle        — bereit, wartet auf Wake-Word
          listening   — Mikrofon offen, Aufnahme läuft
          processing  — Audio empfangen, STT/NLU laufen
          speaking    — TTS-Audio wird abgespielt
        """
        with self._lock:
            sat = self._satellites.get(device)
        if not sat:
            log.debug(f"send_status: Satellit '{device}' nicht registriert.")
            return
        tts_addr = sat.get("tts_addr", sat["addr"])
        self._send_control({"type": "status", "state": state}, tts_addr)
        log.debug(f"Status → {device}: {state}")

    def send_command(self, device: str, cmd: dict):
        """Sendet einen Steuerbefehl (stop/pause/resume) an einen registrierten Satelliten."""
        with self._lock:
            sat = self._satellites.get(device)
        if not sat:
            log.warning(f"send_command: Satellit '{device}' nicht registriert.")
            return
        tts_addr = sat.get("tts_addr", sat["addr"])
        self._send_control(cmd, tts_addr)
        log.debug(f"Command → {device}: {cmd}")

    def send_tts(self, device: str, pcm_bytes: bytes, sample_rate: int = 16000):
        """Sendet TTS-Audio (raw PCM) an einen registrierten Satelliten."""
        with self._lock:
            sat = self._satellites.get(device)
        if not sat:
            log.warning(f"send_tts: Satellit '{device}' nicht registriert.")
            return
        tts_addr = sat.get("tts_addr", sat["addr"])
        self._send_pcm(TYPE_TTS, pcm_bytes, tts_addr)
        time.sleep(0.3)
        self._send_control({"type": "tts_end", "sample_rate": sample_rate}, tts_addr)
        log.info(f"TTS → {device} ({tts_addr[0]}:{tts_addr[1]}): {len(pcm_bytes)} Bytes @ {sample_rate}Hz gesendet.")

    # ------------------------------------------------------------------
    # Registrierungs-Lookup (für main.py → Raum-Fallback)

    def get_registered_room(self, device: str) -> Optional[str]:
        """Gibt den beim Register gemeldeten Raum zurück, oder None."""
        with self._lock:
            sat = self._satellites.get(device)
        return sat["room"] if sat else None

    def registered_devices(self) -> dict[str, str]:
        """Gibt {device_name: room_name} aller aktuell registrierten Satelliten zurück."""
        with self._lock:
            return {d: s["room"] for d, s in self._satellites.items()}

    def registered_devices_full(self) -> dict[str, dict]:
        """Gibt {device_name: {"room": room, "addr": "ip:port"}} zurück."""
        with self._lock:
            result = {}
            for d, s in self._satellites.items():
                ip, port = s.get("addr", ("", 0))
                result[d] = {"room": s["room"], "addr": f"{ip}:{port}"}
            return result

    # ------------------------------------------------------------------
    # Empfangs-Loop

    def _loop(self):
        while self._running:
            try:
                data, addr = self._sock.recvfrom(_MAX_PACKET)
            except socket.timeout:
                continue
            except OSError:
                break

            if len(data) < 2:
                continue

            msg_type = data[0]
            payload  = data[1:]

            if msg_type == TYPE_CONTROL:
                self._handle_control(payload, addr)
            elif msg_type == TYPE_AUDIO:
                self._handle_audio(payload, addr)
            else:
                log.debug(f"UDP: Unbekannter Type 0x{msg_type:02x} von {addr}")

    # ------------------------------------------------------------------
    # Handler

    def _handle_control(self, payload: bytes, addr: tuple):
        try:
            msg = json.loads(payload.decode("utf-8"))
        except Exception as e:
            log.warning(f"UDP: Ungültiges Control-Paket von {addr}: {e}")
            return

        t      = msg.get("type", "")
        device = msg.get("device", "")

        if t == "register":
            self._upsert_satellite(device)
            room = self._resolve_satellite_room(device) or ""
            if not room:
                log.warning(
                    f"Satellit '{device}' hat keinen Raum in RoomManager — "
                    f"nicht funktional (kein Tracking, keine Weiterleitung an Adapter)."
                )
                return
            # Satellit meldet seinen Empfangsport für TTS; Fallback: Absender-Port
            listen_port = msg.get("listen_port", addr[1])
            tts_addr = (addr[0], listen_port)
            with self._lock:
                self._satellites[device] = {"addr": addr, "tts_addr": tts_addr, "room": room, "last_heartbeat": time.monotonic()}
            log.info(
                f"Satellit registriert: '{device}' "
                f"(Raum: '{room}', Audio von {addr[0]}:{addr[1]}, TTS an :{listen_port})"
            )
            self._send_control({"type": "registered", "ok": True}, addr)
            if self._on_satellite_change:
                snapshot = {d: s["room"] for d, s in self._satellites.items()}
                threading.Thread(
                    target=self._on_satellite_change, args=(snapshot,), daemon=True
                ).start()

        elif t == "audio_end":
            with self._lock:
                session = self._sessions.pop(device, None)
            if session:
                audio = session.get_audio()
                log.info(
                    f"[{device}] Aufnahme abgeschlossen: "
                    f"{len(audio)} Bytes ({len(session.chunks)} Pakete)"
                )
                threading.Thread(
                    target=self._on_audio,
                    args=(device, audio),
                    daemon=True,
                ).start()
            else:
                log.debug(f"[{device}] audio_end ohne laufende Session.")

        elif t == "heartbeat":
            with self._lock:
                if device in self._satellites:
                    self._satellites[device]["addr"] = addr
                    self._satellites[device]["last_heartbeat"] = time.monotonic()
                    self._send_control({"type": "heartbeat_ack", "device": device}, addr)
                    log.info(f"Heartbeat von '{device}' — ACK gesendet")
                else:
                    log.warning(f"Heartbeat von unbekanntem Satellit '{device}' {addr} — sende reregister")
                    self._send_control({"type": "reregister"}, addr)
                    return
            self._upsert_satellite(device)

        else:
            log.debug(f"UDP Control unbekannt: type='{t}' von {addr}")

    def _handle_audio(self, payload: bytes, addr: tuple):
        device = self._find_device_by_ip(addr[0])
        if device is None:
            log.warning(f"UDP: Audio von unbekannter IP {addr[0]} — bitte zuerst registrieren.")
            self._send_control({"type": "reregister"}, addr)
            return

        new_session = False
        with self._lock:
            if device not in self._sessions:
                self._sessions[device] = _AudioSession(device, addr)
                log.debug(f"[{device}] Audio-Session geöffnet.")
                new_session = True
            self._sessions[device].add(payload)

        if new_session and self._on_session_start:
            threading.Thread(
                target=self._on_session_start, args=(device,), daemon=True
            ).start()

    # ------------------------------------------------------------------
    # Sende-Hilfen

    def _send_control(self, msg: dict, addr: tuple):
        if not self._sock:
            return
        data = bytes([TYPE_CONTROL]) + json.dumps(msg, ensure_ascii=False).encode()
        try:
            self._sock.sendto(data, addr)
        except OSError:
            pass

    def _send_pcm(self, type_byte: int, pcm: bytes, addr: tuple):
        """Sendet PCM-Daten in Chunks ≤ 60 KB (UDP-Limit)."""
        if not self._sock:
            return
        chunk_size = 60_000
        offset = 0
        while offset < len(pcm):
            chunk = pcm[offset : offset + chunk_size]
            try:
                self._sock.sendto(bytes([type_byte]) + chunk, addr)
            except OSError:
                return
            offset += chunk_size

    # ------------------------------------------------------------------

    _HEARTBEAT_TIMEOUT = 30.0  # 3 × 10s heartbeat interval

    def _watchdog_loop(self):
        while self._running:
            time.sleep(10.0)
            self._check_heartbeats()

    def _check_heartbeats(self):
        now = time.monotonic()
        timed_out = []
        with self._lock:
            for device in list(self._satellites):
                sat = self._satellites[device]
                if now - sat["last_heartbeat"] > self._HEARTBEAT_TIMEOUT:
                    timed_out.append((device, sat["room"]))
                    del self._satellites[device]
        for device, room in timed_out:
            log.warning(f"Satellit '{device}' (Raum: '{room}') — kein Heartbeat seit {self._HEARTBEAT_TIMEOUT:.0f}s, markiere als offline")
            if self._on_satellite_change:
                with self._lock:
                    snapshot = {d: s["room"] for d, s in self._satellites.items()}
                threading.Thread(
                    target=self._on_satellite_change, args=(snapshot,), daemon=True
                ).start()

    def _find_device_by_ip(self, ip: str) -> Optional[str]:
        """Gibt den Device-Namen für eine IP zurück (erste Übereinstimmung)."""
        with self._lock:
            for device, sat in self._satellites.items():
                if sat["addr"][0] == ip:
                    return device
        return None
