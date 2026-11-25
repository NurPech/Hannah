import json
import time
from unittest.mock import MagicMock

from hannah.udp_server import UDPServer


def _make_server(callback=None, resolve_satellite_room=None, upsert_satellite=None):
    return UDPServer(
        cfg={"host": "127.0.0.1", "port": 0},
        on_audio=MagicMock(),
        on_satellite_change=callback or MagicMock(),
        resolve_satellite_room=resolve_satellite_room,
        upsert_satellite=upsert_satellite,
    )


def _register(server, device, addr=("192.168.1.50", 7776)):
    payload = json.dumps({"type": "register", "device": device}).encode("utf-8")
    server._handle_control(payload, addr)


def _heartbeat(server, device, addr=("192.168.1.50", 7776)):
    payload = json.dumps({"type": "heartbeat", "device": device}).encode("utf-8")
    server._handle_control(payload, addr)


class TestHeartbeatWatchdog:
    def test_stale_satellite_removed(self):
        server = _make_server()
        with server._lock:
            server._satellites["stale-sat"] = {
                "addr": ("192.168.1.100", 7776),
                "tts_addr": ("192.168.1.100", 7776),
                "room": "Wohnzimmer",
                "last_heartbeat": time.monotonic() - 31.0,
            }

        server._check_heartbeats()

        with server._lock:
            assert "stale-sat" not in server._satellites

    def test_stale_satellite_triggers_callback(self):
        callback = MagicMock()
        server = _make_server(callback)
        with server._lock:
            server._satellites["stale-sat"] = {
                "addr": ("192.168.1.100", 7776),
                "tts_addr": ("192.168.1.100", 7776),
                "room": "Wohnzimmer",
                "last_heartbeat": time.monotonic() - 31.0,
            }

        server._check_heartbeats()
        time.sleep(0.1)  # callback runs in daemon thread

        callback.assert_called_once()
        snapshot = callback.call_args[0][0]
        assert "stale-sat" not in snapshot

    def test_fresh_heartbeat_not_removed(self):
        callback = MagicMock()
        server = _make_server(callback)
        with server._lock:
            server._satellites["fresh-sat"] = {
                "addr": ("192.168.1.100", 7776),
                "tts_addr": ("192.168.1.100", 7776),
                "room": "Wohnzimmer",
                "last_heartbeat": time.monotonic(),
            }

        server._check_heartbeats()

        with server._lock:
            assert "fresh-sat" in server._satellites
        callback.assert_not_called()

    def test_partial_timeout_only_removes_stale(self):
        callback = MagicMock()
        server = _make_server(callback)
        with server._lock:
            server._satellites["stale-sat"] = {
                "addr": ("192.168.1.100", 7776),
                "tts_addr": ("192.168.1.100", 7776),
                "room": "Wohnzimmer",
                "last_heartbeat": time.monotonic() - 31.0,
            }
            server._satellites["fresh-sat"] = {
                "addr": ("192.168.1.101", 7776),
                "tts_addr": ("192.168.1.101", 7776),
                "room": "Küche",
                "last_heartbeat": time.monotonic(),
            }

        server._check_heartbeats()
        time.sleep(0.1)

        with server._lock:
            assert "stale-sat" not in server._satellites
            assert "fresh-sat" in server._satellites

        snapshot = callback.call_args[0][0]
        assert "stale-sat" not in snapshot
        assert "fresh-sat" in snapshot


class TestHeartbeatUpsertsLastSeen:
    def test_heartbeat_from_known_satellite_upserts(self):
        """Regression: heartbeats only refreshed the in-memory watchdog state, never
        RoomManager's last_seen — DB showed a satellite as never-updated forever,
        even while it kept heartbeating and staying connected."""
        upsert = MagicMock()
        server = _make_server(upsert_satellite=upsert)
        with server._lock:
            server._satellites["wz-sat"] = {
                "addr": ("192.168.1.100", 7776),
                "tts_addr": ("192.168.1.100", 7776),
                "room": "Wohnzimmer",
                "last_heartbeat": time.monotonic() - 5.0,
            }

        _heartbeat(server, "wz-sat")

        upsert.assert_called_once_with("wz-sat")

    def test_heartbeat_from_unknown_satellite_does_not_upsert(self):
        upsert = MagicMock()
        server = _make_server(upsert_satellite=upsert)

        _heartbeat(server, "ghost-sat")

        upsert.assert_not_called()


