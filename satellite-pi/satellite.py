#!/usr/bin/env python3
"""
Hannah-Satellit — Raspberry Pi Variante

Ablauf:
  1. Hannah-Server via MQTT-Discovery oder konfigurierter IP finden
  2. Registrierung bei Hannah (Name, Raum, eigene IP)
  3. Dauerschleife: Wake-Word lauschen (OpenWakeWord)
  4. Nach Wake-Word: Mikrofon-PCM per UDP streamen + VAD-Stille-Erkennung
  5. audio_end senden wenn Stille erkannt oder Max-Dauer erreicht
  6. Eingehende TTS-Pakete empfangen und über Lautsprecher abspielen

Protokoll:  Datei hannah/udp_server.py (Typ-Bytes 0x01 / 0x02 / 0x03)

Wake-Word:
  pip install openwakeword
  # Vortrainierte Modelle werden beim ersten Start automatisch heruntergeladen.
  # Eigene Modelle (ONNX) können mit --wakeword-model angegeben werden.
  # Standard-Modell: "hey_jarvis" (englisch) oder ein eigenes deutsches Modell.
"""

import json
import logging
import math
import socket
import struct
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

import miniaudio
import numpy as np
import pyaudio

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("satelite")

# ── Protokoll-Konstanten (müssen mit hannah/udp_server.py übereinstimmen) ────
TYPE_CONTROL = 0x01
TYPE_AUDIO   = 0x02
TYPE_TTS     = 0x03

# ── Konfiguration ─────────────────────────────────────────────────────────────

@dataclass
class Config:
    # Geräte-Identität
    device_name: str   = "rpi-test"
    room:        str   = "Wohnzimmer"

    # Hannah-Server — wird via MQTT-Discovery ermittelt, kann aber auch fix gesetzt werden
    hannah_host: str   = ""       # leer = MQTT-Discovery
    hannah_port: int   = 7775

    # MQTT-Discovery
    mqtt_broker:        str = "192.168.8.1"
    mqtt_port:          int = 1883
    mqtt_user:          str = ""
    mqtt_pass:          str = ""
    discovery_topic:    str = "hannah/server"
    discovery_timeout:  float = 5.0

    # Wake-Word (OpenWakeWord)
    # Leer = vortrainiertes "hey_jarvis"-Modell; Pfad zu eigener .onnx-Datei möglich
    wakeword_model:      str   = ""
    wakeword_score:      float = 0.7   # Erkennungsschwelle (0.0–1.0)
    inference_framework: str   = "onnx"  # "onnx" (x86/aarch64) oder "tflite" (RPi 3B armv7l)

    # Audio
    mic_device_index: Optional[int] = None   # None = System-Standard
    sample_rate:      int  = 16000   # Aufnahme-Rate des Mikrofons (z.B. 44100 oder 48000)
    # Hannah und OpenWakeWord erwarten 16kHz — bei abweichender sample_rate wird resampelt
    max_record_secs:  int  = 8       # Maximale Aufnahmedauer
    min_record_secs:  float = 0.8    # Mindestaufnahme — keine Stille-Erkennung in dieser Zeit
    silence_secs:     float = 0.8    # Stille → Aufnahme beenden
    silence_threshold: int = 300     # RMS-Schwellwert (0–32768) für Stille

    # TTS-Wiedergabe
    tts_device_index: Optional[int] = None   # None = System-Standard
    tts_sample_rate:  int  = 44100            # Native Rate des Lautsprechers
    # Tipp: RPi-Klinke → 44100, USB-Audio-Adapter → 48000

    # UDP-Empfangsport des Satelliten (für TTS-Pakete von Hannah / send_wav.py)
    listen_port: int = 7776

    # Optionaler Zuhör-Ton als WAV-Datei (leer = synthetisierter Ton)
    pling_sound: str = ""

    # Anzahl der 80ms-Chunks die vor dem Wake-Word gepuffert werden (Pre-Buffer).
    # Diese Chunks werden der Aufnahme vorangestellt, damit Sprache die während der
    # OWW-Inferenz aufgenommen wurde nicht verloren geht (Standard: ~400ms = 5 Chunks).
    pre_buffer_chunks: int = 5

    # Optionale LED-Steuerung (Raspberry Pi GPIO, 0 = deaktiviert)
    led_pin: int = 0

    # Heartbeat & Registration Timeouts
    registration_timeout: float = 5.0      # max. Wartezeit auf "registered" ACK
    heartbeat_interval: int = 10           # Heartbeat alle 10 Sekunden senden
    heartbeat_timeout: int = 3             # max. fehlgeschlagene Heartbeats vor Restart
    max_heartbeat_wait: float = 15.0       # max. Zeit auf heartbeat_ack zu warten

    # Restart-Backoff
    backoff_base: int = 30                 # Basis-Intervall Sekunden
    max_backoff: int = 300                 # max. Backoff (5 Minuten)

# ── armv7l-Kompatibilität: openwakeword-Stubs ─────────────────────────────────

