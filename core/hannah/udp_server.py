"""
UDP-Transport für Audio-Streaming zwischen Satelliten und Hannah-Server.

Protokoll (ein Port, 1-Byte Type-Prefix):
  0x01 + JSON  = Control-Nachricht  (beide Richtungen)
  0x02 + PCM   = Audio-Daten        (Satellit → Server, raw 16kHz 16-bit mono)
  0x03 + PCM   = TTS-Audio          (Server → Satellit, gleiches Format)

Control-Nachrichten (Satellit → Server):
  {"type": "register",   "device": "rpi-test", "ip": "192.168.1.50"}  -- Raum kommt aus RoomManager, nicht vom Satelliten; "ip" (eigene LAN-IP, optional) für die Adresse im Adapter hinter einer NAT (#535)
  {"type": "audio_end",  "device": "rpi-test"}
  {"type": "heartbeat",  "device": "rpi-test"}

Control-Antworten (Server → Satellit):
  {"type": "registered", "ok": true}
"""

import ipaddress
import json
import logging
import socket
import threading
import time
from typing import Callable, Optional

from hannah.audio import resample_pcm

log = logging.getLogger(__name__)

TYPE_CONTROL = 0x01
TYPE_AUDIO   = 0x02
TYPE_TTS     = 0x03

# TTS-Pakete: wie der Proxy (ttsChunk in proxy/internal/udp/server.go) unter der WLAN-MTU,
# damit nichts IP-fragmentiert wird. Ein 60-KB-Datagramm (rund 41 Fragmente) kann die
# Firmware nicht zusammensetzen (lwIP: IP_REASS_MAX_PBUFS=10) und passt auch nicht in den
# 32-KB-Ringpuffer des Lautsprechers (#545).
_TTS_CHUNK = 1400
# Die Satelliten-Firmware spielt TTS immer mit dieser Rate (Core rechnet vor dem Senden um, #546).
_TTS_RATE = 16000
# Vorlauf vor der Abspielzeit: der Satellit bekommt die Pakete im Takt der Wiedergabe, mit
# diesem Polster gegen Jitter. Ein Sendevorsprung wie ein Burst würde den Ringpuffer
# überlaufen lassen.
_TTS_LEAD_S = 0.3

# Maximale Größe eines eingehenden UDP-Pakets
_MAX_PACKET = 65535
# Max. Wartezeit auf audio_end nach letztem Audio-Chunk (Sekunden)
_SESSION_TIMEOUT = 10.0