class TestRegisterRoomCheck:
    def test_register_without_room_not_tracked(self):
        callback = MagicMock()
        upsert = MagicMock()
        server = _make_server(callback, resolve_satellite_room=lambda _d: None, upsert_satellite=upsert)

        _register(server, "roomless-sat")

        # Verbunden (siehe TestRoomlessSatellite), aber dem Rest von Core nicht bekannt (#547)
        assert server.registered_devices() == {}
        assert server.registered_devices_full() == {}
        upsert.assert_called_once_with("roomless-sat")
        callback.assert_not_called()

    def test_register_with_room_tracked(self):
        callback = MagicMock()
        upsert = MagicMock()
        server = _make_server(callback, resolve_satellite_room=lambda _d: "wohnzimmer", upsert_satellite=upsert)

        _register(server, "wz-sat")
        time.sleep(0.1)  # callback runs in daemon thread

        with server._lock:
            assert server._satellites["wz-sat"]["room"] == "wohnzimmer"
        upsert.assert_called_once_with("wz-sat")
        callback.assert_called_once()
        snapshot = callback.call_args[0][0]
        assert snapshot.get("wz-sat") == "wohnzimmer"


class TestTtsAddrBehindNat:
    """#351 — hinter einer NAT ist die gesehene Adresse Gateway + NAT-Port; TTS muss
    dorthin, nicht an Gateway-IP + gemeldeten listen_port."""

    def test_register_uses_source_address_not_listen_port(self):
        server = _make_server(resolve_satellite_room=lambda _d: "wohnzimmer")
        nat_addr = ("172.21.0.1", 44000)
        payload = json.dumps({"type": "register", "device": "nb", "listen_port": 7776}).encode("utf-8")

        server._handle_control(payload, nat_addr)

        with server._lock:
            assert server._satellites["nb"]["tts_addr"] == nat_addr

    def test_heartbeat_follows_nat_remapping(self):
        server = _make_server(resolve_satellite_room=lambda _d: "wohnzimmer")
        _register(server, "nb", addr=("172.21.0.1", 44000))

        _heartbeat(server, "nb", addr=("172.21.0.1", 45123))

        with server._lock:
            assert server._satellites["nb"]["tts_addr"] == ("172.21.0.1", 45123)


class TestReportedLanIp:
    """#535 — hinter einer NAT zeigt der Adapter sonst das Gateway (172.x); der Satellit
    meldet seine eigene LAN-IP im Register. Audio/TTS bleiben auf der gesehenen Adresse."""

    @staticmethod
    def _register_with_ip(server, ip, addr=("172.21.0.1", 44000)):
        msg = {"type": "register", "device": "nb", "listen_port": 7776}
        if ip is not None:
            msg["ip"] = ip
        server._handle_control(json.dumps(msg).encode("utf-8"), addr)

    def test_reported_ip_is_shown_but_tts_stays_on_source(self):
        server = _make_server(resolve_satellite_room=lambda _d: "wohnzimmer")

        self._register_with_ip(server, "192.168.1.77")

        assert server.registered_devices_full()["nb"]["addr"] == "192.168.1.77:44000"
        with server._lock:
            assert server._satellites["nb"]["tts_addr"] == ("172.21.0.1", 44000)

    def test_without_reported_ip_the_observed_address_is_used(self):
        server = _make_server(resolve_satellite_room=lambda _d: "wohnzimmer")

        self._register_with_ip(server, None)

        assert server.registered_devices_full()["nb"]["addr"] == "172.21.0.1:44000"

    def test_unusable_reported_ip_is_ignored(self):
        server = _make_server(resolve_satellite_room=lambda _d: "wohnzimmer")

        for bad in ("8.8.8.8", "127.0.0.1", "0.0.0.0", "224.0.0.1", "not-an-ip", "::1", 5, ""):
            self._register_with_ip(server, bad)
            assert server.registered_devices_full()["nb"]["addr"] == "172.21.0.1:44000", bad

    def test_heartbeat_keeps_reported_ip(self):
        server = _make_server(resolve_satellite_room=lambda _d: "wohnzimmer")
        self._register_with_ip(server, "10.1.2.3")

        _heartbeat(server, "nb", addr=("172.21.0.1", 45123))

        assert server.registered_devices_full()["nb"]["addr"] == "10.1.2.3:45123"