def _apply_tflite_stubs():
    """
    Injiziert minimale Stubs für Pakete die openwakeword auf Modulebene importiert,
    aber auf armv7l (RPi 3B) nicht verfügbar sind:
      - onnxruntime        (vad.py)                → kein armv7l-Wheel
      - custom_verifier_model (openwakeword/__init__)→ benötigt sklearn (Training)
    Muss VOR dem ersten Import von openwakeword aufgerufen werden.
    """
    import types as _types
    if "onnxruntime" not in sys.modules:
        class _FakeSession:
            def run(self, output_names, input_dict):
                return [np.array([[1.0]])]
        _ort = _types.ModuleType("onnxruntime")
        _ort.InferenceSession = lambda *a, **kw: _FakeSession()
        sys.modules["onnxruntime"] = _ort
        log.debug("onnxruntime-Stub injiziert (tflite-Modus).")
    if "openwakeword.custom_verifier_model" not in sys.modules:
        _cvm = _types.ModuleType("openwakeword.custom_verifier_model")
        _cvm.train_custom_verifier = lambda *a, **kw: None
        sys.modules["openwakeword.custom_verifier_model"] = _cvm
        log.debug("custom_verifier_model-Stub injiziert (kein sklearn nötig).")


# ── MQTT-Client (Discovery + persistente Verbindung) ─────────────────────────

class MQTTLink:
    """
    Persistente MQTT-Verbindung des Satelliten.
    Aufgaben:
      - Discovery: Hannah's UDP-Adresse aus retained Topic lesen
      - Status publizieren: hannah/satelite/{device}/status (retained)
      - Online/Offline via LWT: hannah/satelite/{device}/online
      - Commands empfangen: hannah/satelite/{device}/command (zukünftig)
    """

    def __init__(self, cfg: Config, on_command=None, on_server_changed=None):
        import paho.mqtt.client as mqtt

        self._cfg               = cfg
        self._on_command        = on_command
        self._on_server_changed = on_server_changed
        self._hannah_addr: Optional[tuple[str, int]] = None
        self._discovered = threading.Event()

        self._topic_online  = f"hannah/satelite/{cfg.device_name}/online"
        self._topic_status  = f"hannah/satelite/{cfg.device_name}/status"
        self._topic_command = f"hannah/satelite/{cfg.device_name}/command"

        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

        # LWT: Broker publiziert "false" wenn Satellit die Verbindung verliert
        self._client.will_set(self._topic_online, "false", qos=1, retain=True)

        if cfg.mqtt_user:
            self._client.username_pw_set(cfg.mqtt_user, cfg.mqtt_pass or None)

    def connect(self) -> tuple[str, int]:
        """Verbindet, wartet auf Discovery, gibt (host, port) zurück."""
        try:
            self._client.connect(self._cfg.mqtt_broker, self._cfg.mqtt_port, keepalive=30)
            self._client.loop_start()
        except Exception as e:
            log.error(f"MQTT-Verbindung fehlgeschlagen: {e}")
            sys.exit(1)

        if not self._discovered.wait(timeout=self._cfg.discovery_timeout):
            log.error(
                f"Hannah nicht via MQTT-Discovery gefunden "
                f"(Topic: '{self._cfg.discovery_topic}', Broker: {self._cfg.mqtt_broker})"
            )
            sys.exit(1)

        return self._hannah_addr

    def publish_status(self, state: str):
        """Publiziert den Satellit-Status (retained)."""
        self._client.publish(self._topic_status, state, qos=1, retain=True)
        log.debug(f"MQTT Status: {state}")

    def disconnect(self):
        self._client.publish(self._topic_online, "false", qos=1, retain=True)
        self._client.loop_stop()
        self._client.disconnect()

    # ------------------------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            log.error(f"MQTT Verbindungsfehler: {reason_code}")
            return
        client.subscribe(self._cfg.discovery_topic, qos=1)
        client.subscribe(self._topic_command, qos=1)
        # Online-Status setzen
        client.publish(self._topic_online, "true", qos=1, retain=True)
        log.info(f"MQTT verbunden ({self._cfg.mqtt_broker}:{self._cfg.mqtt_port})")

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        if topic == self._cfg.discovery_topic:
            try:
                data = json.loads(msg.payload.decode())
                host = data["host"]
                port = int(data["port"])
                if not self._discovered.is_set():
                    self._hannah_addr = (host, port)
                    log.info(f"Hannah via MQTT-Discovery gefunden: {host}:{port}")
                    self._discovered.set()
                elif (host, port) != self._hannah_addr:
                    log.info(f"Hannah-Adresse geändert: {self._hannah_addr} → {host}:{port}")
                    self._hannah_addr = (host, port)
                    if self._on_server_changed:
                        self._on_server_changed(host, port)
            except Exception as e:
                log.warning(f"Discovery: ungültige Nachricht: {e}")
        elif topic == self._topic_command:
            cmd = msg.payload.decode("utf-8", errors="replace").strip()
            log.info(f"Command empfangen: {cmd!r}")
            if self._on_command:
                self._on_command(cmd)

# ── Satellit ──────────────────────────────────────────────────────────────────