# Adressbereiche, aus denen eine vom Satelliten selbst gemeldete IP akzeptiert wird
# (RFC 1918 + Link-Local). Gleiche Regel wie im Proxy (parseLANIP).
_LAN_NETS = tuple(ipaddress.ip_network(n) for n in
                  ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"))


def parse_lan_ip(raw) -> str:
    """Nimmt die im Register gemeldete eigene IP eines Satelliten nur an, wenn sie eine
    private oder Link-Local-IPv4 ist, sonst "". UDP ist unauthentifiziert: ein Gerät im
    LAN kann jede Adresse behaupten, öffentliche, Loopback-, Multicast- und
    unspezifizierte Adressen werden daher verworfen (#535)."""
    try:
        ip = ipaddress.IPv4Address(raw)
    except (ValueError, TypeError):
        return ""
    return str(ip) if any(ip in net for net in _LAN_NETS) else ""


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
        pair_satellite: Optional[Callable[[str, str], bool]] = None,
        satellite_is_paired: Optional[Callable[[str], bool]] = None,
    ):
        """
        cfg                    : udp-Abschnitt aus config.yaml
        on_audio               : Callback(device, raw_pcm_bytes) — aufgerufen wenn eine
                                 Aufnahme vollständig ist (nach audio_end)
        on_satellite_change    : Callback({device: room, ...}) — bei Register/Abmeldung
        resolve_satellite_room : Callback(device_id) → room_id|None — RoomManager-Lookup.
                                 Ohne zugewiesenen Raum gilt ein Satellit als nicht
                                 funktional: kein Tracking, keine Weiterleitung an den Adapter.
                                 Er bleibt aber verbunden (Register und Heartbeat werden
                                 bestätigt, wie beim Proxy) und wird aufgenommen, sobald ihm
                                 ein Raum zugewiesen ist (#547).
        upsert_satellite       : Callback(device_id) — trägt den Satelliten in die
                                 RoomManager-DB ein (auch ohne Raum), damit er in der
                                 Raum-Zuweisung in der Web UI auftaucht.
        pair_satellite         : Callback(device_id, seed) → bool — koppelt einen vorab
                                 provisionierten Seed an die Hardware-ID (wie über den Proxy).
        satellite_is_paired    : Callback(device_id) → bool — True, wenn der Seed schon
                                 verbraucht wurde (die "paired"-Bestätigung ging verloren).
        """
        self._host = cfg.get("host", "0.0.0.0")
        self._port = cfg.get("port", 7775)
        self._on_audio = on_audio
        self._on_session_start = on_session_start
        self._on_satellite_change = on_satellite_change
        self._resolve_satellite_room = resolve_satellite_room or (lambda *_: None)
        self._upsert_satellite = upsert_satellite or (lambda *_: None)
        self._pair_satellite = pair_satellite or (lambda *_: False)
        self._satellite_is_paired = satellite_is_paired or (lambda *_: False)

        # { device_name: {"addr": (ip, port), "room": str, "tracked": bool, "last_heartbeat": float} }
        # tracked: der Satellit hatte beim Register oder später einen Raum und ist dem Rest von
        # Core (Adapter, Satellitenliste) bekannt; ohne Raum ist er nur verbunden (#547).
        self._satellites: dict[str, dict] = {}
        # { device_name: _AudioSession }
        self._sessions: dict[str, _AudioSession] = {}
        self._lock = threading.Lock()
        # Je Satellit ein Lock, damit zwei Ansagen kurz hintereinander nicht ineinander
        # laufen: die zweite wartet, bis die erste (inkl. tts_end) gesendet ist (#545).
        self._tts_locks: dict[str, threading.Lock] = {}

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
        """Sendet TTS-Audio (raw PCM) an einen registrierten Satelliten.

        Kleine Pakete im Takt der Wiedergabe (siehe _TTS_CHUNK), deshalb dauert das Senden so
        lange wie die Ansage selbst. Es läuft in einem eigenen Thread, damit weder die
        MQTT-Callbacks noch die Pipeline dafür blockieren (#545)."""
        with self._lock:
            sat = self._satellites.get(device)
        if not sat:
            log.warning(f"send_tts: Satellit '{device}' nicht registriert.")
            return
        tts_addr = sat.get("tts_addr", sat["addr"])
        threading.Thread(
            target=self._stream_tts,
            args=(device, pcm_bytes, sample_rate, tts_addr),
            daemon=True,
            name=f"udp-tts-{device}",
        ).start()

    def _stream_tts(self, device: str, pcm_bytes: bytes, sample_rate: int, addr: tuple):
        """Schickt die PCM-Daten in Paketen zu je _TTS_CHUNK Bytes, nach der Abspielzeit
        getaktet (mit _TTS_LEAD_S Vorlauf), danach tts_end."""
        with self._lock:
            lock = self._tts_locks.setdefault(device, threading.Lock())
        with lock:
            if not self._sock:
                return
            if sample_rate != _TTS_RATE:
                # Die Firmware spielt immer mit 16 kHz und ignoriert die Rate im tts_end:
                # Piper (22050 Hz) klänge sonst zu tief und zu langsam (#546)
                pcm_bytes = resample_pcm(pcm_bytes, sample_rate, _TTS_RATE)
                sample_rate = _TTS_RATE
            bytes_per_s = sample_rate * 2  # 16 Bit mono
            start = time.monotonic()
            sent = 0
            for offset in range(0, len(pcm_bytes), _TTS_CHUNK):
                chunk = pcm_bytes[offset : offset + _TTS_CHUNK]
                try:
                    self._sock.sendto(bytes([TYPE_TTS]) + chunk, addr)
                except OSError as e:
                    log.warning(f"TTS → {device}: Senden abgebrochen ({e}).")
                    return
                sent += len(chunk)
                wait = start + sent / bytes_per_s - _TTS_LEAD_S - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
            self._send_control({"type": "tts_end", "sample_rate": sample_rate}, addr)
        log.info(f"TTS → {device} ({addr[0]}:{addr[1]}): {len(pcm_bytes)} Bytes @ {sample_rate}Hz gesendet.")

    # ------------------------------------------------------------------
    # Registrierungs-Lookup (für main.py → Raum-Fallback)

    def get_registered_room(self, device: str) -> Optional[str]:
        """Gibt den beim Register gemeldeten Raum zurück, oder None."""
        with self._lock:
            sat = self._satellites.get(device)
        return (sat["room"] or None) if sat else None

    def _live_room(self, device: str) -> str:
        """Aktuelle Raum-Zuweisung eines Satelliten aus der DB (#519). Der beim Register
        gespeicherte Raum ist nur ein Schnappschuss und wird bei einer Raumänderung nicht
        nachgezogen; ohne Zuweisung ist der Satellit raumlos ("")."""
        return self._resolve_satellite_room(device) or ""

    def registered_devices(self) -> dict[str, str]:
        """Gibt {device_name: room_id} aller aktuell registrierten Satelliten zurück.
        Der Raum ist die aktuelle DB-Zuweisung, nicht der Stand beim Register (#519).
        Ein Satellit, der noch nie einen Raum hatte, ist nur verbunden und taucht hier nicht
        auf (#547)."""
        with self._lock:
            devices = [d for d, s in self._satellites.items() if s.get("tracked", True)]
        return {d: self._live_room(d) for d in devices}

    def registered_devices_full(self) -> dict[str, dict]:
        """Gibt {device_name: {"room": room_id, "addr": "ip:port"}} zurück (Raum: aktuelle
        DB-Zuweisung, #519). Satelliten ohne je zugewiesenen Raum fehlen, wie bei registered_devices()."""
        with self._lock:
            addrs = {d: (s.get("lan_ip") or s.get("addr", ("", 0))[0], s.get("addr", ("", 0))[1])
                     for d, s in self._satellites.items() if s.get("tracked", True)}
        return {d: {"room": self._live_room(d), "addr": f"{ip}:{port}"} for d, (ip, port) in addrs.items()}

    def _pair_from_register(self, device: str, seed: str) -> bool:
        """Koppelt einen vorab provisionierten Seed an die Hardware-ID, wie NotifySatelliteRegistered
        über den Proxy. True, wenn der Satellit ein "paired" bekommen soll (damit er den Seed aus dem
        NVS löscht). Ist der Seed schon verbraucht (die Bestätigung ging verloren), gilt das als
        Erfolg und die Bestätigung geht erneut raus, sonst schickt der Satellit den Seed bei jedem
        Neustart wieder mit (#547)."""
        if not seed:
            return False
        try:
            if self._pair_satellite(device, seed):
                log.info(f"Satellit '{device}' gekoppelt (Seed eingelöst)")
                return True
            if self._satellite_is_paired(device):
                log.info(f"Satellit '{device}': Seed schon eingelöst, sende die Bestätigung erneut")
                return True
            log.warning(f"Satellit '{device}': Seed nicht gefunden, weiter ohne Kopplung")
        except Exception as e:  # DB-Fehler dürfen die Registrierung nicht verhindern
            log.error(f"Satellit '{device}': Kopplung fehlgeschlagen: {e}")
        return False

    def _track_if_room_assigned(self, device: str):
        """Nimmt einen bisher raumlosen Satelliten auf, sobald ihm ein Raum zugewiesen ist,
        ohne dass er neu starten muss (#547)."""
        room = self._live_room(device)
        if not room:
            return
        with self._lock:
            sat = self._satellites.get(device)
            if not sat or sat.get("tracked", True):
                return
            sat["tracked"] = True
            sat["room"] = room
        log.info(f"Satellit '{device}' hat jetzt den Raum '{room}' — wird verfolgt")
        if self._on_satellite_change:
            snapshot = self.registered_devices()
            threading.Thread(
                target=self._on_satellite_change, args=(snapshot,), daemon=True
            ).start()

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
            paired = self._pair_from_register(device, msg.get("seed", ""))
            room = self._resolve_satellite_room(device) or ""
            # TTS geht an die gesehene Absenderadresse (IP + Port), nicht an Absender-IP +
            # gemeldeten listen_port: hinter einer NAT (z.B. Docker-Bridge auf Synology)
            # ist die gesehene IP das NAT-Gateway, und nur der von der NAT vergebene Port
            # führt zurück zum Satelliten (#351). Alle Satelliten senden über ihren
            # Listen-Socket, ohne NAT ist beides ohnehin identisch.
            # Eigene LAN-IP des Satelliten (#535): nur für die Adresse, die der Adapter sieht
            # (Konfigurieren, NVS-Updates). Hinter einer NAT ist die gesehene IP das Gateway.
            # Audio und TTS laufen weiter über die gesehene Absenderadresse.
            reported = msg.get("ip", "")
            lan_ip = parse_lan_ip(reported)
            if reported and not lan_ip:
                log.warning(
                    f"Satellit '{device}': gemeldete IP {reported!r} ignoriert "
                    f"(keine private/Link-Local-IPv4), Absender {addr[0]}"
                )
            listen_port = msg.get("listen_port")
            if listen_port is not None and listen_port != addr[1]:
                log.info(
                    f"Satellit '{device}': Absender-Port {addr[1]} ≠ listen_port {listen_port} "
                    f"(NAT?) — antworte an Absenderadresse"
                )
            with self._lock:
                self._satellites[device] = {
                    "addr": addr, "tts_addr": addr, "room": room, "tracked": bool(room),
                    "lan_ip": lan_ip, "last_heartbeat": time.monotonic(),
                }
            if room:
                log.info(
                    f"Satellit registriert: '{device}' "
                    f"(Raum: '{room}', Adresse {addr[0]}:{addr[1]})"
                )
            else:
                # Wie beim Proxy: verbunden und bestätigt (sonst fordert jeder Heartbeat eine
                # neue Registrierung an, alle 10 s), aber nicht funktional, bis ein Raum
                # zugewiesen ist (#547).
                log.warning(
                    f"Satellit '{device}' hat keinen Raum in RoomManager — verbunden, aber nicht "
                    f"funktional (kein Tracking, keine Weiterleitung an Adapter). Weise ihm in der "
                    f"WebUI oder im Adapter einen Raum zu."
                )
            self._send_control({"type": "registered", "ok": True}, addr)
            if paired:
                self._send_control({"type": "paired", "device": device}, addr)
            if room and self._on_satellite_change:
                snapshot = self.registered_devices()
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
                    self._satellites[device]["tts_addr"] = addr  # NAT-Neuzuordnung folgen (#351)
                    self._satellites[device]["last_heartbeat"] = time.monotonic()
                    was_tracked = self._satellites[device].get("tracked", True)
                    self._send_control({"type": "heartbeat_ack", "device": device}, addr)
                    log.info(f"Heartbeat von '{device}' — ACK gesendet")
                else:
                    log.warning(f"Heartbeat von unbekanntem Satellit '{device}' {addr} — sende reregister")
                    self._send_control({"type": "reregister"}, addr)
                    return
            self._upsert_satellite(device)
            if not was_tracked:
                self._track_if_room_assigned(device)

        else:
            log.debug(f"UDP Control unbekannt: type='{t}' von {addr}")

    def _handle_audio(self, payload: bytes, addr: tuple):
        device = self._find_device_by_addr(addr)
        if device is None:
            log.warning(f"UDP: Audio von unbekannter Adresse {addr[0]}:{addr[1]} — bitte zuerst registrieren.")
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
                    timed_out.append((device, sat["room"], sat.get("tracked", True)))
                    del self._satellites[device]
        for device, room, tracked in timed_out:
            log.warning(f"Satellit '{device}' (Raum: '{room}') — kein Heartbeat seit {self._HEARTBEAT_TIMEOUT:.0f}s, markiere als offline")
            # Ein raumloser Satellit war dem Rest von Core nie bekannt, also gibt es nichts zu melden
            if tracked and self._on_satellite_change:
                snapshot = self.registered_devices()
                threading.Thread(
                    target=self._on_satellite_change, args=(snapshot,), daemon=True
                ).start()

    def _find_device_by_addr(self, addr: tuple) -> Optional[str]:
        """Gibt den Device-Namen für eine Absenderadresse zurück. Exakter Treffer
        (IP + Port) zuerst — hinter einer NAT teilen sich alle Satelliten die
        Gateway-IP, nur der Port unterscheidet sie (#351). Fallback auf reinen
        IP-Vergleich nur, wenn die IP eindeutig ist."""
        with self._lock:
            ip_matches = []
            for device, sat in self._satellites.items():
                if sat["addr"][0] == addr[0]:
                    if sat["addr"][1] == addr[1]:
                        return device
                    ip_matches.append(device)
        return ip_matches[0] if len(ip_matches) == 1 else None
