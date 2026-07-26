"""
Hannah gRPC Server

Exposes HannahService to external services (Telegram bot, web UI, …).
Runs in its own thread pool alongside the main event loop.
"""
import json
import logging
import queue
import sqlite3
import threading
import time
from concurrent import futures
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

import grpc
from werkzeug.security import generate_password_hash

from hannah.satellite_manager import SatelliteManager, SatellitePermissionError
from hannah.user_manager import UserManager
from hannah_proto import hannah_pb2 as pb
from hannah_proto import hannah_pb2_grpc as pb_grpc
from hannah.models.user import User
from hannah.models.satellite import Satellite
from hannah.grpc_interceptors import ProtocolVersionInterceptor, read_proto_version

log = logging.getLogger(__name__)

_KNOWN_PROVIDERS = {"residents", "telegram", "microsoft"}

# Proxy retries RegisterProxy every 5s (proxy/internal/hannah/client.go) — grace
# period must exceed that so a quick reconnect doesn't flip UDP/discovery back
# and forth (#140).
_PROXY_REVERT_GRACE_SECONDS = 10

# ------------------------------------------------------------------
# Event subscriber (one per connected SubscribeEvents call)

class _Subscriber:
    def __init__(self, event_types: list[str]):
        # Empty list = subscribe to all
        self._filter: Optional[set[str]] = set(event_types) if event_types else None
        self._queue: queue.Queue = queue.Queue()

    def put(self, event: pb.HannahEvent):
        if self._filter is None or event.event_type in self._filter:
            self._queue.put(event)

    def close(self):
        self._queue.put(None)  # sentinel — ends the generator

    def get(self, timeout: float = 1.0) -> Optional[pb.HannahEvent]:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return queue.Empty  # sentinel-like, caller checks


# ------------------------------------------------------------------
# Automation subscriber (one per connected AutomationConnect call)

class _AutomationSub:
    def __init__(self):
        # Set once the register message arrives; unset connections receive nothing.
        self.automation: Optional[str] = None
        self._queue: queue.Queue = queue.Queue()

    def put(self, command: pb.AutomationCommand):
        self._queue.put(command)

    def close(self):
        self._queue.put(None)  # sentinel — ends the generator

    def get(self, timeout: float = 1.0) -> Optional[pb.AutomationCommand]:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return queue.Empty  # sentinel-like, caller checks


# ------------------------------------------------------------------
# Servicer