class Satellite:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._mqtt = MQTTLink(cfg, on_server_changed=self._on_server_changed)
        self._hannah_addr: tuple[str, int] = ("", 0)
        self._sock: Optional[socket.socket] = None
        self._pa: Optional[pyaudio.PyAudio] = None
        self._out_stream = None  # persistenter Ausgabe-Stream — einmalig öffnen, kein pa.open()-Delay
        self._oww = None  # openwakeword.model.Model — lazy-imported in _init_audio
        self._tts_thread: Optional[threading.Thread] = None
        self._running = False
        self._pling_pcm: Optional[bytes] = None   # bereits auf tts_sample_rate normalisiert, Stereo
        # Pling-Datei vorladen falls konfiguriert; synthetisierter Fallback wird
        # nach _init_audio() in _precompute_pling() berechnet (miniaudio nötig)
        if cfg.pling_sound:
            self._pling_pcm = self._load_audio(cfg.pling_sound, cfg.tts_sample_rate)

        # Heartbeat & Registration State
        self._registered = threading.Event()      # Signal: "registered" ACK empfangen
        self._heartbeat_ack_received = threading.Event()  # Signal: heartbeat_ack empfangen
        self._heartbeat_failures = 0              # Zähler: fehlgeschlagene Heartbeats
        self._backoff_level = 0                   # Zähler für Backoff-Stufe
        self._heartbeat_thread: Optional[threading.Thread] = None
        # Wenn gesetzt: TTS-Receiver verwirft alle eingehenden Chunks (alte Session)
        self._tts_discard = threading.Event()

        # LED-Steuerung (optional, nur wenn led_pin > 0 und RPi.GPIO verfügbar)
        self._gpio = None
        if cfg.led_pin:
            try:
                import RPi.GPIO as GPIO
                GPIO.setmode(GPIO.BCM)
                GPIO.setup(cfg.led_pin, GPIO.OUT, initial=GPIO.LOW)
                self._gpio = GPIO
                log.info(f"LED-Steuerung aktiv: GPIO {cfg.led_pin}")
            except ImportError:
                log.debug("RPi.GPIO nicht verfügbar — LED-Steuerung deaktiviert.")

    # ------------------------------------------------------------------

    def run(self):
        self._running = True
        self._resolve_hannah_address()
        self._open_socket()
        self._init_audio()
        self._precompute_pling()
        self._start_tts_receiver()

        # Registrierung mit ACK-Handling
        if not self._register():
            log.error("Satellit konnte sich nicht registrieren. Initiiere Restart...")
            self._trigger_restart_with_backoff(reason="Registrierungs-Timeout")
            return

        # Heartbeat-Thread starten
        self._start_heartbeat_thread()

        log.info(f"Satellit '{self.cfg.device_name}' bereit. Lausche auf Wake-Word ...")
        try:
            self._wake_word_loop()
        except KeyboardInterrupt:
            log.info("Beende Satellit ...")
        finally:
            self._shutdown()

    # ------------------------------------------------------------------
    # Setup

    def _resolve_hannah_address(self):
        if self.cfg.hannah_host:
            self._hannah_addr = (self.cfg.hannah_host, self.cfg.hannah_port)
            log.info(f"Hannah-Adresse aus Config: {self._hannah_addr[0]}:{self._hannah_addr[1]}")
            # MQTT trotzdem verbinden (für Status/LWT)
            self._mqtt.connect()
        else:
            self._hannah_addr = self._mqtt.connect()

    def _on_server_changed(self, host: str, port: int):
        """
        Wird gerufen wenn das MQTT-Discovery-Topic eine neue Adresse meldet
        (z.B. wenn ein Proxy startet oder sich abmeldet).
        Aktualisiert die Zieladresse und re-registriert beim neuen Endpunkt.
        """
        self._hannah_addr = (host, port)
        self._heartbeat_failures = 0
        log.info(f"Server-Adresse geändert → {host}:{port} — Re-Registrierung ...")
        threading.Thread(target=self._reregister, daemon=True, name="reregister").start()

    def _reregister(self):
        """Sendet erneute Registrierung nach Adressänderung."""
        if not self._register():
            log.error("Re-Registrierung fehlgeschlagen.")

    def _open_socket(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(0.1)
        self._sock.bind(("0.0.0.0", self.cfg.listen_port))
        log.info(f"UDP-Socket lauscht auf Port {self.cfg.listen_port}")

    def _init_audio(self):
        self._pa = pyaudio.PyAudio()
        framework  = self.cfg.inference_framework

        if framework == "tflite":
            _apply_tflite_stubs()

        from openwakeword.model import Model as WakeWordModel
        model_path = self.cfg.wakeword_model or None
        if model_path:
            model_path = self._resolve_model_path(model_path)
            self._oww = WakeWordModel(wakeword_models=[model_path], inference_framework=framework)
            log.info(f"OpenWakeWord: Modell '{model_path}' geladen ({framework}).")
        else:
            self._oww = WakeWordModel(inference_framework=framework)
            log.info(f"OpenWakeWord: Standard-Modell geladen ({framework}).")

        # Ausgabe-Stream einmalig öffnen — pa.open() auf dem Pi dauert 200-500ms,
        # was sonst als Verzögerung zwischen Wake-Word und Pling wahrnehmbar ist.
        # frames_per_buffer=1024 statt 4096: reduziert Puffer-Latenz von ~93ms auf ~23ms
        # (bei 44100Hz), sodass der Pling-Ton schneller aus dem Lautsprecher kommt.
        self._out_stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=2,
            rate=self.cfg.tts_sample_rate,
            output=True,
            output_device_index=self.cfg.tts_device_index,
            frames_per_buffer=1024,
        )
        log.info("Ausgabe-Stream bereit.")

    @staticmethod
    def _resolve_model_path(model: str) -> str:
        """
        Löst einen Modell-Namen oder -Pfad auf.
        - Absoluter/relativer Pfad: wird direkt verwendet
        - Nur Dateiname (kein /): wird im openwakeword-Ressourcenverzeichnis gesucht
        """
        import os
        if os.sep in model or "/" in model:
            return model  # bereits ein Pfad
        # Suche im openwakeword-Paketverzeichnis
        try:
            import openwakeword
            resources = os.path.join(os.path.dirname(openwakeword.__file__), "resources", "models")
            candidate = os.path.join(resources, model)
            if os.path.isfile(candidate):
                log.debug(f"Modell gefunden: {candidate}")
                return candidate
        except Exception:
            pass
        # Datei im aktuellen Verzeichnis oder absoluter Pfad — openwakeword selbst entscheidet
        return model

    def _precompute_pling(self):
        """Vorberechnung des synthetisierten Pling-Tons — kein Delay bei Wake-Word."""
        if not self._pling_pcm:
            raw, rate = self._synthesize_pling()
            self._pling_pcm = self._normalize_for_playback(raw, rate, channels=1)
            log.debug(f"Pling vorberechnet: {len(self._pling_pcm)} Bytes @ {self.cfg.tts_sample_rate}Hz")

    def _register(self):
        msg = {
            "type":        "register",
            "device":      self.cfg.device_name,
            "room":        self.cfg.room,
            "listen_port": self.cfg.listen_port,
        }
        self._send_control(msg)
        log.info(
            f"Registrierung gesendet an {self._hannah_addr[0]}:{self._hannah_addr[1]} "
            f"(Gerät: '{self.cfg.device_name}', Raum: '{self.cfg.room}')"
        )

        # Warte auf "registered" ACK
        self._registered.clear()
        if self._registered.wait(timeout=self.cfg.registration_timeout):
            log.info("Registrierung bestätigt (ACK empfangen).")
            return True
        else:
            log.error(
                f"Registrierungs-Timeout nach {self.cfg.registration_timeout}s — "
                f"führe Restart durch!"
            )
            return False

    def _start_heartbeat_thread(self):
        """Startet Heartbeat-Sender im Hintergrund."""
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=False, name="heartbeat"
        )
        self._heartbeat_thread.start()
        log.info(f"Heartbeat-Thread gestartet (Interval: {self.cfg.heartbeat_interval}s)")

    def _heartbeat_loop(self):
        """Sendet periodisch Heartbeat, detektiert Timeouts, triggert Restart."""
        import time
        while self._running:
            try:
                time.sleep(self.cfg.heartbeat_interval)
                if not self._running:
                    break

                # clear VOR send — sonst race condition: ACK kommt auf LAN in <1ms
                # zurück, setzt das Event, dann erst clear() → ACK geht verloren
                self._heartbeat_ack_received.clear()
                self._send_control({"type": "heartbeat", "device": self.cfg.device_name})
                log.debug("Heartbeat gesendet")

                if self._heartbeat_ack_received.wait(timeout=self.cfg.max_heartbeat_wait):
                    self._heartbeat_failures = 0
                    log.debug("Heartbeat-ACK OK")
                else:
                    self._heartbeat_failures += 1
                    log.warning(
                        f"Heartbeat-ACK nicht empfangen ({self._heartbeat_failures}/{self.cfg.heartbeat_timeout})"
                    )
                    if self._heartbeat_failures >= self.cfg.heartbeat_timeout:
                        self._trigger_restart_with_backoff(
                            reason=f"Heartbeat-Timeout ({self._heartbeat_failures} Fehler)"
                        )
                        return
            except Exception as e:
                log.error(f"Heartbeat-Loop Error: {e}")
                time.sleep(self.cfg.heartbeat_interval)

    def _trigger_restart_with_backoff(self, reason: str):
        """Restart mit exponentiellem Backoff: 0s → 30s → 60s → 90s → ..."""
        import time
        import os

        backoff_seconds = min(self._backoff_level * self.cfg.backoff_base, self.cfg.max_backoff)
        self._backoff_level += 1

        log.error(f"Restart: {reason} | Backoff-Level: {self._backoff_level} | Wartezeit: {backoff_seconds}s")

        self._running = False
        self._mqtt.publish_status("offline")
        time.sleep(0.5)  # Clean-up Time
        time.sleep(backoff_seconds)

        log.warning(f"[RESTART] Führe 'systemctl restart hannah-satelite.service' aus...")
        try:
            os.system("sudo systemctl restart hannah-satelite.service")
        except Exception as e:
            log.error(f"Restart fehlgeschlagen: {e}")

    # ------------------------------------------------------------------
    # TTS-Empfänger (Hintergrund-Thread)

    def _start_tts_receiver(self):
        self._tts_thread = threading.Thread(
            target=self._tts_receiver_loop, daemon=True, name="tts-rx"
        )
        self._tts_thread.start()

    def _tts_receiver_loop(self):
        tts_chunks: list[bytes] = []
        while self._running:
            try:
                data, _ = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break

            if not data:
                continue

            msg_type = data[0]
            payload  = data[1:]

            if msg_type == TYPE_TTS and payload:
                if self._tts_discard.is_set():
                    tts_chunks.clear()  # alte Session — verwerfen
                else:
                    tts_chunks.append(payload)
            elif msg_type == TYPE_CONTROL:
                try:
                    msg = json.loads(payload.decode("utf-8"))
                    log.debug(f"Control empfangen: {msg}")
                    msg_t = msg.get("type")
                    if msg_t == "heartbeat_ack":
                        log.debug("Heartbeat-ACK empfangen")
                        self._heartbeat_ack_received.set()
                    elif msg_t == "reregister":
                        log.warning("Core fordert Re-Registrierung (nach Neustart?)")
                        threading.Thread(target=self._reregister, daemon=True, name="reregister").start()
                    elif msg_t == "registered":
                        ok = msg.get("ok", False)
                        if ok:
                            log.info("Registrierungs-ACK empfangen ✓")
                            self._registered.set()
                        else:
                            log.error("Registrierung abgelehnt!")
                    elif msg_t == "tts_end" and tts_chunks:
                        rate = msg.get("sample_rate", self.cfg.sample_rate)
                        self._play_tts(b"".join(tts_chunks), sample_rate=rate)
                        tts_chunks.clear()
                    elif msg_t == "status":
                        self._on_status(msg.get("state", ""))
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Status-Handler (Hannah → Satellit)

    # LED-Blink-Muster: state → (on_ms, off_ms) oder None für Dauerlicht/aus
    _LED_PATTERNS: dict[str, Optional[tuple[int, int]]] = {
        "idle":       None,          # aus
        "listening":  None,          # dauerhaft an
        "processing": (100, 100),    # schnelles Blinken
        "speaking":   (400, 200),    # langsames Blinken
    }

    def _on_status(self, state: str):
        log.info(f"Status: {state}")
        self._mqtt.publish_status(state)
        if not self._gpio:
            return
        pattern = self._LED_PATTERNS.get(state)
        if state == "idle":
            self._led_stop()
            self._set_led(False)
        elif state == "listening":
            self._led_stop()
            self._set_led(True)
        elif pattern:
            self._led_blink(*pattern)

    def _set_led(self, on: bool):
        if self._gpio:
            self._gpio.output(self.cfg.led_pin, self._gpio.HIGH if on else self._gpio.LOW)

    _led_blink_stop = threading.Event()

    def _led_stop(self):
        self._led_blink_stop.set()

    def _led_blink(self, on_ms: int, off_ms: int):
        """Startet ein Blink-Muster in einem Hintergrund-Thread."""
        self._led_blink_stop.set()   # laufenden Blink-Thread stoppen
        self._led_blink_stop = threading.Event()
        stop = self._led_blink_stop

        def _blink():
            while not stop.is_set():
                self._set_led(True)
                if stop.wait(on_ms / 1000):
                    break
                self._set_led(False)
                stop.wait(off_ms / 1000)
            self._set_led(False)

        threading.Thread(target=_blink, daemon=True, name="led-blink").start()

    def _play_pling(self):
        """Spielt den Zuhör-Ton — Datei wenn konfiguriert, sonst synthetisiert."""
        if self._pling_pcm:
            pcm = self._pling_pcm
        else:
            raw, src_rate = self._synthesize_pling()
            pcm = self._normalize_for_playback(raw, src_rate, channels=1)
        self._play_audio(pcm)

    @staticmethod
    def _synthesize_pling() -> tuple[bytes, int]:
        """Fallback: synthetisierter Sweep-Ton (Mono 16kHz)."""
        rate     = 16000
        duration = 0.18
        n        = int(rate * duration)
        samples  = []
        for i in range(n):
            t    = i / rate
            freq = 880 + (1320 - 880) * (i / n)
            fade = math.sin(math.pi * i / n)
            val  = int(32767 * 0.6 * fade * math.sin(2 * math.pi * freq * t))
            samples.append(max(-32768, min(32767, val)))
        return struct.pack(f"{n}h", *samples), rate

    def _load_audio(self, path: str, target_rate: int) -> Optional[bytes]:
        """
        Lädt eine Audiodatei (MP3, WAV, OGG, FLAC) und gibt normalisiertes
        Stereo-PCM bei target_rate zurück. Gibt None bei Fehler zurück.
        """
        try:
            decoded = miniaudio.decode_file(
                path,
                output_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=2,
                sample_rate=target_rate,
            )
            pcm = bytes(decoded.samples)
            log.info(f"Audio geladen: {path} → {target_rate}Hz Stereo, {len(pcm)} Bytes")
            return pcm
        except Exception as e:
            log.warning(f"Audio '{path}' nicht ladbar: {e} — nutze synthetisierten Ton.")
            return None

    def _normalize_for_playback(self, pcm: bytes, src_rate: int, channels: int) -> bytes:
        """Konvertiert PCM auf Ziel-Rate und Stereo via miniaudio (hohe Qualität)."""
        import io, wave
        play_rate = self.cfg.tts_sample_rate

        # PCM in WAV-Container verpacken, damit miniaudio es dekodieren kann
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(2)
            wf.setframerate(src_rate)
            wf.writeframes(pcm)
        buf.seek(0)

        # miniaudio übernimmt Resampling + Mono→Stereo in einem Schritt
        decoded = miniaudio.decode(
            buf.read(),
            output_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=2,
            sample_rate=play_rate,
        )
        return bytes(decoded.samples)

    def _play_tts(self, pcm: bytes, sample_rate: Optional[int] = None):
        """Spielt raw Mono-PCM von Hannah — normalisiert auf Stereo/tts_sample_rate."""
        if not pcm:
            return
        src_rate = sample_rate or self._OWW_RATE
        pcm = self._normalize_for_playback(pcm, src_rate, channels=1)
        log.info(f"TTS: {len(pcm)} Bytes @ {self.cfg.tts_sample_rate}Hz Stereo abspielen ...")
        self._play_audio(pcm)

    # Bytes pro Frame bei 16-bit Stereo
    _BYTES_PER_FRAME = 4   # 2 channels × 2 bytes
    _CHUNK_FRAMES    = 4096

    def _play_audio(self, pcm: bytes):
        """Schreibt PCM in den persistenten Ausgabe-Stream (kein open()-Overhead)."""
        chunk_bytes = self._CHUNK_FRAMES * self._BYTES_PER_FRAME
        try:
            offset = 0
            while offset < len(pcm):
                chunk = pcm[offset : offset + chunk_bytes]
                rem = len(chunk) % self._BYTES_PER_FRAME
                if rem:
                    chunk += b"\x00" * (self._BYTES_PER_FRAME - rem)
                self._out_stream.write(chunk)
                offset += chunk_bytes
        except Exception as e:
            log.error(f"Audio-Wiedergabe fehlgeschlagen: {e}")

    # ------------------------------------------------------------------
    # Wake-Word-Schleife

    # OpenWakeWord erwartet Chunks von 1280 Samples (80ms @ 16kHz)
    _OWW_RATE  = 16000
    _OWW_CHUNK = 1280  # Samples bei 16kHz

    @staticmethod
    def _resample(data: np.ndarray, src_rate: int, dst_rate: int = 16000) -> np.ndarray:
        """Lineares Resampling ohne externe Abhängigkeiten (beide Richtungen)."""
        if src_rate == dst_rate:
            return data
        target_len = int(len(data) * dst_rate / src_rate)
        return np.interp(
            np.linspace(0, len(data), target_len),
            np.arange(len(data)),
            data,
        ).astype(np.int16)

    def _wake_word_loop(self):
        src_rate = self.cfg.sample_rate
        # Chunk-Größe in Mikrofon-Samples so wählen dass ~80ms pro Frame eingelesen werden
        mic_chunk = int(self._OWW_CHUNK * src_rate / self._OWW_RATE)

        stream = self._pa.open(
            rate=src_rate,
            channels=1,
            format=pyaudio.paInt16,
            input=True,
            input_device_index=self.cfg.mic_device_index,
            frames_per_buffer=mic_chunk,
        )

        # Pre-Buffer: letzte N bereits resamplte Chunks puffern.
        # Wenn OWW-Inferenz langsamer als Echtzeit läuft (z.B. Pi 3B), baut sich ein
        # Audio-Backlog auf. Durch das Voranstellen des Pre-Buffers an die Aufnahme
        # geht keine Sprache verloren, die während der Inferenz-Verzögerung aufgenommen wurde.
        pre_buffer: deque[bytes] = deque(maxlen=self.cfg.pre_buffer_chunks)

        try:
            while self._running:
                pcm_bytes = stream.read(mic_chunk, exception_on_overflow=False)
                pcm = np.frombuffer(pcm_bytes, dtype=np.int16)
                if src_rate != self._OWW_RATE:
                    pcm = self._resample(pcm, src_rate)

                resampled_bytes = pcm.tobytes()
                pre_buffer.append(resampled_bytes)

                scores = self._oww.predict(pcm)

                if scores:
                    best = max(scores, key=scores.get)
                    best_score = scores[best]
                    if best_score > 0.2:
                        log.debug(f"Wake-Word Score: {best}={best_score:.3f}")
                    if best_score >= self.cfg.wakeword_score:
                        log.info(f"Wake-Word erkannt: {best} ({best_score:.3f})")
                        self._oww.reset()
                        self._tts_discard.set()   # alte TTS-Chunks verwerfen
                        self._set_led(True)
                        self._play_pling()
                        self._tts_discard.clear() # neue Session beginnt
                        self._record_and_stream(stream, pre_buffer=list(pre_buffer))
                        pre_buffer.clear()
                        log.info("Aufnahme beendet. Lausche wieder auf Wake-Word ...")
        finally:
            stream.stop_stream()
            stream.close()

    # ------------------------------------------------------------------
    # Aufnahme + UDP-Streaming

    def _record_and_stream(self, mic_stream, pre_buffer: Optional[list[bytes]] = None):
        """
        Liest PCM vom Mikrofon, resampelt auf 16kHz und streamt per UDP an Hannah.
        Beendet sich wenn Stille erkannt wird oder max_record_secs überschritten.

        pre_buffer: bereits resamplte 16kHz-Chunks aus dem Wake-Word-Loop, die
                    der Aufnahme vorangestellt werden (Sprache während OWW-Inferenz).
        """
        cfg = self.cfg
        src_rate = cfg.sample_rate
        mic_chunk = int(self._OWW_CHUNK * src_rate / self._OWW_RATE)
        frames_per_sec = src_rate / mic_chunk

        max_frames     = int(cfg.max_record_secs * frames_per_sec)
        min_frames     = int(cfg.min_record_secs * frames_per_sec)
        silence_frames = int(cfg.silence_secs * frames_per_sec)

        consecutive_silence = 0
        recorded_frames     = 0
        chunks_sent         = 0

        # Pre-Buffer voranstellen — Sprache die während der OWW-Inferenz aufgenommen
        # wurde geht so nicht verloren.
        if pre_buffer:
            for chunk in pre_buffer:
                self._send_audio(chunk)
                chunks_sent += 1
            log.debug(f"Pre-Buffer: {len(pre_buffer)} Chunks ({len(pre_buffer) * 80}ms) vorangestellt.")

        while recorded_frames < max_frames:
            pcm_bytes = mic_stream.read(mic_chunk, exception_on_overflow=False)

            # Auf 16kHz resampeln bevor an Hannah gesendet wird
            pcm = np.frombuffer(pcm_bytes, dtype=np.int16)
            if src_rate != self._OWW_RATE:
                pcm = self._resample(pcm, src_rate)
            pcm_bytes = pcm.tobytes()

            self._send_audio(pcm_bytes)
            chunks_sent     += 1
            recorded_frames += 1

            # Stille-Erkennung erst nach Mindestaufnahmedauer aktiv
            if recorded_frames < min_frames:
                continue

            rms = self._rms(pcm_bytes)
            if rms < cfg.silence_threshold:
                consecutive_silence += 1
                if consecutive_silence >= silence_frames:
                    log.debug(
                        f"Stille erkannt nach {recorded_frames} Frames "
                        f"(RMS={rms}, threshold={cfg.silence_threshold})"
                    )
                    break
            else:
                consecutive_silence = 0

        # Aufnahme abschließen
        self._send_control({"type": "audio_end", "device": cfg.device_name})
        log.debug(
            f"audio_end gesendet. Frames: {recorded_frames}, Pakete: {chunks_sent}, "
            f"Dauer: {recorded_frames / frames_per_sec:.1f}s"
        )

    # ------------------------------------------------------------------
    # Sende-Hilfen

    def _send_control(self, msg: dict):
        data = bytes([TYPE_CONTROL]) + json.dumps(msg, ensure_ascii=False).encode()
        self._sock.sendto(data, self._hannah_addr)

    def _send_audio(self, pcm_bytes: bytes):
        data = bytes([TYPE_AUDIO]) + pcm_bytes
        self._sock.sendto(data, self._hannah_addr)

    @staticmethod
    def _rms(pcm_bytes: bytes) -> float:
        """Berechnet den RMS-Energiewert eines PCM-Frames (16-bit signed)."""
        if not pcm_bytes:
            return 0.0
        n = len(pcm_bytes) // 2
        samples = struct.unpack_from(f"{n}h", pcm_bytes)
        mean_sq = sum(s * s for s in samples) / n
        return mean_sq ** 0.5

    # ------------------------------------------------------------------

    def _shutdown(self):
        self._running = False

        # Heartbeat-Thread stoppen
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=2.0)

        self._led_stop()
        if self._gpio:
            self._gpio.cleanup()
        self._mqtt.disconnect()
        if self._out_stream:
            try:
                self._out_stream.stop_stream()
                self._out_stream.close()
            except Exception:
                pass
        if self._pa:
            self._pa.terminate()
        if self._sock:
            self._sock.close()


