"""
Hannah gRPC Server

Exposes HannahService to external services (Telegram bot, web UI, …).
Runs in its own thread pool alongside the main event loop.
"""
import logging
import queue
import threading
from concurrent import futures
from datetime import datetime, timezone
from typing import Callable, Optional

import grpc

from hannah.proto import hannah_pb2 as pb
from hannah.proto import hannah_pb2_grpc as pb_grpc
from hannah.user_registry import UserRegistry

log = logging.getLogger(__name__)


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
# Servicer

class HannahServicer(pb_grpc.HannahServiceServicer):
    """
    gRPC service implementation.

    All heavy work is delegated to callbacks passed in from main.py so this
    class stays free of business logic and is easy to test in isolation.
    """

    def __init__(
        self,
        registry: UserRegistry,
        handle_text: Callable[[str], tuple[str, str]],
        handle_voice: Callable[[bytes], tuple[str, str, str, bytes]],
        announce: Callable[[str, str], None],
        get_satellites: Callable[[], dict],
        get_car_state: Callable[[], Optional[object]],      # → CarState | None (erster Tracker)
        get_all_cars: Optional[Callable[[], list]] = None,  # → [(CarState, home_address)]
        handle_satellite_audio: Optional[Callable] = None,  # (device, room, pcm) → (transcript, answer, intent, pcm, rate)
        disable_udp: Optional[Callable[[], None]] = None,
        enable_udp: Optional[Callable[[], None]] = None,
        on_proxy_discovery: Optional[Callable[[str, int], None]] = None,  # (host, port) — None args = restore own address
        get_devices: Optional[Callable[[], list]] = None,           # → [{key,name,devices:[...]}]
        control_device: Optional[Callable[[str, str, str], bool]] = None,  # (device_id, state, value) → bool
        enroll_voiceprint: Optional[Callable[[str, bytes, int], tuple]] = None,  # (roomie_id, pcm, rate) → (ok, msg)
        on_satellite_change: Optional[Callable[[dict], None]] = None,           # ({device: room}) bei Register/Disconnect via Proxy
    ):
        self._registry              = registry
        self._handle_text           = handle_text
        self._handle_voice          = handle_voice
        self._announce              = announce
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

        self._subscribers: list[_Subscriber] = []
        self._subs_lock = threading.Lock()

        # Satelliten die der Proxy gemeldet hat: {device: room}
        self._proxy_satellites: dict[str, str] = {}
        self._proxy_sat_lock = threading.Lock()

        # Per-connection command queues for active proxy streams
        self._proxy_queues: list[queue.Queue] = []
        self._proxy_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public: proxy helpers (called from main.py)

    def has_proxy(self) -> bool:
        """True if at least one proxy stream is currently connected."""
        with self._proxy_lock:
            return len(self._proxy_queues) > 0

    def proxy_satellites(self) -> dict[str, str]:
        """Aktuell vom Proxy gemeldete Satelliten: {device: room}."""
        with self._proxy_sat_lock:
            return dict(self._proxy_satellites)

    def push_audio_to_proxy(self, device_id: str, pcm: bytes, sample_rate: int):
        """Push a PlayAudioCommand to all connected proxy streams."""
        cmd = pb.ProxyCommand(
            play_audio=pb.PlayAudioCommand(
                device_id=device_id,
                audio_pcm=pcm,
                sample_rate=sample_rate,
            )
        )
        with self._proxy_lock:
            for q in list(self._proxy_queues):
                q.put(cmd)

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
        users = self._registry.get_all(include_inactive=request.include_inactive)
        return pb.GetUsersResponse(users=[_user_to_pb(u) for u in users])

    def GetUser(self, request, context):
        lookup = request.WhichOneof("lookup")
        if lookup == "roomie_id":
            raw = self._registry.get_by_roomie(request.roomie_id)
        elif lookup == "uuid":
            raw = self._registry.get_by_uuid(request.uuid)
        elif lookup == "linked_account":
            la = request.linked_account
            raw = self._registry.get_by_linked_account(la.service, la.account_id)
        else:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("Exactly one lookup field must be set.")
            return pb.UserResponse()

        if raw is None:
            return pb.UserResponse(found=False)
        return pb.UserResponse(found=True, user=_user_to_pb(raw))

    def LinkAccount(self, request, _context):
        ok = self._registry.link_account(request.roomie_id, request.service, request.account_id)
        msg = "verknüpft" if ok else f"Roomie {request.roomie_id!r} nicht gefunden"
        return pb.StatusResponse(ok=ok, message=msg)

    def UnlinkAccount(self, request, _context):
        ok = self._registry.unlink_account(request.service, request.account_id)
        msg = "entfernt" if ok else "Account nicht gefunden"
        return pb.StatusResponse(ok=ok, message=msg)

    def SetTrustLevel(self, request, _context):
        ok = self._registry.set_trust_level(request.roomie_id, request.level)
        msg = "aktualisiert" if ok else f"Roomie {request.roomie_id!r} nicht gefunden"
        return pb.StatusResponse(ok=ok, message=msg)

    def SetSystemMessages(self, request, _context):
        ok = self._registry.set_system_messages(request.roomie_id, request.enabled)
        msg = "aktualisiert" if ok else f"Roomie {request.roomie_id!r} nicht gefunden"
        return pb.StatusResponse(ok=ok, message=msg)

    # ------------------------------------------------------------------
    # Control

    def _roomie_from_request(self, source_service: str, source_user_id: str) -> str:
        """Löst source_service + source_user_id via linked_accounts auf eine roomie_id auf."""
        if not source_service or not source_user_id:
            return ""
        user = self._registry.get_by_linked_account(source_service, source_user_id)
        return user.get("roomie_id", "") if user else ""

    def SubmitText(self, request, _context):
        roomie_id = self._roomie_from_request(request.source_service, request.source_user_id)
        log.info(
            f"[grpc] SubmitText von {request.source_service}:{request.source_user_id}"
            f" (roomie={roomie_id or 'anonym'}) — {request.text!r}"
        )
        answer, intent_name = self._handle_text(request.text, roomie_id)
        return pb.SubmitTextResponse(answer=answer, intent_name=intent_name)

    def SubmitVoice(self, request, _context):
        roomie_id = self._roomie_from_request(request.source_service, request.source_user_id)
        log.info(
            f"[grpc] SubmitVoice von {request.source_service}:{request.source_user_id}"
            f" (roomie={roomie_id or 'anonym'}, {len(request.audio)} bytes)"
        )
        transcript, answer, intent_name, audio_ogg = self._handle_voice(request.audio, roomie_id)
        return pb.SubmitVoiceResponse(
            transcript=transcript,
            answer=answer,
            intent_name=intent_name,
            audio_ogg=audio_ogg,
        )

    def Announce(self, request, _context):
        try:
            self._announce(request.device, request.text)
            return pb.StatusResponse(ok=True, message="gesendet")
        except Exception as e:
            log.error(f"[grpc] Announce fehlgeschlagen: {e}")
            return pb.StatusResponse(ok=False, message=str(e))

    def GetSatellites(self, _request, _context):
        sats = self._get_satellites()
        result = [
            pb.Satellite(device_id=dev, room=info.get("room", ""), address=info.get("addr", ""))
            for dev, info in sats.items()
        ]
        return pb.GetSatellitesResponse(satellites=result)

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
                log.info("[grpc] Kein Proxy mehr verbunden — UDP-Server + Discovery werden wiederhergestellt")
                self._enable_udp()
                self._on_proxy_discovery(None, 0)  # None → Restore Hannah's own address
            if no_more and self._on_satellite_change:
                with self._proxy_sat_lock:
                    self._proxy_satellites.clear()
                threading.Thread(
                    target=self._on_satellite_change, args=({},), daemon=True
                ).start()
            log.info(f"[grpc] Proxy '{proxy_id}' getrennt")

    def SubmitSatelliteAudio(self, request, context):
        if self._handle_satellite_audio is None:
            context.set_code(grpc.StatusCode.UNIMPLEMENTED)
            context.set_details("handle_satellite_audio not configured")
            return pb.SubmitSatelliteAudioResponse()

        speaker = request.speaker_roomie_id or ""
        log.info(
            f"[grpc] SubmitSatelliteAudio: device={request.device_id!r}"
            f" room={request.room!r} bytes={len(request.audio_pcm)}"
            + (f" speaker={speaker!r}" if speaker else " speaker=anonymous")
        )
        if self._on_satellite_change and request.device_id:
            with self._proxy_sat_lock:
                known = self._proxy_satellites.get(request.device_id)
                if known != request.room:
                    self._proxy_satellites[request.device_id] = request.room
                    snapshot = dict(self._proxy_satellites)
            if known != request.room:
                threading.Thread(
                    target=self._on_satellite_change, args=(snapshot,), daemon=True
                ).start()
        transcript, answer, intent_name, tts_pcm, sample_rate = self._handle_satellite_audio(
            request.device_id,
            request.room,
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

    def NotifySatelliteRegistered(self, request, _context):
        """Proxy meldet: Satellit hat sich via UDP registriert."""
        device, room = request.device_id, request.room
        with self._proxy_sat_lock:
            self._proxy_satellites[device] = room
            snapshot = dict(self._proxy_satellites)
        log.info(f"[grpc] Satellit registriert via Proxy: '{device}' (Raum: '{room}')")
        if self._on_satellite_change:
            threading.Thread(
                target=self._on_satellite_change, args=(snapshot,), daemon=True
            ).start()
        return pb.StatusResponse(ok=True, message="registered")

    def NotifySatelliteGone(self, request, _context):
        """Proxy meldet: Satellit hat sich abgemeldet."""
        device = request.device_id
        with self._proxy_sat_lock:
            self._proxy_satellites.pop(device, None)
            snapshot = dict(self._proxy_satellites)
        log.info(f"[grpc] Satellit abgemeldet via Proxy: '{device}'")
        if self._on_satellite_change:
            threading.Thread(
                target=self._on_satellite_change, args=(snapshot,), daemon=True
            ).start()
        return pb.StatusResponse(ok=True, message="gone")

    def EnrollVoiceprint(self, request, _context):
        if self._enroll_voiceprint is None:
            return pb.StatusResponse(
                ok=False,
                message="Kein Voice-ID-Backend konfiguriert.",
            )
        log.info(
            f"[grpc] EnrollVoiceprint: roomie={request.roomie_id!r}"
            f" bytes={len(request.audio_pcm)} rate={request.sample_rate}"
        )
        ok, msg = self._enroll_voiceprint(
            request.roomie_id, request.audio_pcm, request.sample_rate
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

    def start(self):
        self._server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
        pb_grpc.add_HannahServiceServicer_to_server(self._servicer, self._server)
        addr = f"{self._host}:{self._port}"
        self._server.add_insecure_port(addr)
        self._server.start()
        log.info(f"gRPC-Server lauscht auf {addr}")

    def stop(self):
        if self._server:
            self._server.stop(grace=2)
            log.info("gRPC-Server beendet.")


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


def make_system_notification_event(text: str) -> pb.HannahEvent:
    return pb.HannahEvent(
        event_type="system.notification",
        timestamp=datetime.now(timezone.utc).isoformat(),
        system_notification=pb.SystemNotificationEvent(text=text),
    )


# ------------------------------------------------------------------
# Conversion helpers

def _user_to_pb(u: dict) -> pb.User:
    return pb.User(
        uuid=u.get("uuid", ""),
        roomie_id=u.get("roomie_id", ""),
        display_name=u.get("display_name", ""),
        trust_level=u.get("trust_level", 5),
        active=bool(u.get("active", True)),
        linked_accounts=u.get("linked_accounts") or {},
        system_messages=bool(u.get("system_messages", False)),
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
