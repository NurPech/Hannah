#!/usr/bin/env python3
"""
hannah — Voice-Assistant Middleware
Empfängt Audio via UDP oder gRPC-Proxy, transkribiert mit Whisper,
erkennt Intents anhand ioBroker-Räumen/Functions
und steuert Geräte via ioBroker REST-API.
"""
import argparse
import datetime
import json
import time
import uuid
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
from typing import Callable, Optional

import numpy as np

from hannah.models.linked_account import LinkedAccount
from hannah.user_manager import UserManager
from hannah.utils.db import get_db, init_db
from hannah import activity_log
from hannah.utils.activity_db import init_activity_db
from hannah.residents import Roomie, Guest, Pet, Resident, HOME_PRESENCE_STATE
from hannah import audio as audio_mod
from hannah import config as config_mod
from hannah.car_tracker import CarManager, CarTracker
from hannah.car_registry import CarRegistry
from hannah.ble_tags import BleTagManager
from hannah.grpc_server import GrpcServer, HannahServicer, make_car_parked_event, make_firmware_event, make_resident_event, make_system_notification_event, pb
from hannah.iobroker import IoBrokerClient
from hannah.mqtt_handler import MQTTHandler
from hannah.nlu import NLU, Intent, build_clarification_question, resolve_clarification_answer, resolve_yes_no
from hannah.residents_manager import ResidentsClient
from hannah.stt import STT
from hannah.tts import TTS
from hannah.udp_server import UDPServer
from hannah.conversation import ConversationContext
from hannah.llm import load as load_llm, prepare_prompt
from hannah.tool_agent import ToolAgent
from hannah.memory import LongTermMemory
from hannah.room_manager import RoomManager
from hannah.satellite_manager import SatelliteManager
from hannah.settings_manager import SettingsManager
from hannah.weather import WeatherCache
from hannah.trigger_engine import TriggerEngine
from hannah.ble_location import BleLocationEngine, BleTag
from hannah.alarms import AlarmManager, format_duration
from hannah.__version__ import VERSION as HANNAH_VERSION

# Asset-IDs, die Core per play_asset anfordern könnte (#170) — Grundlage für die
# Relevanzliste, die hannah_asset auf dem Satelliten reaktiv statt blind alles im
# Manifest lädt. Core braucht dafür nicht das satellite-Namespace-Manifest zu lesen.
RELEVANT_ASSET_IDS = ["alarm_ring", "timer_jingle"]

# TimeQuery/DateQuery: rein regelbasiert aus datetime, kein LLM nötig
_WEEKDAYS_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def _time_answer() -> str:
    return f"Es ist {datetime.datetime.now().strftime('%H:%M')} Uhr."