# ── Einstiegspunkt ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Hannah-Satellit (Raspberry Pi)")
    parser.add_argument("--device",    default="rpi-test",       help="Gerätename")
    parser.add_argument("--room",      default="Wohnzimmer",     help="Raum")
    parser.add_argument("--host",      default="",               help="Hannah-IP (leer = MQTT-Discovery)")
    parser.add_argument("--port",      default=7775, type=int,   help="Hannah UDP-Port")
    parser.add_argument("--broker",    default="192.168.8.1",    help="MQTT-Broker für Discovery")
    parser.add_argument("--broker-port", default=1883, type=int, help="MQTT-Broker-Port")
    parser.add_argument("--mqtt-user", default="",               help="MQTT-Benutzername")
    parser.add_argument("--mqtt-pass", default="",               help="MQTT-Passwort")
    parser.add_argument("--discovery-topic", default="hannah/server", help="MQTT-Discovery-Topic")
    parser.add_argument("--wakeword-model", default="", help="Pfad zu eigenem OpenWakeWord .onnx Modell (leer = Standard hey_jarvis)")
    parser.add_argument("--wakeword-score", default=0.7, type=float, help="Erkennungsschwelle (0.0–1.0, Standard: 0.7)")
    parser.add_argument("--framework", default="onnx", choices=["onnx", "tflite"], help="Inference-Framework: onnx (Standard) oder tflite (RPi 3B armv7l)")
    parser.add_argument("--mic",         default=None,  type=int,   help="Mikrofon-Geräteindex")
    parser.add_argument("--speaker",     default=None,  type=int,   help="Lautsprecher-Geräteindex")
    parser.add_argument("--sample-rate", default=16000, type=int,   help="Mikrofon-Sample-Rate (Standard: 16000, Windows oft 44100 oder 48000)")
    parser.add_argument("--tts-rate",    default=44100, type=int,   help="Lautsprecher-Sample-Rate (44100 für RPi-Klinke, 48000 für USB-Audio)")
    parser.add_argument("--listen-port", default=7776, type=int, help="UDP-Empfangsport für TTS (Standard: 7776)")
    parser.add_argument("--pling-sound", default="",             help="WAV-Datei für Zuhör-Ton (leer = synthetisiert)")
    parser.add_argument("--min-secs",  default=0.8,  type=float, help="Mindestaufnahme in Sekunden bevor Stille-Erkennung aktiv wird")
    parser.add_argument("--silence",   default=0.8,  type=float, help="Stille-Dauer in Sekunden")
    parser.add_argument("--threshold", default=300,  type=int,   help="RMS-Schwellwert für Stille")
    parser.add_argument("--max-secs",  default=8,    type=int,   help="Maximale Aufnahmedauer in Sekunden")
    parser.add_argument("--led-pin",  default=0,    type=int,   help="GPIO-Pin für Status-LED (BCM, 0 = deaktiviert)")
    parser.add_argument("--pre-buffer", default=5, type=int, help="Anzahl 80ms-Chunks die vor dem Wake-Word gepuffert werden (~400ms, verhindert Sprachverlust bei langsamer OWW-Inferenz)")
    parser.add_argument("--reg-timeout", default=5.0, type=float, help="Registrierungs-Timeout (Sek)")
    parser.add_argument("--hb-interval", default=10, type=int, help="Heartbeat-Interval (Sek)")
    parser.add_argument("--hb-timeout", default=3, type=int, help="Max. fehlgeschlagene Heartbeats")
    parser.add_argument("--hb-wait", default=15.0, type=float, help="Max. auf heartbeat_ack warten (Sek)")
    parser.add_argument("--backoff-base", default=30, type=int, help="Backoff Basis (Sek)")
    parser.add_argument("--max-backoff", default=300, type=int, help="Max. Backoff (Sek)")
    parser.add_argument("-v", "--verbose", action="store_true",  help="DEBUG-Logging")
    parser.add_argument("--download-models", action="store_true", help="OpenWakeWord-Modelle herunterladen und beenden")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.download_models:
        _apply_tflite_stubs()
        from openwakeword.utils import download_models
        log.info("Lade OpenWakeWord-Modelle herunter ...")
        download_models()
        log.info("Fertig.")
        sys.exit(0)

    cfg = Config(
        device_name       = args.device,
        room              = args.room,
        hannah_host       = args.host,
        hannah_port       = args.port,
        mqtt_broker       = args.broker,
        mqtt_port         = args.broker_port,
        mqtt_user         = args.mqtt_user,
        mqtt_pass         = args.mqtt_pass,
        discovery_topic   = args.discovery_topic,
        wakeword_model       = args.wakeword_model,
        wakeword_score       = args.wakeword_score,
        inference_framework  = args.framework,
        mic_device_index  = args.mic,
        sample_rate       = args.sample_rate,
        tts_sample_rate   = args.tts_rate,
        tts_device_index  = args.speaker,
        listen_port       = args.listen_port,
        pling_sound       = args.pling_sound,
        min_record_secs   = args.min_secs,
        silence_secs      = args.silence,
        silence_threshold = args.threshold,
        max_record_secs   = args.max_secs,
        led_pin           = args.led_pin,
        pre_buffer_chunks = args.pre_buffer,
        registration_timeout  = args.reg_timeout,
        heartbeat_interval    = args.hb_interval,
        heartbeat_timeout     = args.hb_timeout,
        max_heartbeat_wait    = args.hb_wait,
        backoff_base          = args.backoff_base,
        max_backoff           = args.max_backoff,
    )

    Satellite(cfg).run()
