"""
UDP-Transport für Audio-Streaming zwischen Satelliten und Hannah-Server.

Protokoll (ein Port, 1-Byte Type-Prefix):
  0x01 + JSON  = Control-Nachricht  (beide Richtungen)
  0x02 + PCM   = Audio-Daten        (Satellit → Server, raw 16kHz 16-bit mono)
  0x03 + PCM   = TTS-Audio          (Server → Satellit, gleiches Format)

Control-Nachrichten (Satellit → Server):
  {"type": "register",   "device": "rpi-test", "room": "Wohnzimmer"}
  {"type": "audio_end",  "device": "rpi-test"}
  {"type": "heartbeat",  "device": "rpi-test"}

Control-Antworten (Server → Satellit):
  {"type": "registered", "ok": true}
"""

import json
import logging
import socket
import threading
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
    ):
        """
        cfg                 : udp-Abschnitt aus config.yaml
        on_audio            : Callback(device, raw_pcm_bytes) — aufgerufen wenn eine
                              Aufnahme vollständig ist (nach audio_end)
        on_satellite_change : Callback({device: room, ...}) — bei Register/Abmeldung
        """
        self._host = cfg.get("host", "0.0.0.0")
        self._port = cfg.get("port", 7775)
        self._on_audio = on_audio
        self._on_session_start = on_session_start
        self._on_satellite_change = on_satellite_change

        # { device_name: {"addr": (ip, port), "room": str} }
        self._satellites: dict[str, dict] = {}
        # { device_name: _AudioSession }
        self._sessions: dict[str, _AudioSession] = {}
        self._lock = threading.Lock()

        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
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
            room = msg.get("room", "")
            # Satellit meldet seinen Empfangsport für TTS; Fallback: Absender-Port
            listen_port = msg.get("listen_port", addr[1])
            tts_addr = (addr[0], listen_port)
            with self._lock:
                self._satellites[device] = {"addr": addr, "tts_addr": tts_addr, "room": room}
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
                    self._send_control({"type": "heartbeat_ack", "device": device}, addr)
                    log.info(f"Heartbeat von '{device}' — ACK gesendet")
                else:
                    log.warning(f"Heartbeat von unbekanntem Satellit '{device}' {addr} — nicht registriert!")

        else:
            log.debug(f"UDP Control unbekannt: type='{t}' von {addr}")

    def _handle_audio(self, payload: bytes, addr: tuple):
        device = self._find_device_by_ip(addr[0])
        if device is None:
            log.warning(f"UDP: Audio von unbekannter IP {addr[0]} — bitte zuerst registrieren.")
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

    def _find_device_by_ip(self, ip: str) -> Optional[str]:
        """Gibt den Device-Namen für eine IP zurück (erste Übereinstimmung)."""
        with self._lock:
            for device, sat in self._satellites.items():
                if sat["addr"][0] == ip:
                    return device
        return None