class TestFindDeviceByAddr:
    """#351 — hinter derselben NAT teilen sich alle Satelliten die Gateway-IP."""

    def _server_with(self, sats: dict):
        server = _make_server()
        with server._lock:
            for device, addr in sats.items():
                server._satellites[device] = {
                    "addr": addr, "tts_addr": addr, "room": "wohnzimmer",
                    "last_heartbeat": time.monotonic(),
                }
        return server

    def test_distinguishes_satellites_behind_same_nat(self):
        server = self._server_with({"a": ("172.21.0.1", 44000), "b": ("172.21.0.1", 44001)})

        assert server._find_device_by_addr(("172.21.0.1", 44000)) == "a"
        assert server._find_device_by_addr(("172.21.0.1", 44001)) == "b"
        assert server._find_device_by_addr(("172.21.0.1", 45000)) is None

    def test_falls_back_to_unambiguous_ip(self):
        server = self._server_with({"a": ("192.168.1.100", 7776)})

        assert server._find_device_by_addr(("192.168.1.100", 50000)) == "a"


class TestRoomFollowsAssignment:
    """#519: the room of a registered satellite is the current DB assignment, not the one it
    had when it registered. A room change used to be picked up only after the next register."""

    def _server(self, rooms, callback=None):
        server = _make_server(callback, resolve_satellite_room=lambda d: rooms.get(d))
        _register(server, "wz-sat")
        return server

    def test_registered_devices_follow_a_room_change(self):
        rooms = {"wz-sat": "wohnzimmer"}
        server = self._server(rooms)

        rooms["wz-sat"] = "buero"

        assert server.registered_devices() == {"wz-sat": "buero"}
        assert server.registered_devices_full()["wz-sat"]["room"] == "buero"

    def test_a_removed_assignment_leaves_the_satellite_without_a_room(self):
        rooms = {"wz-sat": "wohnzimmer"}
        server = self._server(rooms)

        rooms["wz-sat"] = None

        assert server.registered_devices() == {"wz-sat": ""}

    def test_snapshot_after_a_heartbeat_timeout_carries_the_current_room_of_the_others(self):
        rooms = {"wz-sat": "wohnzimmer", "bad-sat": "bad"}
        callback = MagicMock()
        server = _make_server(callback, resolve_satellite_room=lambda d: rooms.get(d))
        _register(server, "wz-sat", addr=("192.168.1.50", 7776))
        _register(server, "bad-sat", addr=("192.168.1.51", 7776))
        rooms["wz-sat"] = "buero"
        with server._lock:
            server._satellites["bad-sat"]["last_heartbeat"] = time.monotonic() - 31.0
        time.sleep(0.1)  # register callbacks run in daemon threads
        callback.reset_mock()

        server._check_heartbeats()
        time.sleep(0.1)

        callback.assert_called_once_with({"wz-sat": "buero"})


class _FakeSock:
    """Records every datagram instead of sending it."""

    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((data, addr))


