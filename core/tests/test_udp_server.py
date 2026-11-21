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

        with server._lock:
            assert "roomless-sat" not in server._satellites
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