def _date_answer() -> str:
    now = datetime.datetime.now()
    return f"Heute ist {_WEEKDAYS_DE[now.weekday()]}, der {now.strftime('%d.%m.%Y')}."


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
    log.info(f"Hannah Core {HANNAH_VERSION}")

    try:
        cfg = config_mod.load(args.config)
    except FileNotFoundError as e:
        log.error(str(e))
        sys.exit(1)

    # User-Registry (SQLite, Hannah-eigene Quelle der Wahrheit statt ioBroker)
    init_db()
    _user_manager = UserManager(get_db)

    # Activity-Log (#220) — separate MySQL-DB, No-op wenn nicht konfiguriert
    activity_log_cfg = cfg.get("activity_log", {})
    activity_log.configure(activity_log_cfg.get("audio_dir", "activity_audio"))
    init_activity_db(activity_log_cfg)
    activity_log_manager = activity_log.ActivityLogManager(_user_manager)

    def _resolve_roomie_id(speaker_user_id: str) -> str:
        """Löst eine Hannah-User-ID auf die verlinkte ioBroker-Roomie-ID auf (sofern verlinkt).

        Roomie-IDs leben in residents/car_tracker (ioBroker-Welt), die User-ID ist Hannahs
        eigene, davon entkoppelte Identität — hier wird zwischen beiden vermittelt.
        """
        if not speaker_user_id:
            return ""
        user = _user_manager.get_user_by_id(speaker_user_id)
        if not user:
            return ""
        for la in user.linked_accounts:
            if la.provider == "residents":
                payload = la.provider_payload
                return payload.get("roomie_id", "") if isinstance(payload, dict) else ""
        return ""

    # Settings (nlu.*/llm.system_prompt/iobroker.state_names — #27 Phase 5, aus config.yaml
    # migriert via deploy/migrate_config_settings.py). Fällt auf cfg/Code-Defaults zurück,
    # solange eine Kategorie noch nicht migriert ist. ble.tags/cars haben seit #115 eigene
    # Modelle (BleTagManager/CarRegistry) statt hier als JSON-Blob zu laufen.
    settings_manager = SettingsManager(get_db)
    ble_tag_manager = BleTagManager(get_db)
    car_registry = CarRegistry(get_db)
    # nlu/iobroker.state_names/llm.system_prompt automatisch mit generischen Defaults
    # befüllen, falls die Kategorie noch leer ist (Neuinstallation, #114/#115).
    settings_manager.seed_defaults()

    # Hannah selbst als Roomie verlinken (für Trust-Level/Announcements über die
    # residents-Bridge) — einmalig, danach bereits über linked_accounts auffindbar.
    # external_id ist typ-qualifiziert (siehe Roomie.id) — vermeidet Kollisionen mit
    # einem gleichnamigen Guest/Pet; provider_payload trägt den Typ zurück, den die
    # ioBroker-Bridge für AgentSetResident braucht.
    _hannah_roomie = cfg.get("user_registry", {}).get("hannah_roomie", "hannah")
    _hannah_external_id = f"{_hannah_roomie}_roomie"
    if not _user_manager.get_user_by_linked_account("residents", _hannah_external_id):
        _hannah_user = _user_manager.get_user_by_username("hannah")
        if _hannah_user:
            _hannah_user.link_account(
                "residents", _hannah_external_id,
                provider_payload={"resident_type": "roomie", "roomie_id": _hannah_roomie},
            )

    # ioBroker
    iobroker_cfg = {**cfg.get("iobroker", {})}
    _state_names = settings_manager.get_settings_dict("iobroker").get("state_names")
    if _state_names:
        iobroker_cfg["state_names"] = _state_names
    iobroker = IoBrokerClient(iobroker_cfg)

    if not iobroker.rooms:
        log.warning("Keine Räume aus ioBroker geladen — NLU arbeitet ohne Raum-Erkennung.")

    # Room Manager (Räume, Gruppen) + Satellite Manager (Provisioning, Raum-/Owner-Zuweisung) —
    # teilen sich hannah.db mit der User-Registry
    room_manager = RoomManager(get_db)
    satellite_manager = SatelliteManager(
        get_db, cfg.get("satellite_manager", {}), user_manager=_user_manager,
        is_device_free=lambda d: _is_device_free(d),
        trigger_restart=lambda d: mqtt_handler.publish_restart(d),
    )

    # STT + NLU + TTS
    stt = STT(cfg.get("stt", {}))
    nlu_cfg = settings_manager.get_settings_dict("nlu") or cfg.get("nlu", {})
    # Wortliste "automations" entkoppelt gesprochene Phrase ("Autoresponder") vom internen
    # Automation-Key ("telegram_autoresponder") — gleiches Muster wie category_words. Auch an
    # ToolAgent gereicht (unten), damit der LLM-Pfad (Smalltalk-Lock/Unknown) dieselbe Quelle
    # kennt, statt nur über die NLU-Wortliste erreichbar zu sein (#138 Nachfassrunde).
    automation_words = settings_manager.get_settings_dict("automations")
    nlu_cfg["automation_words"] = automation_words
    nlu = NLU(nlu_cfg, iobroker.rooms, iobroker.devices)
    tts = TTS(cfg.get("tts", {}))

    llm = load_llm(cfg.get("llm", {}))
    llm_system_prompt: str = settings_manager.get_settings_dict("llm").get(
        "system_prompt"
    ) or cfg.get("llm", {}).get("system_prompt", "")
    tool_agent = ToolAgent(
        llm, iobroker, user_manager=_user_manager, automations=automation_words,
        # grpc_servicer ist erst weiter unten definiert — Lambda löst das Forward-Reference-
        # Problem (gleiches Muster wie get_satellites/get_residents oben).
        push_automation_update=lambda user_id, automation, enabled: grpc_servicer.push_automation_update(user_id, automation, enabled),
    )

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
    _ANON_SOURCES = {"anon", "grpc-voice"}

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

    weather = WeatherCache()

    # Auto-Tracker: cars-Tabelle (CarRegistry, #115) mit Owner-User-IDs → Roomie-IDs
    # übersetzt (car_tracker.py kennt nur Roomie-IDs); cfg["cars"]/cfg["car"] bleiben
    # Fallback für Installationen, die noch nie migriert wurden.
    _car_cfgs = (
        car_registry.get_tracker_configs(_resolve_roomie_id)
        or cfg.get("cars")
        or ([cfg["car"]] if cfg.get("car") else [{}])
    )
    car_manager = CarManager([CarTracker(c) for c in _car_cfgs])

    audio_cfg = cfg.get("audio", {})

    # ------------------------------------------------------------------
    # Wecker-Attribuierung (#4): Sprecher (Voice-ID) → Satelliten-Owner → System-User "hannah".
    # pipeline() (reiner UDP-Pfad, kein Proxy/VoiceID) hat nie einen speaker_user_id und
    # landet damit praktisch immer bei Owner/System-User.

    _hannah_system_user_id: Optional[int] = None

    def _resolve_alarm_user_id(speaker_user_id, device: str) -> int:
        nonlocal _hannah_system_user_id
        if speaker_user_id:
            try:
                return int(speaker_user_id)
            except (TypeError, ValueError):
                pass
        owner = satellite_manager.get_satellite_owner(device)
        if owner:
            return owner
        if _hannah_system_user_id is None:
            hannah_user = _user_manager.get_user_by_username("hannah")
            _hannah_system_user_id = hannah_user.id if hannah_user else 0
        return _hannah_system_user_id

    # Timer-Attribuierung (#97): dieselbe Kette wie bei Alarmen — Sprecher (Voice-ID) →
    # Satelliten-Owner — aber ohne System-User-Fallback: ein Timer ohne auflösbaren
    # Besitzer bleibt None (unbekannt/anonym), statt ihn "hannah" zuzuschreiben.
    def _resolve_timer_user_id(speaker_user_id, device: Optional[str]) -> Optional[int]:
        if speaker_user_id:
            try:
                return int(speaker_user_id)
            except (TypeError, ValueError):
                pass
        if device:
            owner = satellite_manager.get_satellite_owner(device)
            if owner:
                return owner
        return None

    _WEEKDAY_NAMES_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]

    def _next_alarm_date(time_str: str) -> datetime.date:
        """Ersetzt next_alarm_dt() aus der alten JSON-AlarmManager-Implementierung —
        nächstes Datum (heute oder morgen) für eine Uhrzeit ohne Wochentagsangabe."""
        h, m = map(int, time_str.split(":"))
        now = datetime.datetime.now()
        dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if dt <= now:
            dt += datetime.timedelta(days=1)
        return dt.date()

    def _alarm_weekday_to_date(weekday: int) -> datetime.date:
        today = datetime.date.today()
        return today + datetime.timedelta(days=(weekday - today.weekday()) % 7)

    def _format_alarm_date(d: datetime.date) -> str:
        today = datetime.date.today()
        if d == today:
            return "heute"
        if d == today + datetime.timedelta(days=1):
            return "morgen"
        return _WEEKDAY_NAMES_DE[d.weekday()]

    def _format_alarm_list(records: list[dict], requesting_device: str) -> str:
        if not records:
            return "Du hast keine Wecker gestellt."
        parts = []
        for r in records:
            if r["weekdays"]:
                when = ", ".join(_WEEKDAY_NAMES_DE[w] for w in r["weekdays"])
            else:
                when = _format_alarm_date(datetime.date.fromisoformat(r["one_shot_date"]))
            loc = f" auf {r['satellite_id']}" if r["satellite_id"] != requesting_device else ""
            parts.append(f"{when} um {r['time']} Uhr{loc}")
        return "Deine Wecker: " + "; ".join(parts) + "."

    def _resolve_alarm_confirmation(kind: str, payload: dict, text: str) -> str:
        """Antwortet auf die Ja/Nein-Rückfragen aus SetAlarm/DeleteAlarm (#4)."""
        answer = resolve_yes_no(text)
        if kind == "alarm_expand":
            if answer is True:
                weekday = payload["weekday"]
                alarm_manager.update_alarm(
                    payload["alarm_id"], payload["satellite_id"], payload["time"],
                    weekdays=list(range(weekday, 5)), skip_dates=[], one_shot_date=None, enabled=True,
                )
                return f"Ok, Wecker von {_WEEKDAY_NAMES_DE[weekday]} bis Freitag angelegt."
            return "Ok, bleibt bei dem einen Termin."
        if kind == "alarm_delete_series":
            if answer is True:
                alarm_manager.delete_alarm(payload["alarm_id"])
                return "Ok, den ganzen Wecker gelöscht."
            return "Ok, der Rest der Serie bleibt bestehen."
        return ""

    # ------------------------------------------------------------------
    # Kern-Pipeline: numpy-Array → Intent → Gerät schalten (Sprach-Pfad)

    def pipeline(device: str, audio_array, publish_error, publish_answer):
        # Activity-Log (#220): kein Return-Wert in dieser Funktion (Callback-basiert),
        # daher Erfassung über lokale Wrapper statt Umbau der Dispatch-Logik. publish_answer
        # ist ein Parameter (Shadowing ohne UnboundLocalError-Risiko); _feedback ist der
        # oben umbenannte, bisher globale _handle_feedback-Aufruf — bewusst umbenannt statt
        # geshadowt, da ein lokales "def _handle_feedback" den Zugriff auf die äußere
        # Funktion (gebraucht für den eigentlichen Aufruf) unmöglich gemacht hätte.
        _orig_publish_answer = publish_answer
        _captured_answer = [None]
        intent = None

        def publish_answer(text):
            _captured_answer[0] = text
            _orig_publish_answer(text)

        def _feedback(satellite_device, is_success, text):
            if text:
                _captured_answer[0] = text
            _handle_feedback(satellite_device, is_success, text)

        def _log_pipeline_activity():
            activity_log.log_activity(
                channel_type="satellite", channel_id=device, raw_text=locals_text[0],
                intent=intent, answer_text=_captured_answer[0] or "", audio_array=audio_array,
            )

        locals_text = [""]

        # STT
        try:
            text, no_speech_prob = stt.transcribe(audio_array)
        except Exception as e:
            log.error(f"[{device}] STT fehlgeschlagen: {e}")
            publish_error(f"STT: {e}")
            _log_pipeline_activity()
            return

        locals_text[0] = text

        if not text:
            log.debug(f"[{device}] Keine Sprache erkannt (no_speech={no_speech_prob:.2f})")
            _log_pipeline_activity()
            return

        log.info(f"[{device}] Text: '{text}'")

        # Phrase-Trigger-Check vor NLU (#139, Nachfolger des alten Routine-Checks)
        phrase_reply = trigger_engine.match_phrase(text)
        if phrase_reply is not None:
            _feedback(device, True, phrase_reply)
            _log_pipeline_activity()
            return

        # Offene Rückfrage auflösen
        if conv_ctx.has_clarification(device):
            clarification = conv_ctx.get_clarification(device)
            kind = clarification.get("type", "room")
            if kind in ("alarm_expand", "alarm_delete_series"):
                conv_ctx.clear_clarification(device)
                reply = _resolve_alarm_confirmation(kind, clarification["payload"], text)
                _feedback(device, True, reply)
                _log_pipeline_activity()
                return
            resolved = resolve_clarification_answer(text, clarification["candidates"])
            if resolved:
                conv_ctx.clear_clarification(device)
                orig: Intent = clarification["intent"]
                orig.room    = resolved[1]
                orig.room_id = resolved[0]
                count = iobroker.execute(orig, satellite_device=device)
                conv_ctx.update_from_intent(device, orig)
                if count == 0:
                    _feedback(device, False, "Tut mir leid, ich weiß nicht was du meinst.")
                intent = orig
                _log_pipeline_activity()
                return
            # Keine Übereinstimmung → Rückfrage verwerfen, normal weiterverarbeiten
            conv_ctx.clear_clarification(device)

        # NLU
        intent = nlu.parse(text)

        # Gesprächskontext: fehlende Felder ergänzen + Aktion erben
        conv_ctx.fill_intent(device, intent)
        conv_ctx.inherit_action(device, intent)

        # Raum-Fallback: zugewiesener Raum aus SatelliteManager → nichts
        # Bei Query-Intents nur anwenden wenn der Raum explizit im Text genannt wurde —
        # ohne Raum soll die globale Abfrage greifen.
        if intent.room is None and intent.name not in ("Query", "CarQuery"):
            room = satellite_manager.get_satellite_room(device)
            if room:
                intent.room    = room
                intent.room_id = room
                log.debug(f"[{device}] Raum-Fallback: '{room}'")

        log.info(
            f"[{device}] Intent: {intent.name} | "
            f"Raum: {intent.room} | Gerät: {intent.device} | Wert: {intent.value}"
        )

        # Mehrdeutiger Raum → Rückfrage stellen
        if intent.candidates:
            question = build_clarification_question(intent.candidates)
            conv_ctx.set_clarification(device, intent, intent.candidates)
            _feedback(device, True, question)
            _log_pipeline_activity()
            return

        if intent.name == "CarQuery":
            answer = car_manager.answer_for_roomie(scope=intent.value or "all")
            publish_answer(answer)
        elif intent.name == "WeatherQuery":
            publish_answer(weather.build_answer(scope=intent.value or "today"))
        elif intent.name == "TimeQuery":
            publish_answer(_time_answer())
        elif intent.name == "DateQuery":
            publish_answer(_date_answer())
        elif intent.name == "SetPresence":
            if intent.value == "away":
                log.info(f"[{device}] SetPresence away — Sprecher unbekannt, Status nicht gesetzt.")
                _feedback(device, True, "Tschüss! Bis bald.")
            else:
                log.info(f"[{device}] SetPresence home — Sprecher unbekannt, Status nicht gesetzt.")
                _feedback(device, True, "Willkommen zuhause!")
        elif intent.name in ("StopIntent", "PauseIntent", "ResumeIntent"):
            cmd_type = {"StopIntent": "stop", "PauseIntent": "pause", "ResumeIntent": "resume"}[intent.name]
            targets = _resolve_targets(intent.room_id or device)
            if intent.name == "StopIntent":
                for t in targets:
                    if alarm_manager.is_ringing(t):
                        alarm_manager.stop_ringing(t)
            for t in targets:
                udp_server.send_command(t, {"type": cmd_type})
        elif intent.name == "SetVolume":
            targets = _resolve_targets(intent.room_id or device)
            if not targets:
                _feedback(device, False, "Keinen Satelliten gefunden.")
            else:
                level = _apply_volume(targets, intent.value, intent.unit)
                if intent.unit == "relative":
                    reply = "Lauter." if intent.value > 0 else "Leiser."
                else:
                    reply = f"Lautstärke auf {level} Prozent gesetzt."
                _feedback(device, True, reply)
        elif intent.name == "SetTimer":
            seconds = int(intent.value)
            label = intent.label or format_duration(seconds)
            timer_id = str(uuid.uuid4())
            fire_at = int(time.time()) + seconds
            room_id = intent.room_id or "all"
            metadata = {"room": room_id}
            user_id = _resolve_timer_user_id("", device)
            if user_id:
                metadata["user_id"] = str(user_id)
            grpc_servicer.timer_create(timer_id, label, fire_at, metadata)
            reply = f"Timer für {format_duration(seconds)} gesetzt."
            if intent.label:
                reply = f"Timer für {format_duration(seconds)} gesetzt: {intent.label}."
            _feedback(device, True, reply)
        elif intent.name == "SetAlarm":
            user_id = _resolve_alarm_user_id(None, device)
            weekday = intent.weekdays[0] if intent.weekdays else None
            if weekday is not None:
                target_date = _alarm_weekday_to_date(weekday)
                record = alarm_manager.create_alarm(device, intent.value, None, target_date.isoformat(), user_id)
                weekday_name = _WEEKDAY_NAMES_DE[weekday]
                conv_ctx.set_clarification(device, None, [], kind="alarm_expand", payload={
                    "alarm_id": record["id"], "satellite_id": device, "time": intent.value, "weekday": weekday,
                })
                _feedback(device, True, (
                    f"Wecker für {weekday_name} um {intent.value} Uhr gestellt. "
                    f"Soll ich den Wecker von {weekday_name} bis Freitag anlegen?"
                ))
            else:
                target_date = _next_alarm_date(intent.value)
                alarm_manager.create_alarm(device, intent.value, None, target_date.isoformat(), user_id)
                label = _format_alarm_date(target_date)
                _feedback(device, True, f"Wecker gestellt für {label} um {intent.value} Uhr.")
        elif intent.name == "DeleteAlarm":
            if intent.resolved_date is None:
                _feedback(device, False, "Für welchen Tag soll ich den Wecker löschen?")
            else:
                matches = alarm_manager.find_occurrences(None, intent.resolved_date)
                if intent.value:
                    matches = [m for m in matches if m["time"] == intent.value]
                if not matches:
                    _feedback(device, False, "Ich habe dafür keinen Wecker gefunden.")
                else:
                    series_matches = [m for m in matches if m["weekdays"]]
                    for m in matches:
                        if m["weekdays"]:
                            alarm_manager.skip_occurrence(m["id"], intent.resolved_date.isoformat())
                        else:
                            alarm_manager.delete_alarm(m["id"])
                    reply = f"Ok, habe den Wecker für {_format_alarm_date(intent.resolved_date)} gelöscht."
                    if len(series_matches) == 1:
                        weekday_name = _WEEKDAY_NAMES_DE[intent.resolved_date.weekday()]
                        conv_ctx.set_clarification(device, None, [], kind="alarm_delete_series",
                                                    payload={"alarm_id": series_matches[0]["id"]})
                        reply += f" Soll ich den Wecker für {weekday_name} bis Freitag löschen?"
                    _feedback(device, True, reply)
        elif intent.name == "QueryAlarms":
            _feedback(device, True, _format_alarm_list(alarm_manager.get_alarm_records(), device))
        elif intent.name == "SetDND":
            active = intent.value == "on"
            _apply_global_dnd(active)
            _feedback(device, True, "Nicht stören aktiv." if active else "Nicht stören deaktiviert.")
        elif intent.name == "SetMute":
            active = intent.value == "on"
            _apply_global_mute(active)
            _feedback(device, True, "Mikrofone stumm." if active else "Mikrofone wieder aktiv.")
        elif intent.name == "Smalltalk":
            history = conv_ctx.get_llm_history(device)
            answer = llm.chat(text, system_prompt=prepare_prompt(llm_system_prompt, iobroker), history=history)
            conv_ctx.add_llm_exchange(device, text, answer)
            _maybe_reopen_smalltalk_mic(device)
            _feedback(device, True, answer)
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
                _feedback(device, False, "Tut mir leid, ich habe dich nicht verstanden.")
            elif count == 0:
                log.warning(f"[{device}] Keine States gesetzt — Intent nicht auflösbar.")
                _feedback(device, False, "Tut mir leid, ich weiß nicht was du meinst.")

        _log_pipeline_activity()

    # ------------------------------------------------------------------
    def _speaker_context(speaker_user_id: str) -> str:
        """Gibt einen Zusatz-Abschnitt für den System-Prompt zurück der Sprecher-Info enthält."""
        if not speaker_user_id:
            return ""
        user = _user_manager.get_user_by_id(speaker_user_id)
        if not user:
            return f"\n\nDie Person die gerade mit dir spricht heißt {speaker_user_id}."
        name        = user.display_name
        trust_level = user.trust_level
        # relationship_level: noch nicht implementiert, Platzhalter für spätere Erweiterung
        mem = memory.format_for_prompt(speaker_user_id)
        return (
            f"\n\nDie Person die gerade mit dir spricht heißt {name}."
            f" Vertrauenslevel: {trust_level}/10."
            f"{mem}"
        )

    def _handle_text(
        text: str, speaker_user_id: str = "", source: str = "", device: str = "",
        channel_type: str = "", channel_id: str = "", audio_array=None, sample_rate: int = 16000,
    ) -> tuple[str, str]:
        """
        Verarbeitet einen Text-Befehl durch NLU und gibt (Antwort, Intent-Name) zurück.
        Kein TTS — reines Text-in/Text-out.
        Wird vom gRPC-Server (Telegram/Satelliten/ioBroker-Adapter) genutzt.

        speaker_user_id: optionale Hannah-User-ID aus Voice-ID-Erkennung.
        source: Kontext-Schlüssel (Gerät, Roomie-ID, Kanal). Leer = speaker_user_id oder "anon".
        device: Satelliten-Device-ID für Raum-Fallback (nur gesetzt bei Satelliten-Audio).
        channel_type/channel_id: Activity-Log-Kanal (#220), vom Aufrufer explizit gesetzt
        (z.B. "telegram"/"satellite"/"iobroker") statt aus source/device zurückgeraten.
        audio_array: Roh-Audio fürs Activity-Log (nur bei Voice-Kanälen vorhanden).
        """
        _source = source or speaker_user_id or "anon"

        def _logged(answer: str, intent_name: str, intent=None) -> tuple[str, str]:
            # activity_log.user_id ist INTEGER — speaker_user_id kommt vom VoiceID-Pfad
            # aber ungeprüft durch (VoiceID kennt nur ein beim Enrollment frei gewähltes
            # String-Label, siehe #220-Folgefehler); Fallback auf Username-Lookup löst
            # z.B. ein versehentlich mit Klarnamen statt User-ID enrolltes Profil auf.
            resolved_user = speaker_user_id and (
                _user_manager.get_user_by_id(speaker_user_id)
                or _user_manager.get_user_by_username(speaker_user_id)
            )
            activity_log.log_activity(
                channel_type=channel_type or "grpc_text", channel_id=channel_id,
                user_id=str(resolved_user.id) if resolved_user else "", raw_text=text, intent=intent,
                answer_text=answer, audio_array=audio_array, sample_rate=sample_rate,
            )
            return answer, intent_name

        phrase_reply = trigger_engine.match_phrase(text)
        if phrase_reply is not None:
            return _logged(phrase_reply, "Trigger")

        # Smalltalk-Modus: LLM-Classifier vor NLU schalten
        if conv_ctx.is_smalltalk_active(_source):
            history = conv_ctx.get_llm_history(_source)
            verdict = llm.classify(text, history=history)
            if verdict == "SMALLTALK":
                log.debug(f"[{_source}] Classifier → SMALLTALK (Modus aktiv)")
                sp = prepare_prompt(llm_system_prompt, iobroker) + _speaker_context(speaker_user_id)
                answer = llm.chat(text, system_prompt=sp, history=history)
                conv_ctx.add_llm_exchange(_source, text, answer)
                return _logged(answer, "Smalltalk")
            if verdict == "NOT_ADDRESSED":
                # #159 — erkennbar nicht an Hannah gerichtet (z.B. Fremdgespräch im offenen
                # Follow-up-Mic-Fenster). Bewusst kein Fallthrough in die NLU-Verarbeitung,
                # sonst würde zufällig aufgenommenes Gespräch als Gerätebefehl interpretiert.
                log.debug(f"[{_source}] Classifier → NOT_ADDRESSED (Modus aktiv, ignoriert)")
                return _logged("", "Ignored")
            log.debug(f"[{_source}] Classifier → COMMAND (Modus aktiv, weiter mit NLU)")

        if conv_ctx.has_clarification(_source):
            clarification = conv_ctx.get_clarification(_source)
            kind = clarification.get("type", "room")
            if kind in ("alarm_expand", "alarm_delete_series"):
                conv_ctx.clear_clarification(_source)
                return _logged(_resolve_alarm_confirmation(kind, clarification["payload"], text), "Alarm")
            resolved = resolve_clarification_answer(text, clarification["candidates"])
            if resolved:
                conv_ctx.clear_clarification(_source)
                orig: Intent = clarification["intent"]
                orig.room    = resolved[1]
                orig.room_id = resolved[0]
                count = iobroker.execute(orig)
                conv_ctx.update_from_intent(_source, orig)
                return _logged(("OK." if count > 0 else "Keine Geräte gefunden."), "Routine", orig)
            conv_ctx.clear_clarification(_source)

        intent = nlu.parse(text)

        # Gesprächskontext: fehlende Felder ergänzen + Aktion erben
        conv_ctx.fill_intent(_source, intent)
        conv_ctx.inherit_action(_source, intent)

        # Raum-Fallback: zugewiesener Raum des Satelliten, falls kein Raum im Text genannt wurde
        # (analog zum UDP-direkt-Pfad in pipeline() — ging im gRPC/Proxy-Pfad verloren)
        if device and intent.room is None and intent.name not in ("Query", "CarQuery"):
            room = satellite_manager.get_satellite_room(device)
            if room:
                intent.room    = room
                intent.room_id = room
                log.debug(f"[{device}] Raum-Fallback: '{room}'")

        log.info(
            f"[textcmd] Text: '{text}' → Intent: {intent.name} | "
            f"Raum: {intent.room} | Gerät: {intent.device} | Wert: {intent.value} | SpeakerUser: {speaker_user_id}"
        )

        if intent.candidates:
            question = build_clarification_question(intent.candidates)
            conv_ctx.set_clarification(_source, intent, intent.candidates)
            return _logged(question, "Clarification", intent)

        if intent.name == "CarQuery":
            answer = car_manager.answer_for_roomie(scope=intent.value or "all", roomie_id=_resolve_roomie_id(speaker_user_id))
        elif intent.name == "WeatherQuery":
            answer = weather.build_answer(scope=intent.value or "today")
        elif intent.name == "TimeQuery":
            answer = _time_answer()
        elif intent.name == "DateQuery":
            answer = _date_answer()
        elif intent.name == "SetPresence":
            roomie_id = _resolve_roomie_id(speaker_user_id) if speaker_user_id else ""
            if intent.value == "away":
                if roomie_id:
                    residents.set_user_away(roomie_id)
                else:
                    log.info("SetPresence away — Sprecher anonym oder ohne Residents-Link, Status nicht gesetzt.")
                answer = "Tschüss!"
            else:
                if roomie_id:
                    residents.set_user_home(roomie_id)
                else:
                    log.info("SetPresence home — Sprecher anonym oder ohne Residents-Link, Status nicht gesetzt.")
                answer = "Willkommen zuhause!"
        elif intent.name in ("StopIntent", "PauseIntent", "ResumeIntent"):
            cmd_type = {"StopIntent": "stop", "PauseIntent": "pause", "ResumeIntent": "resume"}[intent.name]
            source_device = source if source in {**udp_server.registered_devices(), **grpc_servicer.proxy_satellites()} else None
            targets = _resolve_targets(intent.room_id or source_device or "all")
            if intent.name == "StopIntent":
                for t in targets:
                    if alarm_manager.is_ringing(t):
                        alarm_manager.stop_ringing(t)
            for t in targets:
                udp_server.send_command(t, {"type": cmd_type})
            answer = ""
        elif intent.name == "SetDND":
            active = intent.value == "on"
            _apply_global_dnd(active)
            answer = "Nicht stören aktiv." if active else "Nicht stören deaktiviert."
        elif intent.name == "SetMute":
            active = intent.value == "on"
            _apply_global_mute(active)
            answer = "Mikrofone stumm." if active else "Mikrofone wieder aktiv."
        elif intent.name == "SetAutomation":
            automation = intent.value.get("automation")
            enabled = intent.value.get("enabled", True)
            user = _user_manager.get_user_by_id(speaker_user_id) if speaker_user_id else None
            if not user:
                answer = "Ich weiß nicht, wer du bist — das kann ich so nicht einstellen."
            else:
                user.enable_automation(automation) if enabled else user.disable_automation(automation)
                grpc_servicer.push_automation_update(user.id, automation, enabled)
                answer = "Automation aktiviert." if enabled else "Automation deaktiviert."
        elif intent.name == "SetVolume":
            source_device = source if source in {**udp_server.registered_devices(), **grpc_servicer.proxy_satellites()} else None
            targets = _resolve_targets(intent.room_id or source_device or "all")
            if not targets:
                answer = "Keinen Satelliten gefunden."
            else:
                level = _apply_volume(targets, intent.value, intent.unit)
                if intent.unit == "relative":
                    answer = "Lauter." if intent.value > 0 else "Leiser."
                else:
                    answer = f"Lautstärke auf {level} Prozent gesetzt."
        elif intent.name == "SetTimer":
            seconds = int(intent.value)
            label = intent.label or format_duration(seconds)
            timer_id = str(uuid.uuid4())
            fire_at = int(time.time()) + seconds
            room_id = intent.room_id or "all"
            source_device = source if source in {**udp_server.registered_devices(), **grpc_servicer.proxy_satellites()} else None
            metadata = {"room": room_id}
            user_id = _resolve_timer_user_id(speaker_user_id, source_device)
            if user_id:
                metadata["user_id"] = str(user_id)
            grpc_servicer.timer_create(timer_id, label, fire_at, metadata)
            answer = f"Timer für {format_duration(seconds)} gesetzt."
            if intent.label:
                answer = f"Timer für {format_duration(seconds)} gesetzt: {intent.label}."
        elif intent.name == "SetAlarm":
            target = source if source in {**udp_server.registered_devices(), **grpc_servicer.proxy_satellites()} else None
            if target is None:
                answer = "Einen Wecker kann ich nur auf einem Satelliten stellen."
            else:
                user_id = _resolve_alarm_user_id(speaker_user_id, target)
                weekday = intent.weekdays[0] if intent.weekdays else None
                if weekday is not None:
                    target_date = _alarm_weekday_to_date(weekday)
                    record = alarm_manager.create_alarm(target, intent.value, None, target_date.isoformat(), user_id)
                    weekday_name = _WEEKDAY_NAMES_DE[weekday]
                    conv_ctx.set_clarification(_source, None, [], kind="alarm_expand", payload={
                        "alarm_id": record["id"], "satellite_id": target, "time": intent.value, "weekday": weekday,
                    })
                    answer = (
                        f"Wecker für {weekday_name} um {intent.value} Uhr gestellt. "
                        f"Soll ich den Wecker von {weekday_name} bis Freitag anlegen?"
                    )
                else:
                    target_date = _next_alarm_date(intent.value)
                    alarm_manager.create_alarm(target, intent.value, None, target_date.isoformat(), user_id)
                    answer = f"Wecker gestellt für {_format_alarm_date(target_date)} um {intent.value} Uhr."
        elif intent.name == "DeleteAlarm":
            if intent.resolved_date is None:
                answer = "Für welchen Tag soll ich den Wecker löschen?"
            else:
                matches = alarm_manager.find_occurrences(None, intent.resolved_date)
                if intent.value:
                    matches = [m for m in matches if m["time"] == intent.value]
                if not matches:
                    answer = "Ich habe dafür keinen Wecker gefunden."
                else:
                    series_matches = [m for m in matches if m["weekdays"]]
                    for m in matches:
                        if m["weekdays"]:
                            alarm_manager.skip_occurrence(m["id"], intent.resolved_date.isoformat())
                        else:
                            alarm_manager.delete_alarm(m["id"])
                    answer = f"Ok, habe den Wecker für {_format_alarm_date(intent.resolved_date)} gelöscht."
                    if len(series_matches) == 1:
                        weekday_name = _WEEKDAY_NAMES_DE[intent.resolved_date.weekday()]
                        conv_ctx.set_clarification(_source, None, [], kind="alarm_delete_series",
                                                    payload={"alarm_id": series_matches[0]["id"]})
                        answer += f" Soll ich den Wecker für {weekday_name} bis Freitag löschen?"
        elif intent.name == "QueryAlarms":
            answer = _format_alarm_list(alarm_manager.get_alarm_records(), source)
        elif intent.name == "Smalltalk":
            sp = prepare_prompt(llm_system_prompt, iobroker) + _speaker_context(speaker_user_id)
            history = conv_ctx.get_llm_history(_source)
            answer = tool_agent.run(text, system_prompt=sp, history=history, user_id=speaker_user_id)
            if answer:
                conv_ctx.add_llm_exchange(_source, text, answer)
                conv_ctx.set_smalltalk_active(_source, True)
            else:
                answer = "Das habe ich leider nicht verstanden."
        elif intent.name == "Query":
            answer = iobroker.answer_query(intent) or "Keine Antwort verfügbar."
            conv_ctx.update_from_intent(_source, intent)
        elif intent.name == "Unknown":
            sp = prepare_prompt(llm_system_prompt, iobroker) + _speaker_context(speaker_user_id)
            history = conv_ctx.get_llm_history(_source)
            answer = tool_agent.run(text, system_prompt=sp, history=history, user_id=speaker_user_id)
            if answer:
                conv_ctx.add_llm_exchange(_source, text, answer)
            else:
                answer = "Das habe ich leider nicht verstanden."
        else:
            count = iobroker.execute(intent)
            if count > 0:
                conv_ctx.set_smalltalk_active(_source, False)
            answer = "Keine Geräte gefunden." if count == 0 else f"OK, {count} Gerät(e) geschaltet."
            conv_ctx.update_from_intent(_source, intent)

        return _logged(answer, intent.name, intent)

    # ── Satellit-Steuerung: Volume / Mute / DND ───────────────────────────────
    _global_volume: int = 80          # 0-100
    _device_volume: dict[str, int] = {}
    _device_mute:   dict[str, bool] = {}
    _device_dnd:    dict[str, bool] = {}

    # Activity-Tracking für den periodischen Rejuvenation-Restart (#162): statt eines
    # echten busy=True/False-Paars (fehleranfällig, da die Smalltalk-Follow-up-Logik
    # in einem eigenen Thread noch lange nach _handle_satellite_audio weiterläuft) wird
    # nur ein Zeitstempel je Gerät aktualisiert — "frei" heißt: seit dem letzten Touch
    # ist das Quiet-Window verstrichen. _device_dnd deckt zusätzlich Capture-/Sampling-
    # Modus ab (_on_set_capture setzt DND bereits).
    _device_last_activity: dict[str, float] = {}
    _FREE_QUIET_WINDOW_S = 60.0

    def _touch_activity(device: str) -> None:
        _device_last_activity[device] = time.monotonic()

    def _is_device_free(device: str) -> bool:
        last = _device_last_activity.get(device, 0.0)
        return (time.monotonic() - last) > _FREE_QUIET_WINDOW_S and not _device_dnd.get(device, False)

    def _send_audio(target: str, pcm: bytes, rate: int, label: str = ""):
        """Sendet PCM an einen Satelliten."""
        if grpc_servicer.has_proxy():
            grpc_servicer.stream_audio_to_proxy(target, pcm, rate)
            log.info(f"{label}Announcement → {target} (via Proxy, streamed)")
        else:
            udp_server.send_tts(target, pcm, sample_rate=rate)
            log.info(f"{label}Announcement → {target} (via UDP)")

    def _resolve_targets(device: str = "", label: str = "", *, room_id: str = "", user_id: int = 0) -> list[str]:
        """Löst device/room/group/'all' auf eine Liste von Ziel-Geräten auf.

        room_id/user_id (#31) sind der neue, eindeutige Pfad über die DB-Zuweisung
        (Raum und/oder Person, AND-Verknüpfung wenn beide gesetzt) — wenn gesetzt,
        hat das Vorrang vor der alten device-String-Auflösung (Device-ID/Raumname/
        Gruppenname/"all").
        """
        all_devices = {**udp_server.registered_devices(), **grpc_servicer.proxy_satellites()}

        if room_id or user_id:
            if user_id:
                candidates = {s["device_id"] for s in satellite_manager.get_user_satellites(user_id)}
                if room_id:
                    candidates &= set(satellite_manager.get_room_satellite_ids(room_id))
            else:
                candidates = set(satellite_manager.get_room_satellite_ids(room_id))
            targets = [d for d in candidates if d in all_devices]
            if not targets:
                log.warning(f"{label}kein verbundener Satellit für room_id={room_id!r} user_id={user_id} — ignoriert.")
            return targets

        if device == "all":
            return list(all_devices.keys())
        if device in all_devices:
            return [device]
        room_lower = device.lower()

        # DB-Raum-Overrides laden (eine Query für alle Satelliten)
        db_room_map = satellite_manager.get_satellite_room_map()

        def _satellite_room(d: str, self_reported: str) -> str:
            return db_room_map.get(d, self_reported).lower()

        # Raum-Match: DB-Zuweisung hat Vorrang vor Eigenangabe
        targets = [d for d, r in all_devices.items() if _satellite_room(d, r) == room_lower]

        if not targets:
            # Gruppen referenzieren Satelliten direkt (#56)
            db_groups = room_manager.get_group_satellite_map()
            if room_lower in db_groups:
                targets = [d for d in db_groups[room_lower] if d in all_devices]

        if not targets:
            log.warning(f"{label}kein Satellit in Raum/Gruppe '{device}' — ignoriert.")
        return targets

    # ── Volume/Mute/DND Callbacks ─────────────────────────────────────────────

    def _on_volume(device: Optional[str], level: int):
        nonlocal _global_volume
        if device:
            _device_volume[device] = level
            log.info(f"Lautstärke {device}: {level}%")
            all_devices = {**udp_server.registered_devices(), **grpc_servicer.proxy_satellites()}
            room = all_devices.get(device, "")
            display_name = satellite_manager.resolve_satellite_name(device) or ""
            grpc_servicer.agent_satellite_update(device, room, "", True, volume=level, display_name=display_name)
        else:
            _global_volume = level
            for d in _resolve_targets("all"):
                _device_volume[d] = level
                mqtt_handler.publish_volume_set(d, level)
            log.info(f"Lautstärke global: {level}%")

    def _on_mute(device: str, muted: bool):
        if _device_mute.get(device) == muted:
            return
        _device_mute[device] = muted
        log.info(f"Mute {device}: {muted}")
        all_devices = {**udp_server.registered_devices(), **grpc_servicer.proxy_satellites()}
        room = all_devices.get(device, "")
        display_name = satellite_manager.resolve_satellite_name(device) or ""
        grpc_servicer.agent_satellite_update(device, room, "", True, mute=muted, display_name=display_name)
        if room:
            for sibling, sibling_room in all_devices.items():
                if sibling != device and sibling_room.lower() == room.lower():
                    mqtt_handler.publish_mute_set(sibling, muted)
                    _device_mute[sibling] = muted

    def _on_dnd(device: str, active: bool):
        _device_dnd[device] = active
        mqtt_handler.publish_dnd_state(device, active)
        all_devices = {**udp_server.registered_devices(), **grpc_servicer.proxy_satellites()}
        room = all_devices.get(device, "")
        display_name = satellite_manager.resolve_satellite_name(device) or ""
        grpc_servicer.agent_satellite_update(device, room, "", True, dnd=active, display_name=display_name)
        log.info(f"DND {device}: {active}")

    def _apply_global_dnd(active: bool):
        """Setzt DND auf allen bekannten Satelliten und publiziert den globalen State."""
        all_devices = {**udp_server.registered_devices(), **grpc_servicer.proxy_satellites()}
        for device in _resolve_targets("all"):
            _device_dnd[device] = active
            mqtt_handler.publish_dnd_state(device, active)
            grpc_servicer.agent_satellite_update(device, all_devices.get(device, ""), "", True, dnd=active)
        log.info(f"Globales DND: {active}")

    def _apply_global_mute(active: bool):
        """Setzt Mute auf allen bekannten Satelliten."""
        for device in _resolve_targets("all"):
            _device_mute[device] = active
            mqtt_handler.publish_mute_set(device, active)
        log.info(f"Globales Mute: {active}")

    def _apply_volume(targets: list[str], value: float, unit: str) -> int:
        """Setzt die Lautstärke auf den gegebenen Zielen — absolut (unit='%') oder
        relativ (unit='relative', value als vorzeichenbehaftetes Delta). Gibt den
        resultierenden Level des ersten Ziels zurück (für die Sprachantwort)."""
        result = _global_volume
        for d in targets:
            current = _device_volume.get(d, _global_volume)
            new_level = current + int(value) if unit == "relative" else int(value)
            new_level = max(0, min(100, new_level))
            _device_volume[d] = new_level
            mqtt_handler.publish_volume_set(d, new_level)
            result = new_level
        log.info(f"Lautstärke {targets}: {result}%")
        return result

    # ── Announcements ─────────────────────────────────────────────────────────

    def process_announcement(device: str, text: str, *, ssml: bool = False, room_id: str = "", user_id: int = 0):
        """Synthetisiert Text/SSML per TTS und sendet ihn an Raum/Person/Gerät/alle Satelliten."""
        if not tts.enabled:
            log.warning("Announcement ignoriert — TTS ist nicht konfiguriert.")
            return
        result = tts.synthesize_ssml(text) if ssml else tts.synthesize(text)
        if not result:
            return
        pcm, rate = _resample_to_16k(*result)
        targets = _resolve_targets(device, room_id=room_id, user_id=user_id)
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
    mqtt_handler.set_volume_handler(_on_volume)
    mqtt_handler.set_mute_handler(_on_mute)
    mqtt_handler.set_dnd_handler(_on_dnd)

    # BLE-Lokalisierung: Tags kommen aus der ble_tags-Tabelle (BleTagManager, #115) mit
    # user_id bereits aufgelöst — Fallback auf cfg["ble"]["tags"] für un-migrierte Installs.
    ble_cfg = {**cfg.get("ble", {})}
    _ble_tag_records = ble_tag_manager.get_tag_records()
    if _ble_tag_records:
        ble_cfg["tags"] = _ble_tag_records

    def _get_satellite_room(device: str) -> Optional[str]:
        all_devices = {**udp_server.registered_devices(), **grpc_servicer.proxy_satellites()}
        return all_devices.get(device)

    ble_engine = BleLocationEngine(ble_cfg, _get_satellite_room)
    alarm_manager = AlarmManager(
        db=get_db,
        on_fire=lambda record: _on_alarm_fire(record),
        play_asset_fn=mqtt_handler.publish_play_asset,
        set_volume_fn=mqtt_handler.publish_volume_set,
        get_volume_fn=lambda d: _device_volume.get(d, _global_volume),
        # TTS-Fallback, falls der Satellit einen play_asset-Versuch nackt (#116) —
        # _handle_feedback ist erst weiter unten definiert, gleiches Forward-Reference-
        # Muster wie _on_alarm_fire oben.
        announce_fn=lambda d, text: _handle_feedback(d, True, text),
        reset_playback_done_fn=mqtt_handler.reset_playback_done,
        wait_playback_done_fn=mqtt_handler.wait_for_playback_done,
    )
    mqtt_handler.set_play_asset_result_handler(
        lambda device, asset_id, ok: alarm_manager.on_play_result(device, asset_id, ok)
    )

    def _on_ble_location_change(tag : BleTag, room, satellite, rssi):
        room_str = room or ""
        sat_str = satellite or ""
        payload = json.dumps({"label": tag.label, "mac": tag.mac, "room": room_str,
                              "satellite": sat_str, "rssi": rssi}, ensure_ascii=False)
        mqtt_handler.publish_raw(f"hannah/ble/{tag.label}/location", payload)
        grpc_servicer.agent_ble_update(tag.label, tag.mac, room_str, sat_str, rssi)

        # BLE-Sichtung ist ein starkes "zuhause"-Signal, aber kein zuverlässiges
        # "weg"-Signal (schwacher Empfang ≠ Haus verlassen) — daher nur bei aktiver
        # Sichtung (room gesetzt) presence_state auf HOME setzen, nie zurücksetzen.
        if tag.user_id and room is not None:
            user = _user_manager.get_user_by_id(tag.user_id)
            if user is None:
                return
            user.presence = True

    ble_engine.set_location_change_handler(_on_ble_location_change)
    mqtt_handler.set_ble_report_handler(ble_engine.on_report)

    # Satellite-Online-Tracking: diff berechnen und per-device online/offline publishen
    _known_satellites: set[str] = set()
    _prev_satellite_map: dict[str, str] = {}

    def _on_satellite_change(satellite_map: dict[str, str]):
        nonlocal _known_satellites, _prev_satellite_map
        current = set(satellite_map.keys())
        ble_macs = ble_engine.get_all_macs()
        for device_id in current - _known_satellites:
            display_name = satellite_manager.resolve_satellite_name(device_id) or ""
            grpc_servicer.agent_satellite_update(device_id, satellite_map[device_id], "", True, display_name=display_name)
            if not grpc_servicer.is_captured(device_id):
                # Stelle sicher, dass kein retained Capture-Modus aus einer
                # vorherigen Hannah-Session am Satelliten hängen geblieben ist.
                mqtt_handler.publish_sampling_mode(device_id, False)
            if ble_macs:
                mqtt_handler.publish_ble_watchlist(device_id, ble_macs)
            mqtt_handler.publish_asset_relevant(device_id, RELEVANT_ASSET_IDS)
        for device_id in _known_satellites - current:
            grpc_servicer.agent_satellite_update(device_id, _prev_satellite_map.get(device_id, ""), "", False)
        _known_satellites = current
        _prev_satellite_map = dict(satellite_map)
    def _rephrase_text(text: str) -> str:
        """Lässt Hannah (LLM) einen Announcement-Text frei umformulieren.
        Gibt den Originaltext zurück wenn kein LLM verfügbar oder ein Fehler auftritt."""
        if llm is None:
            return text
        try:
            prompt = (
                "Du bist Hannah, eine 24-jährige Mitbewohnerin. "
                "Formuliere den folgenden Satz kurz und natürlich um — "
                "du redest mit deiner Mitbewohnerin, nicht mit einem Kunden. "
                "Behalte alle konkreten Details bei. "
                "Antworte nur mit dem umformulierten Satz, ohne Erklärung."
            )
            result = llm.chat(text, system_prompt=prompt)
            return result.strip() if result and result.strip() else text
        except Exception as e:
            log.warning(f"LLM-Rephrase fehlgeschlagen, nutze Original: {e}")
            return text

    def _maybe_reopen_smalltalk_mic(device: str) -> None:
        """Öffnet nach einer Smalltalk-Antwort das Mic des Satelliten erneut, wenn
        dieser das Follow-up-Listening-Setting aktiviert hat (#158). Das Silence-
        Timeout dafür lebt bereits in der Satelliten-Firmware (hannah_audio.c,
        8s-Fenster nach `listen`) — Core muss den Trigger nur erneut schicken,
        sobald die aktuelle TTS-Wiedergabe fertig abgespielt ist."""
        if _device_mute.get(device):
            return
        if not satellite_manager.get_satellite_smalltalk_followup(device):
            return
        mqtt_handler.reset_playback_done(device)

        def _reopen():
            mqtt_handler.wait_for_playback_done(device, timeout=15.0)
            mqtt_handler.publish_listen(device)
            _touch_activity(device)

        threading.Thread(target=_reopen, daemon=True, name="smalltalk-followup").start()

    # Pending-Fragen: {room: (callback, timeout_timer)}
    # Wenn Hannah eine Frage stellt, wird die nächste Äußerung aus dem Raum
    # als Antwort gewertet und direkt an den Callback übergeben statt an NLU.
    _pending_questions: dict[str, tuple[Callable, threading.Timer]] = {}
    _pending_lock = threading.Lock()

    def _ask_fn(room: str, question: str, callback: Callable[[str], None]) -> None:
        process_announcement(room, question)
        for target in _resolve_targets(room):
            mqtt_handler.publish_listen(target)

        def _on_timeout():
            with _pending_lock:
                _pending_questions.pop(room, None)
            log.info(f"Pending-Frage für Raum '{room}' abgelaufen (keine Antwort in 60s).")

        timer = threading.Timer(60.0, _on_timeout)
        with _pending_lock:
            old = _pending_questions.pop(room, None)
            if old:
                old[1].cancel()
            _pending_questions[room] = (callback, timer)
        timer.start()

    def _try_answer_pending(device: str, room: str, transcript: str) -> bool:
        """Prüft ob eine offene Frage für diesen Raum wartet und leitet die Antwort weiter.
        Gibt True zurück wenn die Äußerung als Antwort konsumiert wurde."""
        with _pending_lock:
            entry = _pending_questions.pop(room, None)
        if not entry:
            return False
        callback, timer = entry
        timer.cancel()
        log.info(f"[{device}] Antwort auf offene Frage im Raum '{room}': {transcript!r}")
        threading.Thread(target=callback, args=(transcript,), daemon=True, name="trigger-answer").start()
        return True

    def _trigger_set_state(state_id: str, value: object) -> None:
        grpc_servicer.agent_set_state(state_id, value)

    def _schedule_trigger_timer(timer_id: str, label: str, fire_at: int, metadata: dict) -> None:
        grpc_servicer.timer_create(timer_id, label, fire_at, metadata)

    def _cancel_trigger_timer(timer_id: str) -> None:
        grpc_servicer.timer_cancel(timer_id)

    def _on_trigger_change() -> None:
        grpc_servicer.agent_watch_more(list(trigger_engine.get_referenced_state_ids()))

    trigger_engine = TriggerEngine(
        db=get_db,
        announce_fn=process_announcement,
        rephrase_fn=_rephrase_text,
        ask_fn=_ask_fn,
        match_fn=llm.match,
        set_state_fn=_trigger_set_state,
        schedule_timer_fn=_schedule_trigger_timer,
        cancel_timer_fn=_cancel_trigger_timer,
        on_change=_on_trigger_change,
    )

    def _on_state_update(state_id: str, raw: str) -> None:
        iobroker.handle_state_update(state_id, raw)
        trigger_engine.on_state_update(state_id, raw)

    def _json_to_raw(json_value: str) -> str:
        """Decode a JSON-encoded gRPC state value to a plain string for legacy handlers."""
        import json as _json
        try:
            parsed = _json.loads(json_value)
            if isinstance(parsed, bool):
                return "true" if parsed else "false"
            return str(parsed)
        except (ValueError, TypeError):
            return json_value

    # ------------------------------------------------------------------
    # Voice-Pipeline für gRPC: OGG/Opus → STT → NLU → TTS → OGG/Opus
    # Wird von SubmitVoice (Telegram, zukünftige Services) genutzt.

    def _handle_voice(
        audio_ogg: bytes, speaker_user_id: str = "", channel_type: str = "", channel_id: str = "",
    ) -> tuple[str, str, str, bytes]:
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
        answer, intent_name = _handle_text(
            transcript, speaker_user_id, source=speaker_user_id or "grpc-voice",
            channel_type=channel_type or "grpc_voice", channel_id=channel_id, audio_array=audio_array,
        )

        # TTS → PCM → OGG/Opus via ffmpeg
        audio_ogg_out = b""
        if tts.enabled and answer:
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

    def _resample_to_16k(pcm: bytes, rate: int) -> tuple[bytes, int]:
        if rate == 16000:
            return pcm, rate
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        new_len = int(len(samples) * 16000 / rate)
        resampled = np.interp(
            np.linspace(0, len(samples) - 1, new_len),
            np.arange(len(samples)),
            samples,
        ).astype(np.int16)
        return resampled.tobytes(), 16000

    def _handle_satellite_audio(device: str, pcm_bytes: bytes, speaker_user_id: str = "") -> tuple[str, str, str, bytes, int]:
        """
        Verarbeitet eine vollständige Satellit-Aufnahme via Go-Proxy:
        Raw PCM → STT → NLU → TTS → (transcript, answer, intent_name, tts_pcm, sample_rate)

        speaker_user_id: vom Proxy per Voice-ID identifizierter Sprecher (leer = anonym).
        """
        _touch_activity(device)
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
                 + (f" (Sprecher: {speaker_user_id})" if speaker_user_id else ""))

        # Offene Trigger-Frage? Dann Äußerung als Antwort konsumieren statt NLU-Routing.
        room = _get_satellite_room(device) or ""
        if room and _try_answer_pending(device, room, transcript):
            return transcript, "", "AnswerPending", b"", 0

        answer, intent_name = _handle_text(
            transcript, speaker_user_id=speaker_user_id, source=device, device=device,
            channel_type="satellite", channel_id=device, audio_array=audio_array,
        )

        tts_pcm = b""
        sample_rate = 0
        if tts.enabled and answer:
            result = tts.synthesize(answer)
            if result:
                tts_pcm, sample_rate = result
                tts_pcm, sample_rate = _resample_to_16k(tts_pcm, sample_rate)
                log.info(f"[{device}] TTS: {len(tts_pcm)} Bytes @ {sample_rate} Hz")
            else:
                log.warning(f"[{device}] TTS: synthesize() lieferte kein Ergebnis für Antwort: {answer!r}")
        elif not answer:
            log.debug(f"[{device}] Keine Antwort (z.B. Ignored) — kein TTS")
        else:
            log.debug(f"[{device}] TTS deaktiviert — keine Audio-Antwort")

        if intent_name == "Smalltalk" and tts_pcm:
            _maybe_reopen_smalltalk_mic(device)

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

    # ── ioBroker-Adapter gRPC-Callbacks ──────────────────────────────────────

    def _on_agent_state(state_id: str, value: str, *_):
        _on_state_update(state_id, value)
        # Route to handlers that expect slash-notation topics and plain string values
        topic = state_id.replace(".", "/")
        raw = _json_to_raw(value)
        for _ct in car_manager:
            if topic.startswith(_ct.topic_prefix):
                _ct.update(topic, raw)

    def _on_weather_update(update: pb.AgentWeatherUpdate) -> None:
        weather.apply_update(update)

    _RESIDENT_TYPE_CLASSES = {
        pb.ResidentType.ROOMIE: Roomie,
        pb.ResidentType.GUEST: Guest,
        pb.ResidentType.PET: Pet,
    }
    _RESIDENT_TYPE_BY_SEGMENT = {
        "roomie": pb.ResidentType.ROOMIE,
        "guest": pb.ResidentType.GUEST,
        "pet": pb.ResidentType.PET,
    }

    def _on_agent_resident(roomie_id: str, display_name: str | None, presence_state: int | None, resident_type: pb.ResidentType, mood_level: int | None = None):
        # Residents-Update via gRPC. residents ist per late-binding sichtbar
        # (wird kurz nach grpc_servicer gesetzt). display_name/presence_state/mood_level
        # sind None, wenn das AgentResident-Update das jeweilige Feld nicht gesetzt hatte
        # (proto3 optional) — Resident.update() lässt in dem Fall den bisherigen Wert stehen (#206).
        cls = _RESIDENT_TYPE_CLASSES.get(resident_type)
        if cls is None:
            log.warning(f"Unbekannter/unspezifizierter resident_type '{resident_type}' für {roomie_id}")
            return
        residents.get_or_create(roomie_id, cls).update(display_name, presence_state, mood_level)

    def _on_agent_send_residents(residents_list):
        # Initial-Snapshot beim Adapter-Connect: alle Residents in einem Schwung
        # nachziehen, statt auf das nächste Einzel-Update pro Resident zu warten.
        for r in residents_list:
            _on_agent_resident(
                r.roomie_id,
                r.name if r.HasField("name") else None,
                r.presence_state if r.HasField("presence_state") else None,
                r.type,
                r.mood_level if r.HasField("mood_level") else None,
            )

    def _on_agent_text_command(text: str) -> tuple[str, str]:
        return _handle_text(text, source="iobroker", channel_type="iobroker")

    def _on_agent_set_resident(resident_id: str, presence_state: int, resident_type: pb.ResidentType):
        residents.set_presence(resident_id, presence_state, resident_type)

    def _on_agent_satellite_control(room: str, key: str, value: object, device_id: str = ""):
        all_devices = {**udp_server.registered_devices(), **grpc_servicer.proxy_satellites()}
        if device_id:
            targets = [device_id] if device_id in all_devices else []
        else:
            targets = _resolve_targets(room, label="[satellite_control] ")
        if key == "mute":
            for d in targets:
                _device_mute[d] = bool(value)
                mqtt_handler.publish_mute_set(d, bool(value))
        elif key == "dnd":
            for d in targets:
                _device_dnd[d] = bool(value)
                mqtt_handler.publish_dnd_state(d, bool(value))
                grpc_servicer.agent_satellite_update(d, all_devices.get(d, ""), "", True, dnd=bool(value))
        elif key == "volume":
            for d in targets:
                _device_volume[d] = int(value)
                mqtt_handler.publish_volume_set(d, int(value))
        elif key in ("announcement", "announcement_ssml", "announcement_rephrase"):
            if tts.enabled:
                text = str(value)
                if key == "announcement_rephrase":
                    text = _rephrase_text(text)
                result = (tts.synthesize_ssml(text) if key == "announcement_ssml"
                          else tts.synthesize(text))
                if result:
                    pcm, rate = _resample_to_16k(*result)
                    for d in targets:
                        _send_audio(d, pcm, rate, label="[satellite_control] ")
        via = f"device_id={device_id!r}" if device_id else f"room={room!r}"
        log.info(f"[satellite_control] {via} {key}={value!r} → {len(targets)} Satelliten")

    _iobroker_ready: bool = False

    def _on_alarm_fire(record: dict):
        """AlarmManager.on_fire — TTS-Ansage beim Auslösen. Der eigentliche Klingel-Loop
        (Weckton + alternierende Lautstärke) läuft separat in AlarmManager selbst (#4)."""
        label = record.get("label")
        text = f"Wecker! {label}." if label else "Wecker! Guten Morgen!"
        _handle_feedback(record["satellite_id"], True, text)

    def _on_timer_fired(timer_id: str, label: str, metadata: dict):
        trigger_id = metadata.get("trigger_id")
        if trigger_id:
            log.info(f"[timer] Delay-Timer gefeuert für Trigger '{trigger_id}'")
            trigger_engine.fire_delayed(trigger_id)
            return

        room = metadata.get("room", "all")
        targets = [d for d in _resolve_targets(room) if not _device_dnd.get(d)]

        # TTS vorab synthetisieren damit Jingle + TTS nahtlos aufeinanderfolgen
        tts_pcm: Optional[tuple] = None
        if tts.enabled:
            result = tts.synthesize(f"Dein Timer ist abgelaufen: {label}.")
            if result:
                tts_pcm = _resample_to_16k(*result)

        for device in targets:
            mqtt_handler.reset_playback_done(device)
        for device in targets:
            mqtt_handler.publish_play_asset(device, "timer_jingle")
        for device in targets:
            if not mqtt_handler.wait_for_playback_done(device, timeout=3.0):
                log.warning(f"[timer] Kein playback_done von '{device}' erhalten (alte Firmware?) — Fallback-Sleep")
                time.sleep(1.1)

        if tts_pcm:
            pcm, rate = tts_pcm
            for device in targets:
                _send_audio(device, pcm, rate)

    def _on_timer_list(timers: list) -> None:
        trigger_engine.reconcile_timers(timers)

    def _on_timer_connected():
        if _iobroker_ready:
            grpc_servicer.timer_send_ready()
            grpc_servicer.timer_list_request()

    def _on_set_capture(device_id: str, enabled: bool, sample_type: str = "noise"):
        _device_dnd[device_id] = enabled
        mqtt_handler.publish_sampling_mode(device_id, enabled, sample_type)
        log.info(f"[capture] Satellit '{device_id}' Capture-Modus: {'an' if enabled else 'aus'} type={sample_type} (DND={'an' if enabled else 'aus'})")

    def _on_trigger_plink(device_id: str, record_duration: float):
        import time
        from hannah.plink import get_plink_pcm
        _touch_activity(device_id)
        plink_wav = cfg.get("plink_wav_path", "")
        pcm, plink_duration = get_plink_pcm(plink_wav)
        mqtt_handler.reset_playback_done(device_id)
        _send_audio(device_id, pcm, 16000, label="[plink] ")
        if not mqtt_handler.wait_for_playback_done(device_id, timeout=plink_duration + 1.0):
            log.warning(f"[plink] Kein playback_done von '{device_id}' erhalten (alte Firmware?) — Fallback-Sleep")
            time.sleep(plink_duration + 0.1)
        mqtt_handler.publish_virtual_ptt(device_id, True)
        log.info(f"[plink] Virtual PTT AN für {record_duration}s → {device_id}")
        time.sleep(record_duration)
        mqtt_handler.publish_virtual_ptt(device_id, False)
        log.info(f"[plink] Virtual PTT AUS → {device_id}")
        _touch_activity(device_id)

    def _on_agent_device_snapshot(devices):
        nonlocal _iobroker_ready
        iobroker.handle_device_snapshot(devices)
        # Trigger-State-Cache still nachziehen (kein Feuern) — sonst kennt die
        # TriggerEngine nach einem Core-Neustart also/unless-States erst nach der
        # nächsten echten Änderung (#141).
        trigger_engine.seed_from_snapshot({d.state_id: d.value.value for d in devices})
        # Kein sync_rooms() hier: iobroker.rooms enthält nur Räume mit mindestens einem
        # aktuell passenden virtualDevice-State — Räume ohne virtualDevice (z.B. nur ein
        # Satellit drin) würden fälschlich als verschwunden behandelt und gelöscht.
        # Raum-Lebenszyklus läuft ausschließlich über _on_agent_room_snapshot() (siehe
        # dort), das den vollständigen, geräteunabhängigen enum.rooms.*-Katalog bekommt (#134).
        db_group_rooms = {g["group_id"]: g["display_name"] for g in room_manager.get_groups()}
        nlu._rooms = {**iobroker.rooms, **db_group_rooms}
        nlu._devices = iobroker.devices
        _iobroker_ready = True
        grpc_servicer.timer_send_ready()
        grpc_servicer.timer_list_request()

    def _on_agent_room_snapshot(rooms):
        # Full enum.rooms.* catalog, independent of devices — keeps RoomManager
        # aware of rooms that don't have any device (and thus no AgentDeviceSnapshot
        # entry) yet, e.g. right before provisioning the first satellite into them.
        orphaned = room_manager.sync_rooms({r.room_id: dict(r.display_names).get("de") or r.room_id for r in rooms})
        for device_id, room_id in orphaned:
            grpc_servicer.agent_satellite_deleted(device_id, room_id)

    def _on_agent_connect():
        state_ids = trigger_engine.get_referenced_state_ids()
        if state_ids:
            log.info(f"[grpc] ioBroker-Adapter connected — WatchMore: {len(state_ids)} trigger states")
            grpc_servicer.agent_watch_more(list(state_ids))
        for tag, room, satellite, rssi in ble_engine.get_current_locations():
            grpc_servicer.agent_ble_update(tag.label, tag.mac, room or "", satellite or "", rssi)
        # Presence-Updates, die zwischen Core-Start und Adapter-Connect entstanden sind
        # (z.B. eine BLE-Sichtung), wurden beim Push Richtung ioBroker nie zugestellt —
        # hier einmalig nachziehen. Nur "anwesend", nie "weg" (siehe dump_present_users).
        _user_manager.dump_present_users()

    def _on_agent_ask_resident(correlation_id: str, room: str, question: str) -> None:
        log.info(f"[grpc/ask] corr={correlation_id!r} room={room!r} question={question!r}")
        def _answer_callback(answer: str) -> None:
            log.info(f"[grpc/ask] Antwort corr={correlation_id!r}: {answer!r}")
            grpc_servicer.agent_resident_answered(correlation_id, answer)
        _ask_fn(room, question, _answer_callback)

    # gRPC-Servicer wird hier erstellt damit _on_arrival/_on_departure Events pushen können.
    # get_satellites und get_car_state sind Lambdas (late binding) — udp_server ist
    # zum Zeitpunkt des Aufrufs bereits gesetzt.
    grpc_servicer = HannahServicer(
        user_manager=_user_manager,
        satellite_manager=satellite_manager,
        handle_text=_handle_text,
        handle_voice=_handle_voice,
        announce=process_announcement,
        notificate=lambda text, severity: process_notification(text, severity),
        get_satellites=lambda: {
            **udp_server.registered_devices_full(),
            **{dev: {"room": info["room"], "addr": info["addr"]} for dev, info in grpc_servicer.proxy_satellites_full().items()},
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
        on_agent_state=_on_agent_state,
        on_agent_resident=_on_agent_resident,
        on_agent_text_command=_on_agent_text_command,
        on_agent_connect=_on_agent_connect,
        on_agent_set_resident=_on_agent_set_resident,
        on_agent_satellite_control=_on_agent_satellite_control,
        on_agent_device_snapshot=_on_agent_device_snapshot,
        on_agent_send_residents=_on_agent_send_residents,
        on_agent_room_snapshot=_on_agent_room_snapshot,
        on_weather_update=_on_weather_update,
        on_trigger_firmware_update=lambda device: mqtt_handler.publish_ota_ok(device),
        on_trigger_satellite_restart=lambda device: (
            mqtt_handler.publish_restart(device),
            satellite_manager.mark_restarted(device),
        ),
        on_timer_fired=_on_timer_fired,
        on_timer_list=_on_timer_list,
        on_timer_connected=_on_timer_connected,
        on_set_capture=_on_set_capture,
        on_trigger_plink=_on_trigger_plink,
        on_agent_ask_resident=_on_agent_ask_resident,
        provision_satellite=satellite_manager.provision_satellite,
        pair_satellite=satellite_manager.pair_satellite,
        resolve_satellite_room=satellite_manager.get_satellite_room,
        upsert_satellite=satellite_manager.upsert_satellite,
        get_rooms=room_manager.get_rooms,
        get_groups=room_manager.get_groups,
        create_group=room_manager.create_group,
        update_group=room_manager.update_group,
        delete_group=room_manager.delete_group,
        set_group_satellites=room_manager.set_group_satellites,
        get_db_satellites=satellite_manager.get_satellites,
        set_satellite_room=satellite_manager.set_satellite_room,
        set_satellite_display_name=satellite_manager.set_satellite_display_name,
        set_satellite_owner=satellite_manager.set_satellite_owner,
        get_trigger_records=trigger_engine.get_trigger_records,
        create_trigger=trigger_engine.create_trigger,
        update_trigger=trigger_engine.update_trigger,
        delete_trigger=trigger_engine.delete_trigger,
        get_alarm_records=alarm_manager.get_alarm_records,
        create_alarm=alarm_manager.create_alarm,
        update_alarm=alarm_manager.update_alarm,
        delete_alarm=alarm_manager.delete_alarm,
        get_categories=settings_manager.get_categories,
        get_settings_records=settings_manager.get_settings,
        update_setting_value=settings_manager.update_setting_value,
        get_ble_tag_records=ble_tag_manager.get_tag_records,
        create_ble_tag=ble_tag_manager.create_tag,
        update_ble_tag=ble_tag_manager.update_tag,
        delete_ble_tag=ble_tag_manager.delete_tag,
        get_car_records=car_registry.get_car_records,
        create_car=car_registry.create_car,
        update_car=car_registry.update_car,
        delete_car=car_registry.delete_car,
        # `residents` ist erst weiter unten definiert (ResidentsClient) — Lambda löst das
        # Forward-Reference-Problem (gleiches Muster wie get_satellites oben mit grpc_servicer).
        get_residents=lambda: residents.all_residents(),
        on_automation_register=lambda automation: [
            u.id for u in _user_manager.get_users_with_automation_enabled(automation)
        ],
        list_activity=activity_log_manager.list_activity,
        stream_activity_audio=activity_log_manager.get_audio_chunks,
    )

    iobroker.set_setter(grpc_servicer.agent_set_state)

    # ------------------------------------------------------------------
    # Residents + Callbacks (referenzieren grpc_servicer für Event-Push)
    # Presence-Updates kommen via gRPC (_on_agent_resident); MQTT wird nur noch
    # für das Zurückschreiben von Presence-States in den Residents-Adapter genutzt.

    residents = ResidentsClient(cfg.get("residents", {}), _user_manager)
    residents.set_setter(grpc_servicer.agent_set_resident)
    residents.set_mood_setter(grpc_servicer.agent_set_resident_mood)

    # Rückrichtung: wenn Hannah (nicht ioBroker) als erstes von einer Presence-Änderung
    # eines verlinkten Users erfährt, an den Residents-Adapter zurückpushen.
    def _push_user_presence(roomie_id: str, is_home: bool, resident_type: str):
        if resident_type == "guest":
            (residents.set_guest_home if is_home else residents.set_guest_away)(roomie_id)
        else:
            (residents.set_user_home if is_home else residents.set_user_away)(roomie_id)

    _user_manager.set_residents_pusher(_push_user_presence)

    def _push_user_mood(roomie_id: str, mood: int, resident_type: str):
        residents.set_mood(roomie_id, mood, _RESIDENT_TYPE_BY_SEGMENT.get(resident_type, pb.ResidentType.ROOMIE))

    _user_manager.set_mood_pusher(_push_user_mood)

    def _on_resident_arrival(resident: Resident):
        if isinstance(resident, Roomie):
            process_announcement("all", "Willkommen zuhause!")
            user = _user_manager.get_user_by_linked_account("residents", resident.id)
            if user:
                user.presence = True
            display = user.display_name if user else resident.roomie_id
            grpc_servicer.publish_event(make_resident_event(resident.roomie_id, display, "arrived"))
        elif isinstance(resident, Guest):
            process_announcement("all", "Es ist Besuch angekommen!")
            grpc_servicer.publish_event(make_resident_event(f"guest:{resident.roomie_id}", resident.roomie_id, "arrived"))

    def _on_firmware_version(device: str, version: str, restart_reason: str = "", restart_count: int = 0):
        log.info(f"Firmware-Version: {device} = {version}")
        satellite_manager.set_satellite_firmware(device, version)
        if restart_count:
            # restart_count == 0 heißt: ältere Firmware ohne #165, keine Neustart-
            # Historie verfügbar — nichts zu speichern.
            satellite_manager.record_restart_report(device, restart_reason, restart_count)
        grpc_servicer.publish_event(make_firmware_event(device, version))
        grpc_servicer.agent_firmware_event(device, version)

    _ota_pending: set[str] = set()

    def _release_ota_updates():
        for device in list(_ota_pending):
            mqtt_handler.publish_ota_ok(device)
            _ota_pending.discard(device)

    def _on_ota_pending(device: str, version: str):
        log.info(f"OTA-Pending: {device} meldet Version {version}.")
        # version here is the available/target version, not the satellite's
        # current one — AgentFirmwareEvent.version must always report the
        # current version, so send the last known one instead of clobbering
        # it with the pending target (issue found via a resulting ioBroker
        # firmware_version showing the not-yet-installed target version).
        satellite_manager.set_satellite_update_available(device, version)
        current_sat = satellite_manager.get_satellite(device)
        current_version = (current_sat.firmware_version if current_sat else "") or version
        grpc_servicer.agent_firmware_event(device, current_version, update_available=True)
        grpc_servicer.publish_event(
            make_firmware_event(device, current_version, update_available=True, new_version=version)
        )
        if not residents.is_home():
            mqtt_handler.publish_ota_ok(device)
        else:
            log.info(f"OTA-Pending: jemand zuhause — {device} wartet auf Freigabe.")
            _ota_pending.add(device)

    def _on_resident_departure(resident: Resident):
        if isinstance(resident, Roomie):
            user = _user_manager.get_user_by_linked_account("residents", resident.id)
            if user:
                user.presence = False
            display = user.display_name if user else resident.roomie_id
            grpc_servicer.publish_event(make_resident_event(resident.roomie_id, display, "departed"))
            if not residents.is_home() and _ota_pending:
                log.info("Alle weg — OTA-Updates freigeben.")
                _release_ota_updates()
        elif isinstance(resident, Guest):
            grpc_servicer.publish_event(make_resident_event(f"guest:{resident.roomie_id}", resident.roomie_id, "departed"))

    def _on_resident_mood_changed(resident: Resident, _old_mood: int, mood: int):
        """Pull-Richtung: ioBroker meldet eine Stimmungsänderung -> auf den verlinkten Hannah-User übertragen.
        Push-Richtung (Hannah-User -> ioBroker) fehlt noch, AgentSetResident hat aktuell kein mood-Feld."""
        user = _user_manager.get_user_by_linked_account("residents", resident.id)
        if user:
            user.mood = mood

    def _on_sensor(device: str, temperature: float, pressure: float,
                   humidity: float, iaq: float, iaq_accuracy: int,
                   co2_equiv: float, voc_equiv: float):
        grpc_servicer.agent_sensor_update(
            device, temperature, pressure, humidity,
            iaq, iaq_accuracy, co2_equiv, voc_equiv,
        )

    mqtt_handler.set_ota_pending_handler(_on_ota_pending)
    mqtt_handler.set_firmware_handler(_on_firmware_version)
    mqtt_handler.set_sensor_handler(_on_sensor)
    residents.on_arrival(_on_resident_arrival)
    residents.on_departure(_on_resident_departure)
    residents.on_mood_changed(_on_resident_mood_changed)

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

        severity="direct": LLM wird übersprungen, Text wird unverändert weitergeleitet.
        """
        # Direct-Modus: kein LLM, aber TTS + Telegram wie normale Notification
        if severity == "direct":
            log.info(f"Direct-Notification: {raw_text!r}")
            if tts.enabled:
                result = tts.synthesize(raw_text)
                if result:
                    pcm, rate = _resample_to_16k(*result)
                    for target in _resolve_targets("all"):
                        if not _device_dnd.get(target):
                            _send_audio(target, pcm, rate)
            grpc_servicer.publish_event(make_system_notification_event(raw_text))
            return

        # LLM-Reformulierung (optional — wenn kein LLM verfügbar: rohen Text nutzen)
        text = raw_text
        if llm is not None:
            try:
                _tone = {
                    "alert":  "Drücke dich dabei klar und direkt aus, zeig dass es wichtig ist.",
                    "notify": "Drücke dich locker und direkt aus, wie eine Mitbewohnerin die kurz Bescheid gibt.",
                    "info":   "Drücke dich beiläufig aus, als würdest du es nebenbei erwähnen.",
                }.get(severity, "Drücke dich locker und direkt aus.")
                notification_prompt = (
                    "Du bist Hannah, eine 24-jährige Mitbewohnerin. "
                    "Formuliere die folgende Systemmeldung kurz und natürlich um — "
                    "du redest mit deiner Mitbewohnerin, nicht mit einem Kunden. "
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
                pcm, rate = _resample_to_16k(*result)
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
        if grpc_servicer.is_captured(device):
            grpc_servicer.push_capture_audio(
                device, raw_pcm, audio_cfg.get("sample_rate", 16000), end_of_utterance=True,
            )
            log.debug(f"[{device}] Capture-Audio (UDP) weitergeleitet ({len(raw_pcm)} Bytes)")
            return

        try:
            audio_array = audio_mod.from_raw_pcm(raw_pcm, audio_cfg)
        except Exception as e:
            log.error(f"[{device}] UDP Audio-Konvertierung fehlgeschlagen: {e}")
            return

        if len(audio_array) == 0:
            return

        pipeline(
            device,
            audio_array,
            publish_error   = lambda m: log.error(f"[{device}] {m}"),
            publish_answer  = lambda a: _handle_udp_answer(device, a),
        )

    def _handle_udp_answer(device: str, answer: str):
        if tts.enabled:
            result = tts.synthesize(answer)
            if result:
                pcm, rate = result
                udp_server.send_tts(device, pcm, sample_rate=rate)

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
                udp_server.send_tts(satellite_device, pcm, sample_rate=rate)
        elif text and tts.enabled:
            # Smalltalk oder Fehler: Text sprechen
            result = tts.synthesize(text)
            if result:
                pcm, rate = result
                udp_server.send_tts(satellite_device, pcm, sample_rate=rate)

    feedback_timeout = cfg.get("iobroker", {}).get("feedback_timeout", 3.0)
    iobroker.set_feedback_handler(_handle_feedback, timeout=feedback_timeout)

    tts.warm_cache(cfg.get("tts", {}).get("warm_phrases", []))

    udp_server = UDPServer(
        cfg.get("udp", {}),
        process_audio_udp,
        on_satellite_change=_on_satellite_change,
        resolve_satellite_room=satellite_manager.get_satellite_room,
        upsert_satellite=satellite_manager.upsert_satellite,
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
    log.info(f"Residents: Hannah online ({residents.hannah_name})")

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