class HannahServicer(pb_grpc.HannahServiceServicer):
    """
    gRPC service implementation.

    All heavy work is delegated to callbacks passed in from main.py so this
    class stays free of business logic and is easy to test in isolation.
    """

    def __init__(
        self,
        user_manager: UserManager,
        satellite_manager: SatelliteManager,
        handle_text: Callable[[str], tuple[str, str]],
        handle_voice: Callable[[bytes], tuple[str, str, str, bytes]],
        announce: Callable[..., None],  # (device, text, *, room_id="", user_id=0) — siehe AnnounceRequest #31
        notificate: Callable[[str, str], None],
        get_satellites: Callable[[], dict],
        get_car_state: Callable[[], Optional[object]],      # → CarState | None (erster Tracker)
        get_all_cars: Optional[Callable[[], list]] = None,  # → [(CarState, home_address)]
        handle_satellite_audio: Optional[Callable] = None,  # (device, room, pcm) → (transcript, answer, intent, pcm, rate)
        disable_udp: Optional[Callable[[], None]] = None,
        enable_udp: Optional[Callable[[], None]] = None,
        on_proxy_discovery: Optional[Callable[[str, int], None]] = None,  # (host, port) — None args = restore own address
        get_devices: Optional[Callable[[], list]] = None,           # → [{key,name,devices:[...]}]
        control_device: Optional[Callable[[str, str, str], bool]] = None,  # (device_id, state, value) → bool
        enroll_voiceprint: Optional[Callable[[str, bytes, int], tuple]] = None,  # (user_id, pcm, rate) → (ok, msg)
        on_satellite_change: Optional[Callable[[dict], None]] = None,           # ({device: room}) bei Register/Disconnect via Proxy
        on_agent_state: Optional[Callable[[str, str, bool, int], None]] = None,      # (state_id, value, ack, ts)
        on_agent_resident: Optional[Callable[[str, str, int, pb.ResidentType, int], None]] = None,   # (roomie_id, name, presence_state, type, mood_level)
        on_agent_text_command: Optional[Callable[[str], tuple[str, str]]] = None,    # (text) → (answer, intent)
        on_agent_connect: Optional[Callable[[], None]] = None,                       # called on each new adapter connection
        on_agent_set_resident: Optional[Callable[[str, int, pb.ResidentType], None]] = None,    # (resident_id, presence_state, type)
        on_agent_satellite_control: Optional[Callable[[str, str, object], None]] = None,  # (room, key, value)
        on_agent_device_snapshot: Optional[Callable[[Iterable[pb.AgentDevice]], None]] = None,
        on_agent_send_residents: Optional[Callable[[Iterable[pb.AgentResident]], None]] = None,
        on_agent_room_snapshot: Optional[Callable[[Iterable[pb.AgentRoom]], None]] = None,
        on_trigger_firmware_update: Optional[Callable[[str], None]] = None,  # (device)
        on_trigger_satellite_restart: Optional[Callable[[str], None]] = None,  # (device)
        on_timer_fired: Optional[Callable[[str, str, dict], None]] = None,     # (timer_id, label, metadata)
        on_timer_list: Optional[Callable[[list], None]] = None,               # (list[TimerInfo])
        on_timer_connected: Optional[Callable[[], None]] = None,
        on_set_capture: Optional[Callable[[str, bool, str], None]] = None,     # (device_id, enabled, sample_type) — set DND + satellite MQTT
        on_trigger_plink: Optional[Callable[[str, float], None]] = None,       # (device_id, record_duration_s)
        on_agent_ask_resident: Optional[Callable[[str, str, str], None]] = None,  # (correlation_id, room, question)
        provision_satellite: Optional[Callable[[str, str, str], bool]] = None,   # (seed, display_name, room_id) → bool
        pair_satellite: Optional[Callable[[str, str], bool]] = None,             # (device_id, seed) → bool
        resolve_satellite_room: Optional[Callable[[str], Optional[str]]] = None,  # (device_id) → room_id | None
        upsert_satellite: Optional[Callable[[str], None]] = None,                # (device_id) → refresh last_seen
        get_rooms: Optional[Callable[[], list]] = None,                          # () → [{room_id, display_name}]
        get_groups: Optional[Callable[[], list]] = None,                         # () → [{group_id, display_name, rooms}]
        create_group: Optional[Callable[[str, str], bool]] = None,               # (group_id, display_name) → bool
        update_group: Optional[Callable[[str, str], bool]] = None,               # (group_id, display_name) → bool
        delete_group: Optional[Callable[[str], bool]] = None,                    # (group_id) → bool
        set_group_rooms: Optional[Callable[[str, list], None]] = None,           # (group_id, room_ids)
        get_db_satellites: Optional[Callable[[], list]] = None,                  # () → [{device_id, display_name, room_id, last_seen, room_display_name}]
        set_satellite_room: Optional[Callable[[str, Optional[str]], bool]] = None,         # (device_id, room_id) → bool
        set_satellite_display_name: Optional[Callable[[str, str], bool]] = None,          # (device_id, display_name) → bool
        set_satellite_owner: Optional[Callable[[str, Optional[int]], bool]] = None,        # (device_id, user_id) → bool, #31
        get_trigger_records: Optional[Callable[[], list]] = None,                # () → [{id, when, cancel_when, on_response, say, ask, rephrase, room, cooldown, delay}]
        create_trigger: Optional[Callable[..., bool]] = None,                    # (id, when, cancel_when, on_response, say, ask, rephrase, room, cooldown, delay) → bool
        update_trigger: Optional[Callable[..., bool]] = None,                    # gleiche Signatur wie create_trigger → bool
        delete_trigger: Optional[Callable[[str], bool]] = None,                  # (id) → bool
        get_alarm_records: Optional[Callable[[], list]] = None,                  # () → [{id, satellite_id, time, weekdays, skip_dates, one_shot_date, enabled, label, user_id}]
        create_alarm: Optional[Callable[..., dict]] = None,                      # (satellite_id, time, weekdays, one_shot_date, user_id, label) → dict
        update_alarm: Optional[Callable[..., bool]] = None,                      # (id, satellite_id, time, weekdays, skip_dates, one_shot_date, enabled, label) → bool
        delete_alarm: Optional[Callable[[int], bool]] = None,                    # (id) → bool
        get_categories: Optional[Callable[[], list]] = None,                     # () → [{id, name, parent}]
        get_settings_records: Optional[Callable[[], list]] = None,               # () → [{id, category, name, value}]
        update_setting_value: Optional[Callable[[int, object], bool]] = None,    # (setting_id, value) → bool
        get_ble_tag_records: Optional[Callable[[], list]] = None,                # () → [{id, mac_address, label, user_id}]
        create_ble_tag: Optional[Callable[..., Optional[dict]]] = None,          # (mac_address, label, user_id) → dict | None
        update_ble_tag: Optional[Callable[..., bool]] = None,                    # (id, mac_address, label, user_id) → bool
        delete_ble_tag: Optional[Callable[[int], bool]] = None,                  # (id) → bool
        get_car_records: Optional[Callable[[], list]] = None,                    # () → [{id, topic_prefix, home_address, owner_user_ids}]
        create_car: Optional[Callable[..., Optional[dict]]] = None,              # (topic_prefix, home_address, owner_user_ids) → dict | None
        update_car: Optional[Callable[..., bool]] = None,                        # (id, topic_prefix, home_address, owner_user_ids) → bool
        delete_car: Optional[Callable[[int], bool]] = None,                      # (id) → bool
        get_residents: Optional[Callable[[], list]] = None,                      # () → [Resident]
        on_automation_register: Optional[Callable[[str], list]] = None,          # (automation) → [user_id] currently enabled
    ):
        self._user_manager          = user_manager
        self._satellite_manager     = satellite_manager
        self._handle_text           = handle_text
        self._handle_voice          = handle_voice
        self._announce              = announce
        self._notificate            = notificate
        self._get_satellites        = get_satellites
        self._get_car_state         = get_car_state
        self._get_all_cars          = get_all_cars or (lambda: [])
        self._handle_satellite_audio = handle_satellite_audio
        self._disable_udp           = disable_udp or (lambda: None)
        self._enable_udp            = enable_udp or (lambda: None)
        self._on_proxy_discovery    = on_proxy_discovery or (lambda *_: None)
        self._get_devices           = get_devices or (lambda: [])
        self._control_device        = control_device or (lambda *_: False)
        self._enroll_voiceprint     = enroll_voiceprint
        self._on_satellite_change   = on_satellite_change
        self._on_agent_state        = on_agent_state
        self._on_agent_resident     = on_agent_resident
        self._on_agent_text_command = on_agent_text_command
        self._on_agent_connect           = on_agent_connect
        self._on_agent_set_resident      = on_agent_set_resident
        self._on_agent_satellite_control = on_agent_satellite_control
        self._on_agent_device_snapshot    = on_agent_device_snapshot
        self._on_agent_send_residents     = on_agent_send_residents
        self._on_agent_room_snapshot      = on_agent_room_snapshot
        self._on_trigger_firmware_update  = on_trigger_firmware_update or (lambda _: None)
        self._on_trigger_satellite_restart = on_trigger_satellite_restart or (lambda _: None)
        self._on_timer_fired    = on_timer_fired
        self._on_timer_list     = on_timer_list
        self._on_timer_connected = on_timer_connected
        self._on_set_capture          = on_set_capture or (lambda *_: None)
        self._on_trigger_plink        = on_trigger_plink or (lambda *_: None)
        self._on_agent_ask_resident   = on_agent_ask_resident
        self._provision_satellite     = provision_satellite or (lambda *_: False)
        self._pair_satellite          = pair_satellite or (lambda *_: False)
        self._resolve_satellite_room  = resolve_satellite_room or (lambda *_: None)
        self._upsert_satellite        = upsert_satellite or (lambda *_: None)
        self._get_rooms                = get_rooms or (lambda: [])
        self._get_groups               = get_groups or (lambda: [])
        self._create_group             = create_group or (lambda *_: False)
        self._update_group             = update_group or (lambda *_: False)
        self._delete_group             = delete_group or (lambda *_: False)
        self._set_group_rooms          = set_group_rooms or (lambda *_: None)
        self._get_db_satellites        = get_db_satellites or (lambda: [])
        self._set_satellite_room       = set_satellite_room or (lambda *_: False)
        self._set_satellite_display_name = set_satellite_display_name or (lambda *_: False)
        self._set_satellite_owner      = set_satellite_owner or (lambda *_: False)
        self._get_trigger_records       = get_trigger_records or (lambda: [])
        self._create_trigger            = create_trigger or (lambda *_: False)
        self._update_trigger            = update_trigger or (lambda *_: False)
        self._delete_trigger            = delete_trigger or (lambda *_: False)
        self._get_alarm_records         = get_alarm_records or (lambda: [])
        self._create_alarm              = create_alarm or (lambda *_a, **_k: None)
        self._update_alarm              = update_alarm or (lambda *_: False)
        self._delete_alarm              = delete_alarm or (lambda *_: False)
        self._get_categories            = get_categories or (lambda: [])
        self._get_settings_records      = get_settings_records or (lambda: [])
        self._update_setting_value      = update_setting_value or (lambda *_: False)
        self._get_ble_tag_records       = get_ble_tag_records or (lambda: [])
        self._create_ble_tag            = create_ble_tag or (lambda *_a, **_k: None)
        self._update_ble_tag            = update_ble_tag or (lambda *_: False)
        self._delete_ble_tag            = delete_ble_tag or (lambda *_: False)
        self._get_car_records           = get_car_records or (lambda: [])
        self._create_car                = create_car or (lambda *_a, **_k: None)
        self._update_car                = update_car or (lambda *_: False)
        self._delete_car                = delete_car or (lambda *_: False)
        self._get_residents             = get_residents or (lambda: [])
        self._on_automation_register    = on_automation_register or (lambda _automation: [])

        self._subscribers: list[_Subscriber] = []
        self._subs_lock = threading.Lock()


        # Satelliten die der Proxy gemeldet hat: {device_id: {"room": str, "addr": str}}
        self._proxy_satellites: dict[str, dict] = {}
        self._proxy_sat_lock = threading.Lock()

        # Per-connection command queues for active proxy streams
        self._proxy_queues: list[queue.Queue] = []
        self._proxy_lock = threading.Lock()
        self._proxy_revert_timer: Optional[threading.Timer] = None

        # Per-connection command queues for active agent streams
        self._agent_queues: list[queue.Queue] = []
        self._agent_lock = threading.Lock()

        # Single queue for the connected Timer Service (at most one at a time)
        self._timer_queue: Optional[queue.Queue] = None
        self._timer_lock = threading.Lock()
        # One-shot queues waiting on the next TimerListResponse — see GetTimers()/_request_timer_list()
        self._timer_list_waiters: list[queue.Queue] = []

        # Captured satellites: device_id → audio queue (SatelliteAudioChunk)
        self._captured_satellites: dict[str, queue.Queue] = {}
        self._capture_lock = threading.Lock()

        # Per-connection subs for active AutomationConnect streams
        self._automation_subs: list[_AutomationSub] = []
        self._automation_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public: proxy helpers (called from main.py)

    def has_proxy(self) -> bool:
        """True if at least one proxy stream is currently connected."""
        with self._proxy_lock:
            return len(self._proxy_queues) > 0

    def proxy_satellites(self) -> dict[str, str]:
        """Aktuell vom Proxy gemeldete Satelliten: {device: room}."""
        with self._proxy_sat_lock:
            return {dev: info["room"] for dev, info in self._proxy_satellites.items()}

    def proxy_satellites_full(self) -> dict[str, dict]:
        """Aktuell vom Proxy gemeldete Satelliten: {device: {"room": str, "addr": str}}."""
        with self._proxy_sat_lock:
            return dict(self._proxy_satellites)

    def push_audio_to_proxy(self, device_id: str, pcm: bytes, sample_rate: int):
        """Push a single PlayAudioCommand (full PCM, is_last=True) to all connected proxy streams."""
        cmd = pb.ProxyCommand(
            play_audio=pb.PlayAudioCommand(
                device_id=device_id,
                audio_pcm=pcm,
                sample_rate=sample_rate,
                is_last=True,
            )
        )
        with self._proxy_lock:
            for q in list(self._proxy_queues):
                q.put(cmd)

    def stream_audio_to_proxy(self, target: str, pcm: bytes, sample_rate: int,
                               chunk_size: int = 4800):
        """Slice PCM into chunks and push one PlayAudioCommand per chunk.

        target is the satellite device_id. Chunks arrive at the proxy in order;
        the proxy forwards each to the satellite immediately so playback can start
        before the full TTS response is sent.
        chunk_size=4800 ≈ 100ms @ 24kHz 16-bit mono.
        """
        if not pcm:
            return
        proxy_device_id = target
        with self._proxy_lock:
            queues = list(self._proxy_queues)
        if not queues:
            return
        offset = 0
        total = len(pcm)
        while offset < total:
            end = min(offset + chunk_size, total)
            chunk = pcm[offset:end]
            is_last = end >= total
            cmd = pb.ProxyCommand(
                play_audio=pb.PlayAudioCommand(
                    device_id=proxy_device_id,
                    audio_pcm=chunk,
                    sample_rate=sample_rate,
                    is_last=is_last,
                )
            )
            for q in queues:
                q.put(cmd)
            offset = end

    # ------------------------------------------------------------------
    # Public: push an event to all matching subscribers

    def publish_event(self, event: pb.HannahEvent):
        """Called by Hannah core when something notable happens."""
        with self._subs_lock:
            for sub in list(self._subscribers):
                sub.put(event)

    # ------------------------------------------------------------------
    # User Registry

    def GetUsers(self, request, _context):
        users = self._user_manager.users(include_inactive=request.include_inactive)
        return pb.GetUsersResponse(users=[_user_to_pb(u) for u in users])

    def GetUser(self, request, context):
        lookup = request.WhichOneof("lookup")
        match lookup:
            case "user_name":
                raw: User = self._user_manager.get_user_by_username(request.user_name)
            case "id":
                raw: User = self._user_manager.get_user_by_id(request.id)
            case "linked_account":
                la = request.linked_account
                raw: User = self._user_manager.get_user_by_linked_account(la.provider, la.external_id)
        if not raw:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details("User nicht gefunden.")
            return pb.UserResponse(found=False)

        return pb.UserResponse(found=True, user=_user_to_pb(raw))

    def LinkAccount(self, request, context):
        if request.service not in _KNOWN_PROVIDERS:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(f"Unbekannter Provider: {request.service}")
            return pb.StatusResponse(ok=False, message=f"Unbekannter Provider: {request.service}")

        user: User = self._user_manager.get_user_by_id(request.user_id)
        if not user:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details("User nicht gefunden.")
            return pb.StatusResponse(ok=False, message="User nicht gefunden.")

        existing = self._user_manager.get_user_by_linked_account(request.service, request.account_id)
        if existing and existing.id != request.user_id:
            context.set_code(grpc.StatusCode.ALREADY_EXISTS)
            context.set_details("Account bereits mit einem anderen User verknüpft.")
            return pb.StatusResponse(ok=False, message="Account bereits mit einem anderen User verknüpft.")

        user.link_account(request.service, request.account_id, provider_payload=request.provider_payload)
        return pb.StatusResponse(ok=True, message="verknüpft")

    def UnlinkAccount(self, request, context):
        user: User = self._user_manager.get_user_by_id(request.user_id)
        if not user:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details("User nicht gefunden.")
            return pb.StatusResponse(ok=False, message="User nicht gefunden.")

        if request.requestor_id:
            requestor = self._user_manager.get_user_by_id(request.requestor_id)
            requestor_trust = requestor.trust_level if requestor else 0
            if request.requestor_id != request.user_id and requestor_trust < 10:
                context.set_code(grpc.StatusCode.PERMISSION_DENIED)
                context.set_details("forbidden")
                return pb.StatusResponse(ok=False, message="forbidden")

        user.unlink_account(request.service)
        return pb.StatusResponse(ok=True, message="entfernt")

    def SetTrustLevel(self, request, context):
        user = self._user_manager.get_user_by_id(request.user_id)
        if not user:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details("User nicht gefunden.")
            return pb.StatusResponse(ok=False, message="User nicht gefunden.")
        
        user.trust_level = request.level
        user.save()
        msg = "aktualisiert" if user else f"User {request.user_id!r} nicht gefunden"
        return pb.StatusResponse(ok=(True if user else False), message=msg)

    def SetSystemMessages(self, request, _context):
        # user_id ist immer eindeutig (PRIMARY KEY), wird immer vom User aufgerufen, daher keine Unterscheidung zwischen get und get_or_404 nötig
        user: User = self._user_manager.get_user_by_id(request.user_id)
        user.system_messages = 1 if request.enabled else 0
        user.save()
        msg = "aktualisiert"
        return pb.StatusResponse(ok=True, message=msg)

    def SetAutomation(self, request, context):
        user: User = self._user_manager.get_user_by_id(request.user_id)
        if not user:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details("User nicht gefunden.")
            return pb.StatusResponse(ok=False, message="User nicht gefunden.")

        if request.enabled:
            user.enable_automation(request.automation)
        else:
            user.disable_automation(request.automation)

        self.push_automation_update(request.user_id, request.automation, request.enabled)
        return pb.StatusResponse(ok=True, message="aktualisiert")

    def Login(self, request, context):
        user: User = self._user_manager.login_user(request.username, request.password)
        if not user:
            context.set_code(grpc.StatusCode.UNAUTHENTICATED)
            context.set_details("Ungültige Zugangsdaten.")
            return pb.UserResponse(found=False)
        return pb.UserResponse(found=True, user=_user_to_pb(user))

    # ------------------------------------------------------------------
    # User-Verwaltung (Admin-UI, #27 Phase 6)

    def CreateUser(self, request, _context):
        try:
            user: User = self._user_manager.create_user(
                request.username, generate_password_hash(request.password),
                email=request.email, display_name=request.display_name or None,
                type=request.type or "roomie",
            )
        except ValueError as e:
            return pb.CreateUserResponse(ok=False, message=str(e))
        except sqlite3.IntegrityError:
            return pb.CreateUserResponse(ok=False, message="username oder email existiert bereits")
        return pb.CreateUserResponse(ok=True, id=user.id, message="created")

    def UpdateUser(self, request, context):
        user: User = self._user_manager.get_user_by_id(request.user_id)
        if not user:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details("User nicht gefunden.")
            return pb.StatusResponse(ok=False, message="User nicht gefunden.")
        user.display_name = request.display_name.strip() or user.username
        user.email = request.email.strip() or user.email
        user.type = request.type or user.type
        user.is_active = 1 if request.is_active else 0
        if request.password:
            user.password_hash = generate_password_hash(request.password)
        user.save()
        return pb.StatusResponse(ok=True, message="updated")

    def DeleteUser(self, request, _context):
        ok = self._user_manager.delete_user(request.user_id)
        return pb.StatusResponse(ok=ok, message="deleted" if ok else "not found")

    def GetResidents(self, _request, _context):
        return pb.GetResidentsResponse(residents=[_resident_to_pb(r) for r in self._get_residents()])

    # ------------------------------------------------------------------
    # Control

    def _user_from_request(self, source_service: str, source_user_id: str) -> str:
        """Löst source_service + source_user_id via linked_accounts auf eine roomie_id auf."""
        if not source_service or not source_user_id:
            return ""
        user: User = self._user_manager.get_user_by_linked_account(source_service, source_user_id)

        if not user:
            return ""
        
        return user.id

    def SubmitText(self, request, _context):
        user_id = self._user_from_request(request.source_service, request.source_user_id)
        log.info(
            f"[grpc] SubmitText von {request.source_service}:{request.source_user_id}"
            f" (user={user_id or 'anonym'}) — {request.text!r}"
        )
        answer, intent_name = self._handle_text(request.text, user_id)
        return pb.SubmitTextResponse(answer=answer, intent_name=intent_name)

    def SubmitVoice(self, request, _context):
        user_id = self._user_from_request(request.source_service, request.source_user_id)
        log.info(
            f"[grpc] SubmitVoice von {request.source_service}:{request.source_user_id}"
            f" (user={user_id or 'anonym'}, {len(request.audio)} bytes)"
        )
        transcript, answer, intent_name, audio_ogg = self._handle_voice(request.audio, user_id)
        return pb.SubmitVoiceResponse(
            transcript=transcript,
            answer=answer,
            intent_name=intent_name,
            audio_ogg=audio_ogg,
        )

    def Announce(self, request, _context):
        try:
            self._announce(request.device, request.text, room_id=request.room_id, user_id=request.user_id)
            return pb.StatusResponse(ok=True, message="gesendet")
        except Exception as e:
            log.error(f"[grpc] Announce fehlgeschlagen: {e}")
            return pb.StatusResponse(ok=False, message=str(e))

    def Notify(self, request, _context):
        if not self._notificate:
            return pb.StatusResponse(ok=False, message="notification not configured")
        try:
            severity = "direct" if request.direct else (request.severity or "notify")
            log.info(f"[grpc] Notify empfangen: severity={severity!r} text={request.text!r}")
            threading.Thread(
                target=self._notificate, args=(request.text, severity), daemon=True
            ).start()
            return pb.StatusResponse(ok=True, message="queued")
        except Exception as e:
            log.error(f"[grpc] Notify fehlgeschlagen: {e}")
            return pb.StatusResponse(ok=False, message=str(e))

    def GetSatellites(self, _request, _context):
        connected = self._get_satellites()
        db_sats = {s["device_id"]: s for s in self._get_db_satellites()}
        for device_id in connected:
            if device_id not in db_sats:
                self._upsert_satellite(device_id)
        result = []
        seen = set()
        for device_id, sat in db_sats.items():
            seen.add(device_id)
            info = connected.get(device_id)
            room_id = sat.get("room_id") or ""
            kwargs = dict(
                device_id=device_id,
                room=info.get("room", "") if info else "",
                address=info.get("addr", "") if info else "",
                display_name=sat.get("display_name") or "",
                room_id=room_id,
                room_display_name=sat.get("room_display_name") or "",
                last_seen=sat.get("last_seen") or "",
                connected=info is not None,
                room_mismatch=info is not None and info.get("room") != room_id,
                owner_user_id=sat.get("owner_user_id") or 0,
                owner_display_name=sat.get("owner_display_name") or "",
                firmware_version=sat.get("firmware_version") or "",
                update_available=bool(sat.get("update_available")),
                smalltalk_followup_listen=bool(sat.get("smalltalk_followup_listen")),
            )
            if sat.get("new_version"):
                kwargs["new_version"] = sat["new_version"]
            result.append(pb.Satellite(**kwargs))
        for device_id, info in connected.items():
            if device_id not in seen:
                result.append(pb.Satellite(
                    device_id=device_id,
                    room=info.get("room", ""),
                    address=info.get("addr", ""),
                    connected=True,
                    room_mismatch=True,
                ))
        return pb.GetSatellitesResponse(satellites=result)

    def SetSatelliteRoom(self, request, _context):
        self._upsert_satellite(request.device_id)
        try:
            ok = self._set_satellite_room(request.device_id, request.room_id or None, requestor_id=request.requestor_id)
        except SatellitePermissionError:
            return pb.StatusResponse(ok=False, message="forbidden")
        if ok:
            connected = self._get_satellites().get(request.device_id)
            self.agent_satellite_update(
                request.device_id,
                request.room_id or "",
                connected.get("addr", "") if connected else "",
                connected is not None,
            )
        return pb.StatusResponse(ok=ok, message="set" if ok else "not found")

    def SetSatelliteDisplayName(self, request, _context):
        if not request.display_name:
            return pb.StatusResponse(ok=False, message="display_name required")
        self._upsert_satellite(request.device_id)
        try:
            ok = self._set_satellite_display_name(request.device_id, request.display_name, requestor_id=request.requestor_id)
        except SatellitePermissionError:
            return pb.StatusResponse(ok=False, message="forbidden")
        return pb.StatusResponse(ok=ok, message="set" if ok else "not found")

    def SetSatelliteOwner(self, request, _context):
        self._upsert_satellite(request.device_id)
        try:
            ok = self._set_satellite_owner(request.device_id, request.user_id or None, requestor_id=request.requestor_id)
        except SatellitePermissionError:
            return pb.StatusResponse(ok=False, message="forbidden")
        return pb.StatusResponse(ok=ok, message="set" if ok else "not found")

    def SetSatelliteSmalltalkFollowup(self, request, _context):
        self._upsert_satellite(request.device_id)
        try:
            ok = self._satellite_manager.set_satellite_smalltalk_followup(
                request.device_id, request.enabled, requestor_id=request.requestor_id,
            )
        except SatellitePermissionError:
            return pb.StatusResponse(ok=False, message="forbidden")
        return pb.StatusResponse(ok=ok, message="set" if ok else "not found")

    def DeleteSatellite(self, request, _context):
        sat: Optional[Satellite] = self._satellite_manager.get_satellite(device_id=request.device_id)
        try:
            ok = self._satellite_manager.delete_satellite(request.device_id, requestor_id=request.requestor_id) if sat else False
        except SatellitePermissionError:
            return pb.StatusResponse(ok=False, message="forbidden")
        if ok:
            self.agent_satellite_deleted(device_id=sat.device_id, room=sat.room_id or "")
        return pb.StatusResponse(ok=ok, message="deleted" if ok else "not found")

    # ------------------------------------------------------------------
    # Rooms/Groups (Admin-UI, #27 Phase 1)

    def GetRooms(self, _request, _context):
        rooms = self._get_rooms()
        return pb.GetRoomsResponse(
            rooms=[pb.Room(room_id=r["room_id"], display_name=r["display_name"]) for r in rooms]
        )

    def GetGroups(self, _request, _context):
        groups = self._get_groups()
        return pb.GetGroupsResponse(groups=[
            pb.Group(
                group_id=g["group_id"],
                display_name=g["display_name"],
                rooms=[pb.Room(room_id=r["room_id"], display_name=r["display_name"]) for r in g["rooms"]],
            )
            for g in groups
        ])

    def CreateGroup(self, request, _context):
        ok = self._create_group(request.group_id, request.display_name)
        return pb.StatusResponse(ok=ok, message="created" if ok else "group_id existiert bereits")

    def UpdateGroup(self, request, _context):
        ok = self._update_group(request.group_id, request.display_name)
        return pb.StatusResponse(ok=ok, message="updated" if ok else "not found")

    def DeleteGroup(self, request, _context):
        ok = self._delete_group(request.group_id)
        return pb.StatusResponse(ok=ok, message="deleted" if ok else "not found")

    def SetGroupRooms(self, request, _context):
        self._set_group_rooms(request.group_id, list(request.room_ids))
        return pb.StatusResponse(ok=True, message="set")

    # ------------------------------------------------------------------
    # Triggers (Admin-UI, #27 Phase 4; absorbierte vormals separate Routinen, #139)

    def GetTriggers(self, _request, _context):
        return pb.GetTriggersResponse(triggers=[_trigger_to_pb(t) for t in self._get_trigger_records()])

    def _parse_trigger_json(self, request):
        """Gibt (when, cancel_when, on_response, actions) zurück; wirft json.JSONDecodeError bei kaputtem Input."""
        when = json.loads(request.when_json) if request.when_json else {}
        cancel_when = json.loads(request.cancel_when_json) if request.cancel_when_json else None
        on_response = json.loads(request.on_response_json) if request.on_response_json else []
        actions = json.loads(request.actions_json) if request.actions_json else []
        return when, cancel_when, on_response, actions

    def CreateTrigger(self, request, _context):
        try:
            when, cancel_when, on_response, actions = self._parse_trigger_json(request)
        except json.JSONDecodeError as e:
            return pb.StatusResponse(ok=False, message=f"invalid JSON: {e}")
        room = request.room or "all"
        cooldown = request.cooldown if request.cooldown > 0 else 3600
        ok = self._create_trigger(request.id, when, cancel_when, on_response, actions, request.say, request.ask,
                                   request.rephrase, room, cooldown, request.delay)
        return pb.StatusResponse(ok=ok, message="created" if ok else "id existiert bereits")

    def UpdateTrigger(self, request, _context):
        try:
            when, cancel_when, on_response, actions = self._parse_trigger_json(request)
        except json.JSONDecodeError as e:
            return pb.StatusResponse(ok=False, message=f"invalid JSON: {e}")
        room = request.room or "all"
        cooldown = request.cooldown if request.cooldown > 0 else 3600
        ok = self._update_trigger(request.id, when, cancel_when, on_response, actions, request.say, request.ask,
                                   request.rephrase, room, cooldown, request.delay)
        return pb.StatusResponse(ok=ok, message="updated" if ok else "not found")

    def DeleteTrigger(self, request, _context):
        ok = self._delete_trigger(request.id)
        return pb.StatusResponse(ok=ok, message="deleted" if ok else "not found")

    # ------------------------------------------------------------------
    # Alarms (Wecker, #4)

    def GetAlarms(self, _request, _context):
        return pb.GetAlarmsResponse(alarms=[_alarm_to_pb(a) for a in self._get_alarm_records()])

    def CreateAlarm(self, request, _context):
        record = self._create_alarm(request.satellite_id, request.time, list(request.weekdays) or None,
                                     request.one_shot_date or None, request.user_id, request.label)
        if record is None:
            return pb.CreateAlarmResponse(ok=False, message="invalid alarm")
        return pb.CreateAlarmResponse(ok=True, id=record["id"], message="created")

    def UpdateAlarm(self, request, _context):
        ok = self._update_alarm(request.id, request.satellite_id, request.time, list(request.weekdays) or None,
                                 list(request.skip_dates), request.one_shot_date or None, request.enabled,
                                 request.label)
        return pb.StatusResponse(ok=ok, message="updated" if ok else "not found")

    def DeleteAlarm(self, request, _context):
        ok = self._delete_alarm(request.id)
        return pb.StatusResponse(ok=ok, message="deleted" if ok else "not found")

    # ------------------------------------------------------------------
    # Settings (Admin-UI, #27 Phase 5)

    def GetSettings(self, _request, _context):
        return pb.GetSettingsResponse(
            categories=[_category_to_pb(c) for c in self._get_categories()],
            settings=[_setting_to_pb(s) for s in self._get_settings_records()],
        )

    def UpdateConfig(self, request, _context):
        for u in request.updates:
            try:
                value = json.loads(u.value)
            except json.JSONDecodeError as e:
                return pb.StatusResponse(ok=False, message=f"invalid JSON for setting {u.setting_id}: {e}")
            if not self._update_setting_value(u.setting_id, value):
                return pb.StatusResponse(ok=False, message=f"setting {u.setting_id} not found")
        return pb.StatusResponse(ok=True, message="updated")

    # ------------------------------------------------------------------
    # BLE Tags (Admin-UI, #115)

    def GetBleTags(self, _request, _context):
        return pb.GetBleTagsResponse(tags=[_ble_tag_to_pb(t) for t in self._get_ble_tag_records()])

    def CreateBleTag(self, request, _context):
        result = self._create_ble_tag(request.mac_address, request.label, request.user_id or None)
        if result is None:
            return pb.CreateBleTagResponse(ok=False, message="mac_address existiert bereits")
        return pb.CreateBleTagResponse(ok=True, id=result["id"], message="created")

    def UpdateBleTag(self, request, _context):
        ok = self._update_ble_tag(request.id, request.mac_address, request.label, request.user_id or None)
        return pb.StatusResponse(ok=ok, message="updated" if ok else "not found")

    def DeleteBleTag(self, request, _context):
        ok = self._delete_ble_tag(request.id)
        return pb.StatusResponse(ok=ok, message="deleted" if ok else "not found")

    # ------------------------------------------------------------------
    # Cars (Admin-UI, #115)

    def GetCars(self, _request, _context):
        return pb.GetCarsResponse(cars=[_car_config_to_pb(c) for c in self._get_car_records()])

    def CreateCar(self, request, _context):
        result = self._create_car(request.name, request.topic_prefix, request.home_address, list(request.owner_user_ids))
        if result is None:
            return pb.CreateCarResponse(ok=False, message="topic_prefix existiert bereits")
        return pb.CreateCarResponse(ok=True, id=result["id"], message="created")

    def UpdateCar(self, request, _context):
        ok = self._update_car(request.id, request.name, request.topic_prefix, request.home_address, list(request.owner_user_ids))
        return pb.StatusResponse(ok=ok, message="updated" if ok else "not found")

    def DeleteCar(self, request, _context):
        ok = self._delete_car(request.id)
        return pb.StatusResponse(ok=ok, message="deleted" if ok else "not found")

    def TriggerFirmwareUpdate(self, request, _context):
        device = request.device
        if not device:
            return pb.StatusResponse(ok=False, message="device required")
        self._on_trigger_firmware_update(device)
        return pb.StatusResponse(ok=True, message=f"OTA-OK gesendet an {device}")

    def TriggerSatelliteRestart(self, request, _context):
        device = request.device
        if not device:
            return pb.StatusResponse(ok=False, message="device required")
        self._on_trigger_satellite_restart(device)
        return pb.StatusResponse(ok=True, message=f"Neustart-Kommando gesendet an {device}")

    def GetDevices(self, _request, _context):
        rooms_raw = self._get_devices()
        rooms_pb = []
        for r in rooms_raw:
            devices_pb = [
                pb.DeviceInfo(
                    id=d["id"],
                    name=d["name"],
                    category=d["category"],
                    states=d["states"],
                    current=d["current"],
                    state_types=d["state_types"],
                    state_enum_values={
                        k: pb.EnumValues(values=v) for k, v in d["state_enum_values"].items()
                    },
                    state_writable=d["state_writable"],
                )
                for d in r["devices"]
            ]
            rooms_pb.append(pb.RoomInfo(key=r["key"], name=r["name"], devices=devices_pb))
        return pb.GetDevicesResponse(rooms=rooms_pb)

    def ControlDevice(self, request, _context):
        log.info(
            f"[grpc] ControlDevice: device={request.device_id!r}"
            f" state={request.state!r} value={request.value!r}"
        )
        ok = self._control_device(request.device_id, request.state, request.value)
        msg = "OK" if ok else "Gerät oder State nicht gefunden"
        return pb.StatusResponse(ok=ok, message=msg)

    # ------------------------------------------------------------------
    # Car

    def GetCarState(self, _request, _context):
        cars = self._get_all_cars()
        if cars:
            state, home = cars[0]
            if state is not None and state.available:
                return pb.CarStateResponse(available=True, state=_car_to_pb(state, home))
        return pb.CarStateResponse(available=False)

    def GetAllCarStates(self, _request, _context):
        protos = [
            _car_to_pb(state, home)
            for state, home in self._get_all_cars()
            if state is not None and state.available
        ]
        return pb.GetAllCarStatesResponse(states=protos)

    # ------------------------------------------------------------------
    # Event stream

    def SubscribeEvents(self, request, context):
        sub = _Subscriber(list(request.event_types))
        with self._subs_lock:
            self._subscribers.append(sub)
        log.info(f"[grpc] Neuer Event-Subscriber (filter={list(request.event_types) or 'alle'})")

        try:
            while context.is_active():
                result = sub.get(timeout=1.0)
                if result is None:
                    break           # sentinel: server closed the stream
                if result is queue.Empty:
                    continue        # timeout, check context.is_active() again
                yield result
        finally:
            with self._subs_lock:
                if sub in self._subscribers:
                    self._subscribers.remove(sub)
            log.info("[grpc] Event-Subscriber getrennt")

    # ------------------------------------------------------------------
    # Satellite Proxy

    def RegisterProxy(self, request_iterator, context):
        """
        Bidirectional stream: proxy → heartbeats, Hannah → ProxyCommand.

        On first connection Hannah disables its UDP server.
        On last disconnection Hannah re-enables it.
        """
        q: queue.Queue = queue.Queue()
        with self._proxy_lock:
            self._proxy_queues.append(q)
            is_first = len(self._proxy_queues) == 1
            # A proxy reconnected within the grace period — cancel the pending
            # UDP/discovery revert so it never fires.
            if self._proxy_revert_timer is not None:
                self._proxy_revert_timer.cancel()
                self._proxy_revert_timer = None

        if is_first:
            log.info("[grpc] Erster Proxy verbunden — UDP-Server wird deaktiviert")
            self._disable_udp()

        # Send initial ACK
        yield pb.ProxyCommand(
            ack=pb.ProxyAck(udp_disabled=True, message="UDP-Server gestoppt")
        )

        proxy_id = "unknown"

        def _drain():
            nonlocal proxy_id
            discovery_published = False
            try:
                for hb in request_iterator:
                    proxy_id = hb.proxy_id
                    log.debug(f"[grpc] Heartbeat von Proxy '{proxy_id}'")
                    # Der Proxy leitet keine Einzel-Heartbeats pro Satellit weiter (nur
                    # einen Stream-Heartbeat für die ganze Verbindung) — bei jedem davon
                    # last_seen für alle aktuell bekannten Proxy-Satelliten mitziehen,
                    # sonst friert er nach der ersten Registrierung für immer ein.
                    with self._proxy_sat_lock:
                        proxy_satellite_ids = list(self._proxy_satellites)
                    for device_id in proxy_satellite_ids:
                        self._upsert_satellite(device_id)
                    if not discovery_published and hb.udp_host and hb.udp_port:
                        log.info(
                            f"[grpc] Proxy-Discovery: {hb.udp_host}:{hb.udp_port}"
                            f" → hannah/server wird aktualisiert"
                        )
                        self._on_proxy_discovery(hb.udp_host, hb.udp_port)
                        discovery_published = True
            except Exception as e:
                log.debug(f"[grpc] Proxy-Drain beendet: {e}")
            finally:
                q.put(None)  # signal EOF to yield loop

        drain_thread = threading.Thread(target=_drain, daemon=True, name="proxy-drain")
        drain_thread.start()

        try:
            while context.is_active():
                try:
                    cmd = q.get(timeout=1.0)
                except queue.Empty:
                    continue
                if cmd is None:
                    break  # stream ended
                yield cmd
        finally:
            drain_thread.join(timeout=2)
            with self._proxy_lock:
                if q in self._proxy_queues:
                    self._proxy_queues.remove(q)
                no_more = len(self._proxy_queues) == 0
                if no_more:
                    self._proxy_revert_timer = threading.Timer(
                        _PROXY_REVERT_GRACE_SECONDS, self._revert_proxy_state
                    )
                    self._proxy_revert_timer.daemon = True
                    self._proxy_revert_timer.start()
            if no_more:
                log.info(
                    f"[grpc] Kein Proxy mehr verbunden — UDP-Server + Discovery werden in "
                    f"{_PROXY_REVERT_GRACE_SECONDS}s wiederhergestellt, falls kein Proxy zurückkehrt"
                )
            log.info(f"[grpc] Proxy '{proxy_id}' getrennt")

    def _revert_proxy_state(self):
        """Fired after the grace period once no proxy has reconnected — restores
        Hannah's own UDP server + MQTT discovery (#140)."""
        with self._proxy_lock:
            still_none = len(self._proxy_queues) == 0
            self._proxy_revert_timer = None
        if not still_none:
            return
        log.info("[grpc] Grace-Period abgelaufen, kein Proxy zurück — UDP-Server + Discovery werden wiederhergestellt")
        self._enable_udp()
        self._on_proxy_discovery(None, 0)  # None → Restore Hannah's own address
        if self._on_satellite_change:
            with self._proxy_sat_lock:
                self._proxy_satellites.clear()
            threading.Thread(
                target=self._on_satellite_change, args=({},), daemon=True
            ).start()

    def SubmitSatelliteAudio(self, request, context):
        if self._handle_satellite_audio is None:
            context.set_code(grpc.StatusCode.UNIMPLEMENTED)
            context.set_details("handle_satellite_audio not configured")
            return pb.SubmitSatelliteAudioResponse()

        speaker = request.speaker_user_id or ""
        with self._proxy_sat_lock:
            known = self._proxy_satellites.get(request.device_id)
        room_id = (known or {}).get("room") or self._resolve_satellite_room(request.device_id) or ""
        log.info(
            f"[grpc] SubmitSatelliteAudio: device={request.device_id!r}"
            f" room={room_id!r} bytes={len(request.audio_pcm)}"
            + (f" speaker={speaker!r}" if speaker else " speaker=anonymous")
        )
        if self._on_satellite_change and request.device_id and known is None:
            with self._proxy_sat_lock:
                self._proxy_satellites[request.device_id] = {"room": room_id, "addr": ""}
                snapshot = {d: info["room"] for d, info in self._proxy_satellites.items()}
            threading.Thread(
                target=self._on_satellite_change, args=(snapshot,), daemon=True
            ).start()
        if self.is_captured(request.device_id):
            self.push_capture_audio(
                request.device_id, request.audio_pcm,
                request.sample_rate or 16000, end_of_utterance=True,
            )
            log.debug(f"[grpc/capture] Audio von '{request.device_id}' weitergeleitet ({len(request.audio_pcm)} Bytes)")
            return pb.SubmitSatelliteAudioResponse()

        transcript, answer, intent_name, tts_pcm, sample_rate = self._handle_satellite_audio(
            request.device_id,
            request.audio_pcm,
            speaker,
        )
        return pb.SubmitSatelliteAudioResponse(
            transcript=transcript,
            answer=answer,
            intent_name=intent_name,
            audio_pcm=tts_pcm,
            sample_rate=sample_rate,
        )

    def ProvisionSatellite(self, request, _context):
        """Adapter pre-registriert einen Satelliten vor dem WebFlash."""
        ok = self._provision_satellite(request.seed, request.display_name, request.room_id or None)
        return pb.StatusResponse(ok=ok, message="provisioned" if ok else "failed")

    def NotifySatelliteRegistered(self, request, _context):
        """Proxy meldet: Satellit hat sich via UDP registriert."""
        device, address = request.device_id, request.address
        self._upsert_satellite(device)

        paired = False
        if request.seed:
            paired = self._pair_satellite(device, request.seed)
            if paired:
                log.info("Satellite %s paired", device)
            else:
                log.warning("Satellite %s: seed not found, proceeding without pairing", device)

        room_id = self._resolve_satellite_room(device) or ""
        if not room_id:
            log.warning(f"[grpc] Satellit '{device}' hat keinen Raum in RoomManager — nicht an Adapter weitergeleitet")
            return pb.StatusResponse(ok=True, message="registered without room")
        with self._proxy_sat_lock:
            self._proxy_satellites[device] = {"room": room_id, "addr": address}
            snapshot = {d: info["room"] for d, info in self._proxy_satellites.items()}
        log.info(f"[grpc] Satellit registriert via Proxy: '{device}' (Raum: '{room_id}')")
        if self._on_satellite_change:
            threading.Thread(
                target=self._on_satellite_change, args=(snapshot,), daemon=True
            ).start()
        return pb.StatusResponse(ok=True, message="paired" if paired else "registered")

    def NotifySatelliteGone(self, request, _context):
        """Proxy meldet: Satellit hat sich abgemeldet."""
        device = request.device_id
        with self._proxy_sat_lock:
            self._proxy_satellites.pop(device, None)
            snapshot = {d: info["room"] for d, info in self._proxy_satellites.items()}
        log.info(f"[grpc] Satellit abgemeldet via Proxy: '{device}'")
        if self._on_satellite_change:
            threading.Thread(
                target=self._on_satellite_change, args=(snapshot,), daemon=True
            ).start()
        return pb.StatusResponse(ok=True, message="gone")

    # ------------------------------------------------------------------
    # ioBroker Adapter

    def agent_connected(self) -> bool:
        """True if at least one adapter stream is active."""
        with self._agent_lock:
            return len(self._agent_queues) > 0

    def agent_set_state(self, state_id: str, value: str) -> bool:
        """Push SetState command to all connected adapters. Returns True if at least one is active."""
        cmd = pb.AgentCommand(set_state=pb.AgentSetState(state_id=state_id, value=value))
        with self._agent_lock:
            for q in self._agent_queues:
                q.put(cmd)
            return len(self._agent_queues) > 0

    def agent_watch_more(self, state_ids: list[str]) -> bool:
        """Push WatchMore request to all connected adapters."""
        cmd = pb.AgentCommand(watch_more=pb.AgentWatchMore(state_ids=state_ids))
        with self._agent_lock:
            for q in self._agent_queues:
                q.put(cmd)
            return len(self._agent_queues) > 0

    def agent_set_resident(self, resident_id: str, presence_state: int, resident_type: pb.ResidentType) -> bool:
        """Push SetResident command to all connected adapters."""
        cmd = pb.AgentCommand(set_resident=pb.AgentSetResident(
            resident_id=resident_id,
            presence_state=presence_state,
            type=resident_type,
        ))
        with self._agent_lock:
            for q in self._agent_queues:
                q.put(cmd)
            return len(self._agent_queues) > 0

    def agent_set_resident_mood(self, resident_id: str, mood: int, resident_type: pb.ResidentType) -> bool:
        """Push SetResidentMood command to all connected adapters."""
        cmd = pb.AgentCommand(set_resident_mood=pb.AgentSetResidentMood(
            resident_id=resident_id,
            mood=mood,
            type=resident_type,
        ))
        with self._agent_lock:
            for q in self._agent_queues:
                q.put(cmd)
            return len(self._agent_queues) > 0

    def get_proxy_device_id(self, key: str) -> str:
        """Returns the device_id for a proxy satellite key (which IS the device_id now)."""
        return key

    def agent_satellite_update(self, device_id: str, room: str, address: str, online: bool,
                               volume: int = None, mute: bool = None,
                               display_name: str = "") -> bool:
        """Push a satellite online/offline or state update to all connected adapters."""
        kwargs = dict(device_id=device_id, room=room, address=address, online=online)
        if volume is not None:
            kwargs["volume"] = volume
        if mute is not None:
            kwargs["mute"] = mute
        if display_name:
            kwargs["display_name"] = display_name
        cmd = pb.AgentCommand(satellite_update=pb.AgentSatelliteUpdate(**kwargs))
        with self._agent_lock:
            for q in self._agent_queues:
                q.put(cmd)
            return len(self._agent_queues) > 0

    def agent_firmware_event(self, device: str, version: str, update_available: bool = False) -> bool:
        """Push a firmware version update to all connected adapters."""
        cmd = pb.AgentCommand(firmware_event=pb.AgentFirmwareEvent(
            device=device, version=version, update_available=update_available,
        ))
        with self._agent_lock:
            for q in self._agent_queues:
                q.put(cmd)
            return len(self._agent_queues) > 0

    def agent_ble_update(self, label: str, mac: str, room: str, satellite: str, rssi: int) -> bool:
        """Push a BLE tag location update to all connected adapters."""
        cmd = pb.AgentCommand(ble_update=pb.AgentBleUpdate(
            label=label, mac=mac, room=room or "", satellite=satellite or "", rssi=rssi,
        ))
        with self._agent_lock:
            for q in self._agent_queues:
                q.put(cmd)
            return len(self._agent_queues) > 0

    def agent_sensor_update(self, device: str, temperature: float, pressure: float,
                            humidity: float, iaq: float = 0.0, iaq_accuracy: int = 0,
                            co2_equiv: float = 0.0, voc_equiv: float = 0.0) -> bool:
        """Push a satellite sensor reading to all connected adapters."""
        cmd = pb.AgentCommand(sensor_update=pb.AgentSensorUpdate(
            device=device,
            temperature=temperature,
            pressure=pressure,
            humidity=humidity,
            iaq=iaq,
            iaq_accuracy=iaq_accuracy,
            co2_equiv=co2_equiv,
            voc_equiv=voc_equiv,
        ))
        with self._agent_lock:
            n = len(self._agent_queues)
            for q in self._agent_queues:
                q.put(cmd)
            log.debug(f"agent_sensor_update({device}): pushed to {n} adapter(s)")
            return n > 0

    def agent_satellite_deleted(self, device_id: str, room: str) -> bool:
        """Push a satellite-deleted command to all connected adapters."""
        cmd = pb.AgentCommand(satellite_deleted=pb.AgentSatelliteDeleted(device_id=device_id, room=room))
        with self._agent_lock:
            for q in self._agent_queues:
                q.put(cmd)
            return len(self._agent_queues) > 0

    def agent_resident_answered(self, correlation_id: str, answer: str) -> bool:
        """Push AgentResidentAnswered to all connected adapters."""
        cmd = pb.AgentCommand(resident_answered=pb.AgentResidentAnswered(
            correlation_id=correlation_id,
            answer=answer,
        ))
        with self._agent_lock:
            for q in self._agent_queues:
                q.put(cmd)
            return len(self._agent_queues) > 0

    # ------------------------------------------------------------------
    # Timer Service (called from main.py)

    def timer_connected(self) -> bool:
        """True if the Timer Service stream is currently active."""
        with self._timer_lock:
            return self._timer_queue is not None

    def timer_send_ready(self) -> bool:
        """Send TimerReady to the connected Timer Service. No-op if not connected."""
        with self._timer_lock:
            if self._timer_queue is None:
                return False
            self._timer_queue.put(pb.TimerCommand(ready=pb.TimerReady()))
        log.info("[grpc] TimerReady gesendet")
        return True

    def timer_create(self, timer_id: str, label: str, fire_at: int,
                     metadata: Optional[dict] = None) -> bool:
        """Send TimerCreate to the Timer Service. Returns False if not connected.
        metadata is opaque to the Timer Service — it's echoed back verbatim on
        TimerFired/TimerListResponse (e.g. "room", "roomie_id", "trigger_id")."""
        cmd = pb.TimerCommand(create=pb.TimerCreate(
            timer_id=timer_id, label=label, fire_at=fire_at, metadata=metadata or {},
        ))
        with self._timer_lock:
            if self._timer_queue is None:
                return False
            self._timer_queue.put(cmd)
        return True

    def timer_cancel(self, timer_id: str) -> bool:
        """Send TimerCancel to the Timer Service. Returns False if not connected."""
        cmd = pb.TimerCommand(cancel=pb.TimerCancel(timer_id=timer_id))
        with self._timer_lock:
            if self._timer_queue is None:
                return False
            self._timer_queue.put(cmd)
        return True

    def timer_list_request(self) -> bool:
        """Send TimerListRequest to the Timer Service. Returns False if not connected."""
        cmd = pb.TimerCommand(list=pb.TimerListRequest())
        with self._timer_lock:
            if self._timer_queue is None:
                return False
            self._timer_queue.put(cmd)
        return True
    
    def _request_timer_list(self, timeout: float = 5.0) -> Optional[list]:
        """Sends TimerListRequest and blocks for the matching TimerListResponse.

        Bridges the async TimerConnect stream (Timer Service pushes TimerListResponse
        whenever it feels like it) to a synchronous unary caller. Returns None if no
        Timer Service is connected or the response didn't arrive within timeout.
        """
        waiter: queue.Queue = queue.Queue(maxsize=1)
        with self._timer_lock:
            if self._timer_queue is None:
                return None
            self._timer_list_waiters.append(waiter)
        if not self.timer_list_request():
            with self._timer_lock:
                if waiter in self._timer_list_waiters:
                    self._timer_list_waiters.remove(waiter)
            return None
        try:
            return waiter.get(timeout=timeout)
        except queue.Empty:
            with self._timer_lock:
                if waiter in self._timer_list_waiters:
                    self._timer_list_waiters.remove(waiter)
            return None

    # ------------------------------------------------------------------
    # Timer Admin (unary RPCs, independent of the TimerConnect stream — #97)

    def GetTimers(self, request, _context):
        """Returns the requesting user's active timers from the Timer Service."""
        timers = self._request_timer_list()
        if timers is None:
            return pb.GetTimersResponse(timers=[])
        now = int(time.time())
        result = []
        for t in timers:
            if t.metadata.get("user_id") != str(request.user_id):
                continue
            result.append(pb.Timer(
                timer_id=t.timer_id,
                label=t.label,
                seconds=max(0, t.fire_at - now),
                room_id=t.metadata.get("room", ""),
                user_id=request.user_id,
            ))
        return pb.GetTimersResponse(timers=result)

    def DeleteTimer(self, request, _context):
        """Cancels a timer via the Timer Service.

        requestor_id is accepted but not yet enforced against the timer's owner —
        ownership policy is still undecided (#97), so any connected caller can
        currently cancel any timer_id.
        """
        ok = self.timer_cancel(request.timer_id)
        return pb.StatusResponse(ok=ok, message="" if ok else "Timer Service nicht verbunden")

    def AgentConnect(self, request_iterator, context):
        """
        Bidirektionaler Stream: Adapter → State-Updates, Hannah → Geräte-Befehle.

        Der Adapter sendet AgentMessage-Frames (state_update / resident_update /
        text_command); Hannah antwortet mit AgentCommand-Frames (set_state /
        watch_more). Beim Disconnect werden alle gepufferten Befehle verworfen.
        """
        q: queue.Queue = queue.Queue()
        with self._agent_lock:
            self._agent_queues.append(q)
        log.info("[grpc] ioBroker-Adapter connected")

        if self._on_agent_connect:
            try:
                self._on_agent_connect()
            except Exception as e:
                log.warning(f"[grpc] on_agent_connect Fehler: {e}")

        def _drain():
            try:
                for msg in request_iterator:
                    which = msg.WhichOneof("payload")
                    if which == "state_update" and self._on_agent_state:
                        u = msg.state_update
                        self._on_agent_state(u.state_id, u.value, u.ack, u.ts)
                    elif which == "resident_update" and self._on_agent_resident:
                        r = msg.resident_update
                        if r.HasField("mood_level"):
                            self._on_agent_resident(r.roomie_id, r.name, r.presence_state, r.type, r.mood_level)
                        else:
                            self._on_agent_resident(r.roomie_id, r.name, r.presence_state, r.type)
                        
                    elif which == "text_command" and self._on_agent_text_command:
                        answer, intent = self._on_agent_text_command(msg.text_command.text)
                        q.put(pb.AgentCommand(text_answer=pb.AgentTextAnswer(
                            text=answer, intent=intent,
                        )))
                    elif which == "satellite_control" and self._on_agent_satellite_control:
                        sc = msg.satellite_control
                        ctrl = sc.WhichOneof("control")
                        if ctrl:
                            self._on_agent_satellite_control(
                                sc.room, ctrl, getattr(sc, ctrl),
                                device_id=sc.device_id or "",
                            )
                    elif which == "set_resident" and self._on_agent_set_resident:
                        r = msg.set_resident
                        self._on_agent_set_resident(r.resident_id, r.presence_state, r.type)
                    elif which == "send_snapshot" and self._on_agent_device_snapshot:
                        snapshot = msg.send_snapshot
                        self._on_agent_device_snapshot(snapshot.devices)
                    elif which == "send_residents" and self._on_agent_send_residents:
                        r = msg.send_residents
                        self._on_agent_send_residents(r.residents)
                    elif which == "send_rooms" and self._on_agent_room_snapshot:
                        self._on_agent_room_snapshot(msg.send_rooms.rooms)
                    elif which == "ask_resident" and self._on_agent_ask_resident:
                        ar = msg.ask_resident
                        threading.Thread(
                            target=self._on_agent_ask_resident,
                            args=(ar.correlation_id, ar.room, ar.question),
                            daemon=True, name="agent-ask",
                        ).start()
                    else:
                        log.warning(f"[grpc] Unrecognized AgentMessage payload: {which}")
                            

            except Exception as e:
                log.debug(f"[grpc] Adapter drain ended: {e}")
                log.debug(f"[grpc] Adapter drain ended: {e}", exc_info=True)
            finally:
                q.put(None)

        drain_thread = threading.Thread(target=_drain, daemon=True, name="agent-drain")
        drain_thread.start()

        try:
            while context.is_active():
                try:
                    cmd = q.get(timeout=1.0)
                except queue.Empty:
                    continue
                if cmd is None:
                    break
                yield cmd
        finally:
            drain_thread.join(timeout=2)
            with self._agent_lock:
                if q in self._agent_queues:
                    self._agent_queues.remove(q)
            log.info("[grpc] ioBroker-Adapter disconnected")

    # ------------------------------------------------------------------
    # Automations

    def push_automation_update(self, user_id: int, automation: str, enabled: bool):
        """Fan-out an alle AutomationConnect-Verbindungen, die sich für genau diese
        Automation registriert haben (nicht an alle — jede Verbindung sieht nur ihre eigene)."""
        cmd = pb.AutomationCommand(
            state_changed=pb.AutomationStateChanged(user_id=user_id, enabled=enabled)
        )
        with self._automation_lock:
            for sub in self._automation_subs:
                if sub.automation == automation:
                    sub.put(cmd)

    def AutomationConnect(self, request_iterator, context):
        """
        Bidirektionaler Stream: Automation-Service → AutomationMessage, Hannah → AutomationCommand.

        Der Service meldet sich einmalig mit AutomationRegister{automation}; danach bekommt
        er einen Snapshot aller User, für die diese Automation gerade aktiviert ist, gefolgt
        von Live-Deltas (AutomationStateChanged), sobald sich das über SetAutomation/Sprache
        ändert. Bewusst kein Fan-out an alle Verbindungen wie bei AgentConnect — jede
        Verbindung ist auf genau eine Automation gefiltert (siehe push_automation_update).
        """
        sub = _AutomationSub()
        with self._automation_lock:
            self._automation_subs.append(sub)

        def _drain():
            try:
                for msg in request_iterator:
                    which = msg.WhichOneof("payload")
                    if which == "register":
                        sub.automation = msg.register.automation
                        log.info(f"[grpc] Automation-Service registriert: {sub.automation!r}")
                        user_ids = self._on_automation_register(sub.automation)
                        sub.put(pb.AutomationCommand(
                            snapshot=pb.AutomationSnapshot(user_ids=user_ids)
                        ))
                    else:
                        log.warning(f"[grpc] Unrecognized AutomationMessage payload: {which}")
            except Exception as e:
                log.debug(f"[grpc] Automation drain ended: {e}")
            finally:
                sub.close()

        drain_thread = threading.Thread(target=_drain, daemon=True, name="automation-drain")
        drain_thread.start()

        try:
            while context.is_active():
                cmd = sub.get(timeout=1.0)
                if cmd is None:
                    break
                if cmd is queue.Empty:
                    continue
                yield cmd
        finally:
            drain_thread.join(timeout=2)
            with self._automation_lock:
                if sub in self._automation_subs:
                    self._automation_subs.remove(sub)
            log.info(f"[grpc] Automation-Service getrennt: {sub.automation!r}")

    def TimerConnect(self, request_iterator, context):
        """
        Bidirektionaler Stream: Timer Service → TimerMessage, Hannah → TimerCommand.

        Nur ein Timer Service kann gleichzeitig verbunden sein. Bei erneutem
        Connect wird die bestehende Verbindung verdrängt.
        """
        q: queue.Queue = queue.Queue()
        with self._timer_lock:
            if self._timer_queue is not None:
                log.warning("[grpc] Timer Service reconnect — bestehende Verbindung wird verdrängt")
                self._timer_queue.put(None)  # EOF für alten Stream
            self._timer_queue = q
        log.info("[grpc] Timer Service connected")

        if self._on_timer_connected:
            try:
                self._on_timer_connected()
            except Exception as e:
                log.warning(f"[grpc] on_timer_connected Fehler: {e}")

        def _drain():
            try:
                for msg in request_iterator:
                    which = msg.WhichOneof("payload")
                    if which == "ack":
                        log.info(
                            f"[grpc] Timer Service Ack: {msg.ack.message!r}"
                            f" ({msg.ack.active_timers} aktive Timer)"
                        )
                    elif which == "fired":
                        fired = msg.fired
                        log.info(f"[grpc] TimerFired: {fired.timer_id!r} ({fired.label!r})")
                        if self._on_timer_fired:
                            try:
                                self._on_timer_fired(fired.timer_id, fired.label, dict(fired.metadata))
                            except Exception as e:
                                log.error(f"[grpc] on_timer_fired Fehler: {e}")
                    elif which == "list":
                        log.debug(f"[grpc] TimerListResponse: {len(msg.list.timers)} Timer")
                        timers = list(msg.list.timers)
                        if self._on_timer_list:
                            try:
                                self._on_timer_list(timers)
                            except Exception as e:
                                log.error(f"[grpc] on_timer_list Fehler: {e}")
                        with self._timer_lock:
                            waiters, self._timer_list_waiters = self._timer_list_waiters, []
                        for waiter in waiters:
                            waiter.put(timers)
                    else:
                        log.warning(f"[grpc] Unbekanntes TimerMessage-Payload: {which!r}")
            except Exception as e:
                log.debug(f"[grpc] Timer Service drain ended: {e}")
            finally:
                q.put(None)

        drain_thread = threading.Thread(target=_drain, daemon=True, name="timer-drain")
        drain_thread.start()

        try:
            while context.is_active():
                try:
                    cmd = q.get(timeout=1.0)
                except queue.Empty:
                    continue
                if cmd is None:
                    break
                yield cmd
        finally:
            drain_thread.join(timeout=2)
            with self._timer_lock:
                if self._timer_queue is q:
                    self._timer_queue = None
            log.info("[grpc] Timer Service disconnected")

    # ------------------------------------------------------------------
    # Wakeword Capture

    def is_captured(self, device_id: str) -> bool:
        with self._capture_lock:
            return device_id in self._captured_satellites

    def push_capture_audio(self, device_id: str, pcm: bytes, sample_rate: int,
                           end_of_utterance: bool = False):
        """Route raw PCM from a captured satellite into its StreamSatelliteAudio queue."""
        with self._capture_lock:
            q = self._captured_satellites.get(device_id)
        if q is not None:
            q.put(pb.SatelliteAudioChunk(
                pcm=pcm,
                sample_rate=sample_rate,
                end_of_utterance=end_of_utterance,
            ))

    def RequestSatelliteCapture(self, request, _context):
        device_id = request.device_id
        all_sats = self._get_satellites()
        if device_id not in all_sats:
            return pb.SatelliteCaptureResponse(ok=False, error=f"Satellit '{device_id}' nicht gefunden")
        with self._capture_lock:
            if device_id in self._captured_satellites:
                return pb.SatelliteCaptureResponse(ok=False, error=f"'{device_id}' bereits belegt")
            self._captured_satellites[device_id] = queue.Queue()
        sample_type = request.sample_type or "noise"
        log.info(f"[grpc/capture] Satellit '{device_id}' in Capture-Modus versetzt (type={sample_type})")
        self._on_set_capture(device_id, True, sample_type)
        return pb.SatelliteCaptureResponse(ok=True)

    def ReleaseSatelliteCapture(self, request, _context):
        device_id = request.device_id
        with self._capture_lock:
            q = self._captured_satellites.pop(device_id, None)
        if q is not None:
            q.put(None)  # EOF für laufenden StreamSatelliteAudio
            log.info(f"[grpc/capture] Satellit '{device_id}' aus Capture-Modus entlassen")
            self._on_set_capture(device_id, False)
        return pb.StatusResponse(ok=True, message="released")

    def TriggerPlink(self, request, _context):
        device_id = request.device_id
        duration  = request.record_duration if request.record_duration > 0 else 3.0
        self._on_trigger_plink(device_id, duration)
        return pb.StatusResponse(ok=True)


    def StreamSatelliteAudio(self, request, context):
        device_id = request.device_id
        with self._capture_lock:
            q = self._captured_satellites.get(device_id)
        if q is None:
            context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
            context.set_details(f"'{device_id}' ist nicht im Capture-Modus — RequestSatelliteCapture zuerst aufrufen")
            return

        log.info(f"[grpc/capture] StreamSatelliteAudio gestartet für '{device_id}'")
        try:
            while context.is_active():
                try:
                    chunk = q.get(timeout=1.0)
                except queue.Empty:
                    continue
                if chunk is None:
                    break  # ReleaseSatelliteCapture wurde aufgerufen
                yield chunk
        finally:
            # Cleanup falls Client trennt ohne ReleaseSatelliteCapture
            with self._capture_lock:
                if self._captured_satellites.get(device_id) is q:
                    self._captured_satellites.pop(device_id, None)
                    self._on_set_capture(device_id, False)
                    log.info(f"[grpc/capture] '{device_id}' auto-released nach Stream-Disconnect")

    def EnrollVoiceprint(self, request, _context):
        if self._enroll_voiceprint is None:
            return pb.StatusResponse(
                ok=False,
                message="Kein Voice-ID-Backend konfiguriert.",
            )
        log.info(
            f"[grpc] EnrollVoiceprint: user={request.user_id!r}"
            f" bytes={len(request.audio_pcm)} rate={request.sample_rate}"
        )
        ok, msg = self._enroll_voiceprint(
            request.user_id, request.audio_pcm, request.sample_rate
        )
        return pb.StatusResponse(ok=ok, message=msg)