class _FakeClock:
    """Replaces the time module of udp_server: sleeping moves the clock, nothing really waits."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _server_with_sock(monkeypatch):
    from hannah import udp_server

    clock = _FakeClock()
    monkeypatch.setattr(udp_server, "time", clock)
    server = _make_server(resolve_satellite_room=lambda d: "wohnzimmer")
    server._sock = _FakeSock()
    return server, clock


def _tts_packets(sock):
    return [d for d, _ in sock.sent if d[0] == 0x03]


def _control_packets(sock):
    return [json.loads(d[1:]) for d, _ in sock.sent if d[0] == 0x01]


class TestTtsStreaming:
    """TTS goes out in small packets paced to the playback speed, like the Proxy does (#545)."""

    def test_packets_stay_below_the_wifi_mtu_and_keep_the_audio_in_order(self, monkeypatch):
        server, _ = _server_with_sock(monkeypatch)
        pcm = bytes(range(256)) * 470  # 120,320 bytes, like the long answer of the bug report

        server._stream_tts("wz-sat", pcm, 16000, ("192.168.1.50", 7776))

        packets = _tts_packets(server._sock)
        assert all(len(p) - 1 <= 1400 for p in packets)
        assert b"".join(p[1:] for p in packets) == pcm

    def test_tts_end_comes_last_with_the_sample_rate(self, monkeypatch):
        server, _ = _server_with_sock(monkeypatch)

        server._stream_tts("wz-sat", bytes(5000), 16000, ("192.168.1.50", 7776))

        assert server._sock.sent[-1][0][0] == 0x01
        assert _control_packets(server._sock)[-1] == {"type": "tts_end", "sample_rate": 16000}

    def test_all_packets_go_to_the_given_address(self, monkeypatch):
        server, _ = _server_with_sock(monkeypatch)

        server._stream_tts("wz-sat", bytes(5000), 16000, ("192.168.1.50", 7776))

        assert {addr for _, addr in server._sock.sent} == {("192.168.1.50", 7776)}

    def test_sending_is_paced_to_the_playback_speed(self, monkeypatch):
        server, clock = _server_with_sock(monkeypatch)
        seconds = 4
        pcm = bytes(16000 * 2 * seconds)

        server._stream_tts("wz-sat", pcm, 16000, ("192.168.1.50", 7776))

        # Not as a burst: sending takes as long as the audio, minus the lead
        assert seconds - 0.4 <= clock.now <= seconds

    def test_no_audio_still_sends_tts_end(self, monkeypatch):
        server, _ = _server_with_sock(monkeypatch)

        server._stream_tts("wz-sat", b"", 16000, ("192.168.1.50", 7776))

        assert _tts_packets(server._sock) == []
        assert _control_packets(server._sock) == [{"type": "tts_end", "sample_rate": 16000}]

    def test_a_socket_error_stops_the_transfer_without_tts_end(self, monkeypatch):
        server, _ = _server_with_sock(monkeypatch)

        def broken(data, addr):
            raise OSError("network unreachable")

        server._sock.sendto = broken

        server._stream_tts("wz-sat", bytes(5000), 16000, ("192.168.1.50", 7776))  # must not raise

    def test_send_tts_for_an_unknown_satellite_does_nothing(self, monkeypatch):
        server, _ = _server_with_sock(monkeypatch)

        server.send_tts("nobody", bytes(5000))

        assert server._sock.sent == []

    def test_send_tts_streams_to_the_observed_address_of_a_registered_satellite(self):
        server = _make_server(resolve_satellite_room=lambda d: "wohnzimmer")
        server._sock = _FakeSock()
        _register(server, "wz-sat", addr=("172.18.0.1", 41234))
        server._sock.sent.clear()  # drop the "registered" answer

        server.send_tts("wz-sat", bytes(2800))
        for _ in range(100):  # runs in its own thread
            if any(c.get("type") == "tts_end" for c in _control_packets(server._sock)):
                break
            time.sleep(0.02)

        assert {addr for _, addr in server._sock.sent} == {("172.18.0.1", 41234)}
        assert len(_tts_packets(server._sock)) == 2


class TestTtsSampleRate:
    """The firmware always plays 16 kHz and ignores the rate in tts_end, so Core converts (#546)."""

    def test_piper_audio_leaves_core_with_16_khz(self, monkeypatch):
        server, _ = _server_with_sock(monkeypatch)
        one_second_22k = bytes(22050 * 2)

        server._stream_tts("wz-sat", one_second_22k, 22050, ("192.168.1.50", 7776))

        sent = sum(len(p) - 1 for p in _tts_packets(server._sock))
        assert abs(sent - 16000 * 2) < 16000 * 2 * 0.02  # one second at 16 kHz, 16 bit
        assert _control_packets(server._sock)[-1] == {"type": "tts_end", "sample_rate": 16000}

    def test_audio_at_16_khz_is_sent_unchanged(self, monkeypatch):
        server, _ = _server_with_sock(monkeypatch)
        pcm = bytes(range(256)) * 20

        server._stream_tts("wz-sat", pcm, 16000, ("192.168.1.50", 7776))

        assert b"".join(p[1:] for p in _tts_packets(server._sock)) == pcm

    def test_pacing_follows_the_converted_duration(self, monkeypatch):
        server, clock = _server_with_sock(monkeypatch)
        three_seconds_22k = bytes(22050 * 2 * 3)

        server._stream_tts("wz-sat", three_seconds_22k, 22050, ("192.168.1.50", 7776))

        assert 3 - 0.4 <= clock.now <= 3.05


def _server_with_fake_sock(rooms=None, callback=None, pair=None, is_paired=None):
    rooms = {} if rooms is None else rooms
    server = _make_server(callback, resolve_satellite_room=lambda d: rooms.get(d))
    server._pair_satellite = pair or (lambda *_: False)
    server._satellite_is_paired = is_paired or (lambda *_: False)
    server._sock = _FakeSock()
    return server, rooms


def _register_seed(server, device, seed, addr=("192.168.1.50", 7776)):
    payload = json.dumps({"type": "register", "device": device, "seed": seed}).encode("utf-8")
    server._handle_control(payload, addr)


class TestRoomlessSatellite:
    """A satellite without a room stays connected like behind the Proxy, but Core does not track it (#547)."""

    def test_register_is_acknowledged(self):
        server, _ = _server_with_fake_sock()

        _register(server, "new-sat")

        assert {"type": "registered", "ok": True} in _control_packets(server._sock)

    def test_heartbeat_is_acknowledged_and_never_answered_with_reregister(self):
        server, _ = _server_with_fake_sock()
        _register(server, "new-sat")
        server._sock.sent.clear()

        _heartbeat(server, "new-sat")

        types = [c["type"] for c in _control_packets(server._sock)]
        assert types == ["heartbeat_ack"]

    def test_it_is_not_listed_and_not_forwarded_while_it_has_no_room(self):
        callback = MagicMock()
        server, _ = _server_with_fake_sock(callback=callback)

        _register(server, "new-sat")
        _heartbeat(server, "new-sat")
        time.sleep(0.1)

        assert server.registered_devices() == {}
        assert server.registered_devices_full() == {}
        callback.assert_not_called()

    def test_a_room_assigned_later_is_picked_up_with_the_next_heartbeat(self):
        callback = MagicMock()
        server, rooms = _server_with_fake_sock(callback=callback)
        _register(server, "new-sat")

        rooms["new-sat"] = "wohnzimmer"
        _heartbeat(server, "new-sat")
        time.sleep(0.1)  # callback runs in a daemon thread

        assert server.registered_devices() == {"new-sat": "wohnzimmer"}
        callback.assert_called_once_with({"new-sat": "wohnzimmer"})

    def test_picking_it_up_happens_only_once(self):
        callback = MagicMock()
        server, rooms = _server_with_fake_sock(callback=callback)
        _register(server, "new-sat")
        rooms["new-sat"] = "wohnzimmer"

        _heartbeat(server, "new-sat")
        _heartbeat(server, "new-sat")
        time.sleep(0.1)

        callback.assert_called_once()

    def test_a_timeout_of_a_roomless_satellite_reports_nothing(self):
        callback = MagicMock()
        server, _ = _server_with_fake_sock(callback=callback)
        _register(server, "new-sat")
        with server._lock:
            server._satellites["new-sat"]["last_heartbeat"] = time.monotonic() - 31.0

        server._check_heartbeats()
        time.sleep(0.1)

        with server._lock:
            assert "new-sat" not in server._satellites
        callback.assert_not_called()

    def test_a_satellite_that_lost_its_room_stays_listed_without_one(self):
        # #519: a removed assignment shows as "" for a satellite that was tracked before
        server, rooms = _server_with_fake_sock(rooms={"wz-sat": "wohnzimmer"})
        _register(server, "wz-sat")

        rooms["wz-sat"] = None

        assert server.registered_devices() == {"wz-sat": ""}

    def test_audio_of_a_roomless_satellite_is_accepted(self):
        server, _ = _server_with_fake_sock()
        _register(server, "new-sat")
        server._sock.sent.clear()

        server._handle_audio(bytes(320), ("192.168.1.50", 7776))

        assert [c for c in _control_packets(server._sock) if c["type"] == "reregister"] == []
        with server._lock:
            assert "new-sat" in server._sessions


class TestPairingOverDirectUdp:
    """The seed in the register message is redeemed like through the Proxy (#547)."""

    def test_the_seed_is_redeemed_and_paired_is_sent(self):
        pair = MagicMock(return_value=True)
        server, _ = _server_with_fake_sock(rooms={"new-sat": "wohnzimmer"}, pair=pair)

        _register_seed(server, "new-sat", "seed-123")

        pair.assert_called_once_with("new-sat", "seed-123")
        assert {"type": "paired", "device": "new-sat"} in _control_packets(server._sock)

    def test_pairing_happens_before_the_room_is_read(self):
        order = []
        server = _make_server(resolve_satellite_room=lambda d: order.append("room") or "wohnzimmer")
        server._pair_satellite = lambda d, s: order.append("pair") or True
        server._sock = _FakeSock()

        _register_seed(server, "new-sat", "seed-123")

        assert order[:2] == ["pair", "room"]  # later entries come from the callback thread

    def test_an_already_used_seed_still_gets_the_confirmation(self):
        # The "paired" packet got lost the first time, the satellite still sends the seed
        pair = MagicMock(return_value=False)
        server, _ = _server_with_fake_sock(pair=pair, is_paired=lambda d: True)

        _register_seed(server, "new-sat", "seed-123")

        assert {"type": "paired", "device": "new-sat"} in _control_packets(server._sock)

    def test_an_unknown_seed_gets_no_paired(self):
        server, _ = _server_with_fake_sock(pair=lambda d, s: False, is_paired=lambda d: False)

        _register_seed(server, "new-sat", "seed-123")

        assert [c for c in _control_packets(server._sock) if c["type"] == "paired"] == []
        assert {"type": "registered", "ok": True} in _control_packets(server._sock)

    def test_no_seed_means_no_pairing(self):
        pair = MagicMock(return_value=True)
        server, _ = _server_with_fake_sock(pair=pair)

        _register(server, "new-sat")

        pair.assert_not_called()

    def test_a_database_error_does_not_stop_the_registration(self):
        def broken(device, seed):
            raise RuntimeError("database is locked")

        server, _ = _server_with_fake_sock(rooms={"new-sat": "wohnzimmer"}, pair=broken)

        _register_seed(server, "new-sat", "seed-123")  # must not raise

        assert {"type": "registered", "ok": True} in _control_packets(server._sock)
        assert [c for c in _control_packets(server._sock) if c["type"] == "paired"] == []
