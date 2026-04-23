#!/usr/bin/env python3
"""
hannah — Voice-Assistant Middleware
Empfängt Audio via MQTT oder UDP, transkribiert mit Whisper,
erkennt Intents anhand ioBroker-Räumen/Functions
und steuert Geräte direkt via MQTT.
"""
import argparse
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
from typing import Optional

import numpy as np

from hannah import audio as audio_mod
from hannah import config as config_mod
from hannah.car_tracker import CarManager, CarTracker
from hannah.routines import RoutineManager
from hannah.grpc_server import GrpcServer, HannahServicer, make_car_parked_event, make_resident_event, make_system_notification_event
from hannah.iobroker import IoBrokerClient
from hannah.mqtt_handler import MQTTHandler
from hannah.nlu import NLU, Intent, build_clarification_question, resolve_clarification_answer
from hannah.residents import ResidentsClient
from hannah.stt import STT
from hannah.tts import TTS
from hannah.udp_server import UDPServer
from hannah.conversation import ConversationContext
from hannah.llm import load as load_llm, prepare_prompt
from hannah.memory import LongTermMemory
from hannah.user_registry import UserRegistry
from hannah.weather import WeatherCache


def setup_logging(level: str):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main():
    parser = argparse.ArgumentParser(description="hannah Voice Middleware")
    parser.add_argument("-c", "--config", default="config.yaml", help="Pfad zur config.yaml")
    parser.add_argument("-l", "--log-level", default="INFO", help="Log-Level (DEBUG|INFO|WARNING)")
    args = parser.parse_args()

    setup_logging(args.log_level)
    log = logging.getLogger("hannah.main")

    try:
        cfg = config_mod.load(args.config)
    except FileNotFoundError as e:
        log.error(str(e))
        sys.exit(1)

    # Statische Raum-Zuordnung aus Config (Fallback für MQTT-Satelliten ohne Registrierung)
    device_rooms: dict[str, str] = cfg.get("device_rooms", {})

    # ioBroker
    iobroker = IoBrokerClient(cfg.get("iobroker", {}))
    iobroker.load()

    if not iobroker.rooms:
        log.warning("Keine Räume aus ioBroker geladen — NLU arbeitet ohne Raum-Erkennung.")

    # User Registry
    residents_cfg = cfg.get("residents", {})
    roomie_prefix = residents_cfg.get("topic_prefix_read", "residents/0/roomie").replace("/", ".")
    registry = UserRegistry(
        cfg.get("user_registry", {}),
        fetch_roomies=lambda: iobroker.list_roomies(roomie_prefix),
        hannah_roomie=residents_cfg.get("hannah_roomie", "hannah"),
    )
    registry.start_sync_loop()

    # STT + NLU + TTS
    stt = STT(cfg.get("stt", {}))
    _group_pseudo_rooms = {k: k.capitalize() for k in cfg.get("groups", {})}
    nlu = NLU(cfg.get("nlu", {}), {**iobroker.rooms, **_group_pseudo_rooms}, iobroker.devices)
    tts = TTS(cfg.get("tts", {}))

    llm = load_llm(cfg.get("llm", {}))
    llm_system_prompt: str = cfg.get("llm", {}).get("system_prompt", "")

    mem_cfg = cfg.get("memory", {})
    memory = LongTermMemory(
        db_path=mem_cfg.get("db", "memory.db"),
        recent_limit=int(mem_cfg.get("recent_limit", 10)),
    )

    _SUMMARY_PROMPT = (
        "Fasse das folgende Gespräch in einem einzigen, natürlichen deutschen Satz zusammen. "
        "Konzentriere dich auf das Wesentliche: worüber wurde gesprochen, was hat die Person "
        "erwähnt oder gefragt. Antworte nur mit dem Satz, ohne Einleitung."
    )
    _ANON_SOURCES = {"anon", "mqtt", "grpc-voice"}

    def _on_conversation_end(source: str, history: list):
        if source in _ANON_SOURCES or not history:
            return
        try:
            history_text = "\n".join(
                f"{'Nutzer' if m['role'] == 'user' else 'Hannah'}: {m['content']}"
                for m in history
            )
            summary = llm.chat(history_text, system_prompt=_SUMMARY_PROMPT)
            if summary and summary.strip():
                memory.add(source, summary.strip())
        except Exception as e:
            log.warning(f"[memory] Zusammenfassung fehlgeschlagen für {source!r}: {e}")

    llm_cfg = cfg.get("llm", {})
    conv_ctx = ConversationContext(
        ttl=float(llm_cfg.get("context_ttl", 120.0)),
        max_history_turns=int(llm_cfg.get("history_turns", 3)),
        on_conversation_end=_on_conversation_end,
    )

    weather_cfg = cfg.get("weather", {})
    weather = WeatherCache(
        topic_prefix=weather_cfg.get("topic_prefix", "openweathermap/0/forecast")
    )

    # Auto-Tracker (cars: Liste; car: alter Einzeleintrag — Backward-Compat)
    _car_cfgs = cfg.get("cars") or ([cfg["car"]] if cfg.get("car") else [{}])
    car_manager = CarManager([CarTracker(c) for c in _car_cfgs])

    routine_manager = RoutineManager(cfg.get("routines_file", "routines.yaml"))

    audio_cfg = cfg.get("audio", {})

    # ------------------------------------------------------------------
    # Kern-Pipeline: numpy-Array → Intent → Gerät schalten (Sprach-Pfad)
    # Wird von MQTT- und UDP-Pfad gleichermaßen genutzt.

    def pipeline(device: str, audio_array, publish_error, publish_text, publish_intent, publish_answer):
        # STT
        try:
            text, no_speech_prob = stt.transcribe(audio_array)
        except Exception as e:
            log.error(f"[{device}] STT fehlgeschlagen: {e}")
            publish_error(f"STT: {e}")
            return

        if not text:
            log.debug(f"[{device}] Keine Sprache erkannt (no_speech={no_speech_prob:.2f})")
            return

        log.info(f"[{device}] Text: '{text}'")
        publish_text(text)
        _room = (udp_server.get_registered_room(device) or device_rooms.get(device, device)).lower()
        mqtt_handler.publish_transcript(_room, text)

        # Routine-Check vor NLU
        routine = routine_manager.match(text)
        if routine:
            for action in routine.actions:
                mqtt_handler.publish_raw(action.topic, action.value)
            if routine.reply:
                _handle_feedback(device, True, routine.reply)
            publish_intent(Intent(name="Routine", value=routine.name, raw_text=text))
            return

        # Offene Rückfrage auflösen
        if conv_ctx.has_clarification(device):
            clarification = conv_ctx.get_clarification(device)
            resolved = resolve_clarification_answer(text, clarification["candidates"])
            if resolved:
                conv_ctx.clear_clarification(device)
                orig: Intent = clarification["intent"]
                orig.room    = resolved[1]
                orig.room_id = resolved[0]
                count = iobroker.execute(orig, satellite_device=device)
                conv_ctx.update_from_intent(device, orig)
                if count == 0:
                    _handle_feedback(device, False, "Tut mir leid, ich weiß nicht was du meinst.")
                publish_intent(orig)
                return
            # Keine Übereinstimmung → Rückfrage verwerfen, normal weiterverarbeiten
            conv_ctx.clear_clarification(device)

        # NLU
        intent = nlu.parse(text)

        # Gesprächskontext: fehlende Felder ergänzen + Aktion erben
        conv_ctx.fill_intent(device, intent)
        conv_ctx.inherit_action(device, intent)

        # Raum-Fallback: UDP-Registrierung → Config → nichts
        # Bei Query-Intents nur anwenden wenn der Raum explizit im Text genannt wurde —
        # ohne Raum soll die globale Abfrage greifen.
        if intent.room is None and intent.name not in ("Query", "CarQuery"):
            room = udp_server.get_registered_room(device) or device_rooms.get(device)
            if room:
                intent.room    = room
                intent.room_id = room.lower()
                log.debug(f"[{device}] Raum-Fallback: '{room}'")

        log.info(
            f"[{device}] Intent: {intent.name} | "
            f"Raum: {intent.room} | Gerät: {intent.device} | Wert: {intent.value}"
        )

        # Mehrdeutiger Raum → Rückfrage stellen
        if intent.candidates:
            question = build_clarification_question(intent.candidates)
            conv_ctx.set_clarification(device, intent, intent.candidates)
            _handle_feedback(device, True, question)
            return

        if intent.name == "CarQuery":
            answer = car_manager.answer_for_roomie(scope=intent.value or "all")
            publish_answer(answer)
        elif intent.name == "WeatherQuery":
            publish_answer(weather.build_answer(scope=intent.value or "today"))
        elif intent.name == "SetPresence":
            if intent.value == "away":
                log.info(f"[{device}] SetPresence away — Sprecher unbekannt, Status nicht gesetzt.")
                _handle_feedback(device, True, "Tschüss! Bis bald.")
            else:
                log.info(f"[{device}] SetPresence home — Sprecher unbekannt, Status nicht gesetzt.")
                _handle_feedback(device, True, "Willkommen zuhause!")
        elif intent.name in ("StopIntent", "PauseIntent", "ResumeIntent"):
            cmd_type = {"StopIntent": "stop", "PauseIntent": "pause", "ResumeIntent": "resume"}[intent.name]
            targets = _resolve_targets(intent.room_id or device)
            for t in targets:
                udp_server.send_command(t, {"type": cmd_type})
        elif intent.name == "SetDND":
            active = intent.value == "on"
            _apply_global_dnd(active)
            mqtt_handler.publish_global_dnd(active)
            _handle_feedback(device, True, "Nicht stören aktiv." if active else "Nicht stören deaktiviert.")
        elif intent.name == "SetMute":
            active = intent.value == "on"
            _apply_global_mute(active)
            mqtt_handler.publish_global_mute(active)
            _handle_feedback(device, True, "Mikrofone stumm." if active else "Mikrofone wieder aktiv.")
        elif intent.name == "Smalltalk":
            history = conv_ctx.get_llm_history(device)
            answer = llm.chat(text, system_prompt=prepare_prompt(llm_system_prompt, iobroker), history=history)
            conv_ctx.add_llm_exchange(device, text, answer)
            _handle_feedback(device, True, answer)
        elif intent.name == "Query":
            answer = iobroker.answer_query(intent)
            if answer:
                conv_ctx.update_from_intent(device, intent)
                publish_answer(answer)
            else:
                log.warning(f"[{device}] Keine Antwort auf Query möglich.")
        else:
            count = iobroker.execute(intent, satellite_device=device)
            conv_ctx.update_from_intent(device, intent)
            if intent.name == "Unknown":
                _handle_feedback(device, False, "Tut mir leid, ich habe dich nicht verstanden.")
            elif count == 0:
                log.warning(f"[{device}] Keine States gesetzt — Intent nicht auflösbar.")
                _handle_feedback(device, False, "Tut mir leid, ich weiß nicht was du meinst.")

        publish_intent(intent)

    # ------------------------------------------------------------------
    def _speaker_context(speaker_roomie_id: str) -> str:
        """Gibt einen Zusatz-Abschnitt für den System-Prompt zurück der Sprecher-Info enthält."""
        if not speaker_roomie_id:
            return ""
        user = registry.get_by_roomie(speaker_roomie_id)
        if not user:
            return f"\n\nDie Person die gerade mit dir spricht heißt {speaker_roomie_id}."
        name        = user["display_name"]
        trust_level = user.get("trust_level", 5)
        # relationship_level: noch nicht implementiert, Platzhalter für spätere Erweiterung
        mem = memory.format_for_prompt(speaker_roomie_id)
        return (
            f"\n\nDie Person die gerade mit dir spricht heißt {name}."
            f" Vertrauenslevel: {trust_level}/10."
            f"{mem}"
        )

    def _handle_text(text: str, speaker_roomie_id: str = "", source: str = "") -> tuple[str, str]:
        """
        Verarbeitet einen Text-Befehl durch NLU und gibt (Antwort, Intent-Name) zurück.
        Kein MQTT, kein TTS — reines Text-in/Text-out.
        Wird von process_text_command (MQTT) und dem gRPC-Server (Telegram/Satelliten) genutzt.

        speaker_roomie_id: optionale Roomie-ID aus Voice-ID-Erkennung.
        source: Kontext-Schlüssel (Gerät, Roomie-ID, Kanal). Leer = speaker_roomie_id oder "anon".
        """
        _source = source or speaker_roomie_id or "anon"

        routine = routine_manager.match(text)
        if routine:
            for action in routine.actions:
                mqtt_handler.publish_raw(action.topic, action.value)
            return routine.reply or "Routine ausgeführt.", "Routine"

        # Smalltalk-Modus: LLM-Classifier vor NLU schalten
        if conv_ctx.is_smalltalk_active(_source):
            if not llm.classify(text):
                log.debug(f"[{_source}] Classifier → SMALLTALK (Modus aktiv)")
                sp = prepare_prompt(llm_system_prompt, iobroker) + _speaker_context(speaker_roomie_id)
                history = conv_ctx.get_llm_history(_source)
                answer = llm.chat(text, system_prompt=sp, history=history)
                conv_ctx.add_llm_exchange(_source, text, answer)
                return answer, "Smalltalk"
            log.debug(f"[{_source}] Classifier → COMMAND (Modus aktiv, weiter mit NLU)")

        if conv_ctx.has_clarification(_source):
            clarification = conv_ctx.get_clarification(_source)
            resolved = resolve_clarification_answer(text, clarification["candidates"])
            if resolved:
                conv_ctx.clear_clarification(_source)
                orig: Intent = clarification["intent"]
                orig.room    = resolved[1]
                orig.room_id = resolved[0]
                count = iobroker.execute(orig)
                conv_ctx.update_from_intent(_source, orig)
                return ("OK." if count > 0 else "Keine Geräte gefunden."), "Routine"
            conv_ctx.clear_clarification(_source)

        intent = nlu.parse(text)

        # Gesprächskontext: fehlende Felder ergänzen + Aktion erben
        conv_ctx.fill_intent(_source, intent)
        conv_ctx.inherit_action(_source, intent)

        log.info(
            f"[textcmd] Text: '{text}' → Intent: {intent.name} | "
            f"Raum: {intent.room} | Gerät: {intent.device} | Wert: {intent.value} | SpeakerRoomie: {speaker_roomie_id}"
        )

        if intent.candidates:
            question = build_clarification_question(intent.candidates)
            conv_ctx.set_clarification(_source, intent, intent.candidates)
            return question, "Clarification"

        if intent.name == "CarQuery":
            answer = car_manager.answer_for_roomie(scope=intent.value or "all", roomie_id=speaker_roomie_id)
        elif intent.name == "WeatherQuery":
            answer = weather.build_answer(scope=intent.value or "today")
        elif intent.name == "SetPresence":
            if intent.value == "away":
                if speaker_roomie_id:
                    residents.set_user_away(speaker_roomie_id)
                else:
                    log.info("SetPresence away — Sprecher anonym, Status nicht gesetzt.")
                answer = "Tschüss!"
            else:
                if speaker_roomie_id:
                    residents.set_user_home(speaker_roomie_id)
                else:
                    log.info("SetPresence home — Sprecher anonym, Status nicht gesetzt.")
                answer = "Willkommen zuhause!"
        elif intent.name in ("StopIntent", "PauseIntent", "ResumeIntent"):
            cmd_type = {"StopIntent": "stop", "PauseIntent": "pause", "ResumeIntent": "resume"}[intent.name]
            source_device = source if source in {**udp_server.registered_devices(), **grpc_servicer.proxy_satellites()} else None
            targets = _resolve_targets(intent.room_id or source_device or "all")
            for t in targets:
                udp_server.send_command(t, {"type": cmd_type})
            answer = ""
        elif intent.name == "SetDND":
            active = intent.value == "on"
            _apply_global_dnd(active)
            mqtt_handler.publish_global_dnd(active)
            answer = "Nicht stören aktiv." if active else "Nicht stören deaktiviert."
        elif intent.name == "SetMute":
            active = intent.value == "on"
            _apply_global_mute(active)
            mqtt_handler.publish_global_mute(active)
            answer = "Mikrofone stumm." if active else "Mikrofone wieder aktiv."
        elif intent.name == "Smalltalk":
            sp = prepare_prompt(llm_system_prompt, iobroker) + _speaker_context(speaker_roomie_id)
            history = conv_ctx.get_llm_history(_source)
            answer = llm.chat(text, system_prompt=sp, history=history)
            conv_ctx.add_llm_exchange(_source, text, answer)
            conv_ctx.set_smalltalk_active(_source, True)
        elif intent.name == "Query":
            answer = iobroker.answer_query(intent) or "Keine Antwort verfügbar."
            conv_ctx.update_from_intent(_source, intent)
        elif intent.name == "Unknown":
            answer = "Intent nicht erkannt."
        else:
            count = iobroker.execute(intent)
            if count > 0:
                conv_ctx.set_smalltalk_active(_source, False)
            answer = "Keine Geräte gefunden." if count == 0 else f"OK, {count} Gerät(e) geschaltet."
            conv_ctx.update_from_intent(_source, intent)

        return answer, intent.name

    def process_text_command(text: str):
        """Text-Command direkt aus MQTT — überspringt STT und TTS/UDP."""
        answer, _ = _handle_text(text, source="mqtt")
        mqtt_handler.publish_text_answer(answer)

    # ── Satellit-Steuerung: Volume / Mute / DND ───────────────────────────────
    _global_volume: int = 80          # 0-100
    _device_volume: dict[str, int] = {}
    _device_mute:   dict[str, bool] = {}
    _device_dnd:    dict[str, bool] = {}

    def _get_volume(device: str) -> int:
        return _device_volume.get(device, _global_volume)

    def _scale_pcm(pcm: bytes, volume: int) -> bytes:
        if volume == 100:
            return pcm
        factor = volume / 100.0
        arr = np.frombuffer(pcm, dtype=np.int16)
        scaled = np.clip(np.round(arr * factor), -32768, 32767).astype(np.int16)
        return scaled.tobytes()

    def _send_audio(target: str, pcm: bytes, rate: int, label: str = ""):
        """Sendet PCM an einen Satelliten — mit Lautstärke-Skalierung."""
        pcm = _scale_pcm(pcm, _get_volume(target))
        _all = {**udp_server.registered_devices(), **grpc_servicer.proxy_satellites()}
        _room = _all.get(target, target).lower()
        mqtt_handler.publish_speaking(_room, True)
        if grpc_servicer.has_proxy():
            grpc_servicer.push_audio_to_proxy(target, pcm, rate)
            log.info(f"{label}Announcement → {target} (via Proxy)")
        else:
            mqtt_handler.publish_satellite_status(target, "speaking")
            udp_server.send_tts(target, pcm, sample_rate=rate)
            mqtt_handler.publish_satellite_status(target, "idle")
            log.info(f"{label}Announcement → {target} (via UDP)")
        mqtt_handler.publish_speaking(_room, False)

    def _resolve_targets(device: str, label: str = "") -> list[str]:
        """Löst device/room/group/'all' auf eine Liste von Ziel-Geräten auf."""
        all_devices = {**udp_server.registered_devices(), **grpc_servicer.proxy_satellites()}
        if device == "all":
            return list(all_devices.keys())
        if device in all_devices:
            return [device]
        room_lower = device.lower()
        targets = [d for d, r in all_devices.items() if r.lower() == room_lower]
        if not targets:
            groups = cfg.get("groups", {})
            for group_key, rooms in groups.items():
                if group_key.lower() == room_lower:
                    for room in rooms:
                        targets += [d for d, r in all_devices.items() if r.lower() == room.lower()]
                    break
        if not targets:
            log.warning(f"{label}kein Satellit in Raum/Gruppe '{device}' — ignoriert.")
        return targets

    # ── Volume/Mute/DND Callbacks ─────────────────────────────────────────────

    def _on_volume(device: Optional[str], level: int):
        nonlocal _global_volume
        if device:
            _device_volume[device] = level
            mqtt_handler.publish_volume_state(level, device)
            log.info(f"Lautstärke {device}: {level}%")
        else:
            _global_volume = level
            mqtt_handler.publish_volume_state(level)
            log.info(f"Lautstärke global: {level}%")

    def _on_mute(device: str, muted: bool):
        _device_mute[device] = muted
        mqtt_handler.publish_mute_state(device, muted)
        log.info(f"Mute {device}: {muted}")

    def _on_dnd(device: str, active: bool):
        _device_dnd[device] = active
        mqtt_handler.publish_dnd_state(device, active)
        log.info(f"DND {device}: {active}")

    def _apply_global_dnd(active: bool):
        """Setzt DND auf allen bekannten Satelliten und publiziert den globalen State."""
        for device in _resolve_targets("all"):
            _device_dnd[device] = active
            mqtt_handler.publish_dnd_state(device, active)
        log.info(f"Globales DND: {active}")

    def _apply_global_mute(active: bool):
        """Setzt Mute auf allen bekannten Satelliten und publiziert den globalen State."""
        for device in _resolve_targets("all"):
            _device_mute[device] = active
            mqtt_handler.publish_mute_state(device, active)
        log.info(f"Globales Mute: {active}")

    # ── Announcements ─────────────────────────────────────────────────────────

    def process_announcement(device: str, text: str, *, ssml: bool = False):
        """Synthetisiert Text/SSML per TTS und sendet ihn an einen oder alle Satelliten."""
        if not tts.enabled:
            log.warning("Announcement ignoriert — TTS ist nicht konfiguriert.")
            return
        result = tts.synthesize_ssml(text) if ssml else tts.synthesize(text)
        if not result:
            return
        pcm, rate = result
        targets = _resolve_targets(device)
        for target in targets:
            if _device_dnd.get(target):
                log.info(f"Announcement → {target} unterdrückt (DND aktiv).")
                continue
            _send_audio(target, pcm, rate)

    def process_room_announce(room: str, text: str):
        process_announcement(room, text)

    def process_ssml_announcement(room: str, ssml: str):
        process_announcement(room, ssml, ssml=True)

    mqtt_handler = MQTTHandler(cfg.get("mqtt", {}), audio_cfg)
    mqtt_handler.set_announcement_handler(process_announcement)
    mqtt_handler.set_room_announce_handler(process_room_announce)
    mqtt_handler.set_room_announce_ssml_handler(process_ssml_announcement)
    mqtt_handler.set_text_command_handler(process_text_command)
    mqtt_handler.set_volume_handler(_on_volume)
    mqtt_handler.set_mute_handler(_on_mute)
    mqtt_handler.set_dnd_handler(_on_dnd)
    mqtt_handler.set_global_dnd_handler(_apply_global_dnd)
    mqtt_handler.set_global_mute_handler(_apply_global_mute)
    tts.set_backend_change_handler(mqtt_handler.publish_tts_backend)

    # Satellite-Online-Tracking: diff berechnen und per-device online/offline publishen
    _known_satellites: set[str] = set()

    def _on_satellite_change(satellite_map: dict[str, str]):
        nonlocal _known_satellites
        current = set(satellite_map.keys())
        for device in current - _known_satellites:
            mqtt_handler.publish_satellite_online(device, True)
        for device in _known_satellites - current:
            mqtt_handler.publish_satellite_online(device, False)
        _known_satellites = current
        mqtt_handler.publish_rooms(satellite_map)
    iobroker.set_publisher(mqtt_handler.publish_raw)
    mqtt_handler.set_state_subscriber(
        iobroker.state_topic_prefix, iobroker.handle_state_update
    )
    log.info(f"State-Cache abonniert: {iobroker.state_topic_prefix}/#")
    mqtt_handler.set_weather_handler(weather.topic_prefix, weather.update)
    log.info(f"Wetter-Cache abonniert: {weather.topic_prefix}/#")
    for _ct in car_manager:
        mqtt_handler.add_car_handler(_ct.topic_prefix, _ct.update)
        log.info(f"Auto-Status abonniert: {_ct.topic_prefix}/#")

    # ------------------------------------------------------------------
    # Voice-Pipeline für gRPC: OGG/Opus → STT → NLU → TTS → OGG/Opus
    # Wird von SubmitVoice (Telegram, zukünftige Services) genutzt.

    def _handle_voice(audio_ogg: bytes, speaker_roomie_id: str = "") -> tuple[str, str, str, bytes]:
        """OGG/Opus bytes → (transcript, answer, intent_name, audio_ogg_out)"""
        # OGG → raw PCM (16kHz, mono, s16le) via ffmpeg
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(audio_ogg)
            ogg_path = f.name
        try:
            proc = subprocess.run(
                ["ffmpeg", "-y", "-i", ogg_path,
                 "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
                capture_output=True,
            )
        finally:
            os.unlink(ogg_path)

        if proc.returncode != 0 or not proc.stdout:
            log.error(f"[grpc/voice] ffmpeg OGG→PCM fehlgeschlagen: {proc.stderr.decode()}")
            return "", "Ich konnte die Sprachnachricht nicht verarbeiten.", "Unknown", b""

        audio_array = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0

        try:
            transcript, _ = stt.transcribe(audio_array)
        except Exception as e:
            log.error(f"[grpc/voice] STT fehlgeschlagen: {e}")
            return "", "Ich konnte dich leider nicht verstehen.", "Unknown", b""

        if not transcript:
            return "", "Ich konnte dich leider nicht verstehen.", "Unknown", b""

        log.info(f"[grpc/voice] Transkript: {transcript!r}")
        answer, intent_name = _handle_text(transcript, speaker_roomie_id, source=speaker_roomie_id or "grpc-voice")

        # TTS → PCM → OGG/Opus via ffmpeg
        audio_ogg_out = b""
        if tts.enabled:
            result = tts.synthesize(answer)
            if result:
                pcm, sample_rate = result
                ff = subprocess.run(
                    ["ffmpeg", "-y",
                     "-f", "s16le", "-ac", "1", "-ar", str(sample_rate), "-i", "pipe:0",
                     "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", "pipe:1"],
                    input=pcm, capture_output=True,
                )
                if ff.returncode == 0:
                    audio_ogg_out = ff.stdout
                else:
                    log.warning(f"[grpc/voice] ffmpeg PCM→OGG fehlgeschlagen: {ff.stderr.decode()}")

        return transcript, answer, intent_name, audio_ogg_out

    def _handle_satellite_audio(device: str, room: str, pcm_bytes: bytes, speaker_roomie_id: str = "") -> tuple[str, str, str, bytes, int]:
        """
        Verarbeitet eine vollständige Satellit-Aufnahme via Go-Proxy:
        Raw PCM → STT → NLU → TTS → (transcript, answer, intent_name, tts_pcm, sample_rate)

        speaker_roomie_id: vom Proxy per Voice-ID identifizierter Sprecher (leer = anonym).
        """
        try:
            audio_array = audio_mod.from_raw_pcm(pcm_bytes, audio_cfg)
        except Exception as e:
            log.error(f"[{device}] Satellit-Audio-Konvertierung fehlgeschlagen: {e}")
            return "", "Fehler bei der Audio-Verarbeitung.", "Unknown", b"", 0

        try:
            transcript, _ = stt.transcribe(audio_array)
        except Exception as e:
            log.error(f"[{device}] STT fehlgeschlagen: {e}")
            return "", "Ich konnte dich leider nicht verstehen.", "Unknown", b"", 0

        if not transcript:
            return "", "Ich konnte dich leider nicht verstehen.", "Unknown", b"", 0

        log.info(f"[{device}] Satellit-Transkript: {transcript!r}"
                 + (f" (Sprecher: {speaker_roomie_id})" if speaker_roomie_id else ""))
        answer, intent_name = _handle_text(transcript, speaker_roomie_id=speaker_roomie_id, source=device)

        mqtt_handler.publish_text(device, transcript)
        mqtt_handler.publish_answer(device, answer)
        mqtt_handler.publish_transcript(room.lower(), transcript)
        if speaker_roomie_id:
            mqtt_handler.publish_speaker(speaker_roomie_id)

        tts_pcm = b""
        sample_rate = 0
        if tts.enabled:
            result = tts.synthesize(answer)
            if result:
                tts_pcm, sample_rate = result

        return transcript, answer, intent_name, tts_pcm, sample_rate

    udp_cfg = cfg.get("udp", {})
    _discovery_topic   = udp_cfg.get("discovery_topic", "hannah/server")
    _own_advertise_host = udp_cfg.get("advertise_host", "")
    _own_udp_port      = int(udp_cfg.get("port", 7775))

    def _on_proxy_discovery(host, port: int):
        """
        Wird vom RegisterProxy-Handler aufgerufen:
        - Proxy verbunden:  host/port sind die UDP-Adresse des Proxys → an Satelliten publizieren
        - Proxy getrennt:   host=None → eigene UDP-Adresse wiederherstellen
        """
        if host:
            log.info(f"Discovery: Proxy-Adresse publizieren → {host}:{port}")
            mqtt_handler.publish_discovery(udp_host=host, udp_port=port, topic=_discovery_topic)
        else:
            log.info("Discovery: eigene Hannah-Adresse wiederherstellen")
            mqtt_handler.publish_discovery(udp_host=_own_advertise_host, udp_port=_own_udp_port, topic=_discovery_topic)

    # gRPC-Servicer wird hier erstellt damit _on_arrival/_on_departure Events pushen können.
    # get_satellites und get_car_state sind Lambdas (late binding) — udp_server ist
    # zum Zeitpunkt des Aufrufs bereits gesetzt.
    grpc_servicer = HannahServicer(
        registry=registry,
        handle_text=_handle_text,
        handle_voice=_handle_voice,
        announce=process_announcement,
        get_satellites=lambda: {
            dev: {"room": room, "addr": ""}
            for dev, room in {**udp_server.registered_devices(), **grpc_servicer.proxy_satellites()}.items()
        },
        get_car_state=lambda: car_manager.first_state,
        get_all_cars=lambda: [(t.state, t.home_address) for t in car_manager],
        handle_satellite_audio=_handle_satellite_audio,
        disable_udp=lambda: udp_server.stop(),
        enable_udp=lambda: udp_server.start(),
        on_proxy_discovery=_on_proxy_discovery,
        on_satellite_change=_on_satellite_change,
        get_devices=lambda: iobroker.get_devices_snapshot(),
        control_device=lambda device_id, state, value: iobroker.control_direct(device_id, state, value),
    )

    # ------------------------------------------------------------------
    # Residents + Callbacks (referenzieren grpc_servicer für Event-Push)

    residents = ResidentsClient(cfg.get("residents", {}), mqtt_handler.publish_raw)
    mqtt_handler.set_residents_handler(residents.topic_prefix_read, residents.update)
    log.info(f"Residents abonniert: {residents.topic_prefix_read}/#")

    def _on_arrival(name: str):
        process_announcement("all", "Willkommen zuhause!")
        user = registry.get_by_roomie(name)
        display = user["display_name"] if user else name
        grpc_servicer.publish_event(make_resident_event(name, display, "arrived"))

    def _on_departure(name: str):
        log.info(f"Residents: {name} hat das Haus verlassen.")
        user = registry.get_by_roomie(name)
        display = user["display_name"] if user else name
        grpc_servicer.publish_event(make_resident_event(name, display, "departed"))

    residents.on_arrival(_on_arrival)
    residents.on_departure(_on_departure)

    # Auto-Einpark-Event → gRPC-Stream (pro Tracker, damit home_address bekannt ist)
    for _ct in car_manager:
        def _make_parked_cb(tracker=_ct):
            def _cb(state):
                grpc_servicer.publish_event(make_car_parked_event(state, tracker.home_address))
            return _cb
        _ct.on_parked(_make_parked_cb())

    # ------------------------------------------------------------------
    # System-Notification Pipeline (hannah/notification)

    def process_notification(raw_text: str, severity: str = "notify"):
        """
        Empfängt rohen Notification-Text aus ioBroker, formuliert ihn per LLM um,
        spielt ihn auf DND-freien Satelliten ab und pusht ihn per gRPC an Telegram-Nutzer
        mit system_messages=True.
        """
        # LLM-Reformulierung (optional — wenn kein LLM verfügbar: rohen Text nutzen)
        text = raw_text
        if llm is not None:
            try:
                _tone = {
                    "alert":  "Drücke dich dabei klar und etwas dringlicher aus.",
                    "notify": "Drücke dich freundlich und sachlich aus.",
                    "info":   "Drücke dich beiläufig und entspannt aus.",
                }.get(severity, "Drücke dich freundlich und sachlich aus.")
                notification_prompt = (
                    "Du bist ein freundlicher Smart-Home-Assistent. "
                    "Formuliere die folgende Systemmeldung in einem kurzen, natürlichen Satz um. "
                    "Behalte dabei alle konkreten Details wie Adapter-Namen und Versionsnummern bei. "
                    "Datumsangaben im Format M/D/YYYY oder M/D/YYYY, H:MM:SS AM/PM sind Zeitstempel — "
                    "nenne sie als Datum oder Uhrzeit, nicht als Versionsnummer. "
                    "Technische Präfixe wie 'system.host.XYZ: adapter.0:' sind Herkunftsangaben und "
                    "müssen nicht wörtlich übernommen werden. "
                    "Leere Felder wie 'onedrive: {}' bedeuten keine Fehler dort — erwähne sie nur "
                    "wenn es relevant ist. "
                    f"{_tone} "
                    "Antworte nur mit dem umformulierten Satz, ohne Erklärung."
                )
                reformulated = llm.chat(raw_text, system_prompt=notification_prompt)
                if reformulated and reformulated.strip():
                    text = reformulated.strip()
            except Exception as e:
                log.warning(f"LLM-Reformulierung fehlgeschlagen, verwende Originaltext: {e}")

        log.info(f"System-Notification: {text!r}")

        # TTS auf DND-freien Satelliten
        if tts.enabled:
            result = tts.synthesize(text)
            if result:
                pcm, rate = result
                for target in _resolve_targets("all"):
                    if not _device_dnd.get(target):
                        _send_audio(target, pcm, rate)

        # gRPC-Event → Telegram
        grpc_servicer.publish_event(make_system_notification_event(text))

    mqtt_handler.set_notification_handler(process_notification)

    mqtt_handler.connect()

    # ------------------------------------------------------------------
    # UDP-Pfad

    def process_audio_udp(device: str, raw_pcm: bytes):
        mqtt_handler.publish_satellite_status(device, "processing")
        try:
            audio_array = audio_mod.from_raw_pcm(raw_pcm, audio_cfg)
        except Exception as e:
            log.error(f"[{device}] UDP Audio-Konvertierung fehlgeschlagen: {e}")
            mqtt_handler.publish_satellite_status(device, "idle")
            return

        if len(audio_array) == 0:
            mqtt_handler.publish_satellite_status(device, "idle")
            return

        pipeline(
            device,
            audio_array,
            publish_error   = lambda m: log.error(f"[{device}] {m}"),
            publish_text    = lambda t: mqtt_handler.publish_text(device, t),
            publish_answer  = lambda a: _handle_udp_answer(device, a),
            publish_intent  = lambda i: mqtt_handler.publish_intent(device, i),
        )
        # idle wird von _handle_udp_answer/_handle_feedback gesetzt (nach TTS),
        # oder hier wenn kein TTS folgt
        if not tts.enabled:
            mqtt_handler.publish_satellite_status(device, "idle")

    def _handle_udp_answer(device: str, answer: str):
        mqtt_handler.publish_answer(device, answer)
        if tts.enabled:
            result = tts.synthesize(answer)
            if result:
                pcm, rate = result
                mqtt_handler.publish_satellite_status(device, "speaking")
                udp_server.send_tts(device, pcm, sample_rate=rate)
        mqtt_handler.publish_satellite_status(device, "idle")

    def _handle_feedback(satellite_device: str, is_success: bool, text: str):
        """
        Feedback-Handler für erfolgreiche Steuerung und Fehler:
        - is_success=True + text: Smalltalk (Text sprechen)
        - is_success=True + kein text: Erfolgreiche Steuerung (Confirmation-Ton)
        - is_success=False + text: Fehler (Text sprechen)
        """
        log.info(f"[{satellite_device}] Feedback: {'✓' if is_success else '✗'} — {text}")

        if is_success and not text:
            # Erfolgreiche Steuerung: Confirmation-Ton
            if tts.enabled:
                pcm, rate = tts.confirmation_tone()
                mqtt_handler.publish_satellite_status(satellite_device, "speaking")
                udp_server.send_tts(satellite_device, pcm, sample_rate=rate)
        elif text and tts.enabled:
            # Smalltalk oder Fehler: Text sprechen
            result = tts.synthesize(text)
            if result:
                pcm, rate = result
                mqtt_handler.publish_satellite_status(satellite_device, "speaking")
                udp_server.send_tts(satellite_device, pcm, sample_rate=rate)

        if text:
            mqtt_handler.publish_answer(satellite_device, text)
        mqtt_handler.publish_satellite_status(satellite_device, "idle")

    feedback_timeout = cfg.get("iobroker", {}).get("feedback_timeout", 3.0)
    iobroker.set_feedback_handler(_handle_feedback, timeout=feedback_timeout)

    def _on_udp_session_start(device: str):
        mqtt_handler.publish_satellite_status(device, "listening")

    tts.warm_cache(cfg.get("tts", {}).get("warm_phrases", []))

    udp_server = UDPServer(
        cfg.get("udp", {}),
        process_audio_udp,
        on_session_start=_on_udp_session_start,
        on_satellite_change=_on_satellite_change,
    )
    udp_server.start()

    # Discovery: eigene UDP-Adresse als retained MQTT-Message publizieren.
    # Wenn später ein Proxy verbindet, überschreibt _on_proxy_discovery diesen Wert.
    mqtt_handler.publish_discovery(
        udp_host=_own_advertise_host,
        udp_port=_own_udp_port,
        topic=_discovery_topic,
    )
    log.info(f"Satelliten finden Hannah über MQTT-Topic: {_discovery_topic}")

    residents.announce_online()
    log.info(f"Residents: Hannah online ({residents.topic_prefix_write}/{residents.hannah_name}/state)")

    # ------------------------------------------------------------------
    # gRPC-Server starten

    grpc_srv = GrpcServer(cfg.get("grpc", {}), grpc_servicer)
    grpc_srv.start()

    # ------------------------------------------------------------------
    # Graceful Shutdown

    stop = threading.Event()

    def on_signal(sig, frame):
        log.info("Shutdown ...")
        stop.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    log.info("hannah läuft. CTRL+C zum Beenden.")
    stop.wait()

    residents.announce_offline()
    grpc_srv.stop()
    udp_server.stop()
    mqtt_handler.disconnect()
    log.info("Beendet.")


if __name__ == "__main__":
    main()