# ------------------------------------------------------------------
# Server lifecycle

class GrpcServer:
    def __init__(self, cfg: dict, servicer: HannahServicer):
        self._host = cfg.get("host", "0.0.0.0")
        self._port = int(cfg.get("port", 50051))
        self._server: Optional[grpc.Server] = None
        self._servicer = servicer
        self._version_interceptor = ProtocolVersionInterceptor(
            expected_version=read_proto_version(),
            enforce=cfg.get("enforce_protocol_version", False),
        )

    def start(self):
        self._server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=8),
            interceptors=[self._version_interceptor],
        )
        pb_grpc.add_HannahServiceServicer_to_server(self._servicer, self._server)
        addr = f"{self._host}:{self._port}"
        self._server.add_insecure_port(addr)
        self._server.start()
        log.info(f"gRPC-Server lauscht auf {addr}")

    def stop(self):
        if self._server:
            self._server.stop(grace=2)
            log.info("gRPC-Server beendet.")

    def set_protocol_version_enforcement(self, enforce: bool) -> None:
        """Reject-Mode für den Protocol-Version-Check gezielt an-/ausschalten (#60).

        Solange nicht alle 6 externen Clients x-proto-version mitschicken, muss
        das False bleiben (nur Logging) — sonst lehnt Hannah jeden RPC ab.
        """
        self._version_interceptor.enforce = enforce
        log.info(f"[grpc/version] Protocol-Version-Enforcement: {'AN' if enforce else 'AUS'}")


# ------------------------------------------------------------------
# Event factory helpers (called from main.py)

def make_car_parked_event(state, home_address: str = "") -> pb.HannahEvent:
    return pb.HannahEvent(
        event_type="car.parked",
        timestamp=datetime.now(timezone.utc).isoformat(),
        car_state=_car_to_pb(state, home_address),
    )


def make_resident_event(roomie_id: str, display_name: str, event: str) -> pb.HannahEvent:
    """event: 'arrived' | 'departed'"""
    return pb.HannahEvent(
        event_type=f"resident.{event}",
        timestamp=datetime.now(timezone.utc).isoformat(),
        resident_event=pb.ResidentEventProto(
            roomie_id=roomie_id,
            display_name=display_name,
            event=event,
        ),
    )


def make_firmware_event(
    device: str, version: str, update_available: bool = False, new_version: Optional[str] = None,
) -> pb.HannahEvent:
    kwargs = dict(device=device, version=version, update_available=update_available)
    if new_version:
        kwargs["new_version"] = new_version
    return pb.HannahEvent(
        event_type="satellite.firmware",
        timestamp=datetime.now(timezone.utc).isoformat(),
        firmware_event=pb.FirmwareEventProto(**kwargs),
    )


def make_system_notification_event(text: str) -> pb.HannahEvent:
    return pb.HannahEvent(
        event_type="system.notification",
        timestamp=datetime.now(timezone.utc).isoformat(),
        system_notification=pb.SystemNotificationEvent(text=text),
    )


# ------------------------------------------------------------------
# Conversion helpers

def _user_to_pb(u: User) -> pb.User:
    return pb.User(
        id=u.id or 0,
        user_name=u.username or "",
        display_name=u.display_name or "",
        trust_level=int(u.trust_level or 5),
        active=bool(u.is_active),
        system_messages=bool(u.system_messages),
        linked_accounts={acc.provider: acc.external_id for acc in u.linked_accounts},
        email=u.email or "",
        type=u.type or "",
        enabled_automations=list(u.enabled_automations),
    )


def _resident_to_pb(r) -> pb.Resident:
    return pb.Resident(
        id=r.id,
        roomie_id=r.roomie_id,
        display_name=r.display_name or "",
        type=type(r).__name__.lower(),
        home=r.is_home(),
    )


def _trigger_to_pb(t: dict) -> pb.Trigger:
    return pb.Trigger(
        id=t["id"],
        when_json=json.dumps(t.get("when") or {}),
        cancel_when_json=json.dumps(t["cancel_when"]) if t.get("cancel_when") else "",
        on_response_json=json.dumps(t.get("on_response") or []),
        actions_json=json.dumps(t.get("actions") or []),
        say=t.get("say") or "",
        ask=t.get("ask") or "",
        rephrase=bool(t.get("rephrase")),
        room=t.get("room") or "all",
        cooldown=int(t.get("cooldown") or 3600),
        delay=t.get("delay") or "",
    )


def _alarm_to_pb(a: dict) -> pb.Alarm:
    return pb.Alarm(
        id=a["id"],
        satellite_id=a.get("satellite_id") or "",
        time=a.get("time") or "",
        weekdays=a.get("weekdays") or [],
        skip_dates=a.get("skip_dates") or [],
        one_shot_date=a.get("one_shot_date") or "",
        enabled=bool(a.get("enabled", True)),
        label=a.get("label") or "",
        user_id=a.get("user_id") or 0,
    )


def _category_to_pb(c: dict) -> pb.Category:
    return pb.Category(
        id=c["id"],
        name=c["name"],
        parent_id=c.get("parent") or 0,
    )


def _setting_to_pb(s: dict) -> pb.Setting:
    return pb.Setting(
        id=s["id"],
        category_id=s["category"],
        name=s["name"],
        value=json.dumps(s.get("value")),
    )


def _ble_tag_to_pb(t: dict) -> pb.BleTag:
    return pb.BleTag(
        id=t["id"],
        mac_address=t.get("mac_address") or "",
        label=t.get("label") or "",
        user_id=t.get("user_id") or 0,
    )


def _car_config_to_pb(c: dict) -> pb.Car:
    return pb.Car(
        id=c["id"],
        name=c.get("name") or "",
        topic_prefix=c.get("topic_prefix") or "",
        home_address=c.get("home_address") or "",
        owner_user_ids=c.get("owner_user_ids") or [],
    )


def _car_to_pb(state, home_address: str = "") -> pb.CarStateProto:
    return pb.CarStateProto(
        latitude=state.latitude or 0.0,
        longitude=state.longitude or 0.0,
        address=state.address or "",
        is_moving=bool(state.is_moving),
        position_date=state.position_date or 0,
        odometer=state.odometer or 0,
        total_range=state.total_range or 0,
        is_car_locked=bool(state.is_car_locked),
        door_lock_status=state.door_lock_status or "",
        overall_status=state.overall_status or "",
        doors=state.doors or {},
        windows=state.windows or {},
        owner_roomie=state.owner_roomie or "",
        display_name=state.display_name or "",
        plate=state.plate or "",
        vin=state.vin or "",
        home_address=home_address,
    )
