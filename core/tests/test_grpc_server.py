import json
import os
import queue
import sqlite3
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from werkzeug.security import generate_password_hash

from hannah.grpc_server import HannahServicer, _user_to_pb
from hannah.user_manager import UserManager
from hannah.models.user import User
from hannah.residents.Roomie import Roomie
from hannah.iobroker import IoBrokerClient
from hannah_proto.hannah_pb2 import AgentDevice, AgentStateValue, AgentResident, AgentRoom, SatelliteRegistration, ResidentType, LinkAccountRequest, ProxyHeartbeat, CreateGroupRequest, UpdateGroupRequest, DeleteGroupRequest, SetGroupRoomsRequest, SetSatelliteRoomRequest, SetSatelliteDisplayNameRequest, SetSatelliteOwnerRequest, SetSatelliteSmalltalkFollowupRequest, DeleteSatelliteRequest, AnnounceRequest, LoginRequest, CreateTriggerRequest, UpdateTriggerRequest, DeleteTriggerRequest, CreateAlarmRequest, UpdateAlarmRequest, DeleteAlarmRequest, UpdateConfigRequest, SettingUpdate, CreateBleTagRequest, UpdateBleTagRequest, DeleteBleTagRequest, CreateCarRequest, UpdateCarRequest, DeleteCarRequest, CreateUserRequest, UpdateUserRequest, DeleteUserRequest, GetTimersRequest, DeleteTimerRequest, TimerInfo, TimerListResponse, EnumValues, StateType
from hannah.satellite_manager import SatellitePermissionError

def _make_server(user_manager=None,satellite_manager=None,handle_text=None,handle_voice=None,get_satellites=None,get_car_state=None,announce=None,notificate=None,on_agent_device_snapshot=None,on_agent_send_residents=None,on_agent_room_snapshot=None,on_satellite_change=None,resolve_satellite_room=None,upsert_satellite=None,get_rooms=None,get_groups=None,create_group=None,update_group=None,delete_group=None,set_group_rooms=None,get_db_satellites=None,set_satellite_room=None,set_satellite_display_name=None,set_satellite_owner=None,get_trigger_records=None,create_trigger=None,update_trigger=None,delete_trigger=None,get_alarm_records=None,create_alarm=None,update_alarm=None,delete_alarm=None,get_categories=None,get_settings_records=None,update_setting_value=None,get_ble_tag_records=None,create_ble_tag=None,update_ble_tag=None,delete_ble_tag=None,get_car_records=None,create_car=None,update_car=None,delete_car=None,get_residents=None,get_devices=None):
    return HannahServicer(
        user_manager=user_manager or MagicMock(),
        satellite_manager=satellite_manager or MagicMock(),
        handle_text=handle_text or MagicMock(),
        handle_voice=handle_voice or MagicMock(),
        announce=announce or MagicMock(),
        notificate=notificate or MagicMock(),
        get_satellites=get_satellites or MagicMock(),
        get_car_state=get_car_state or MagicMock(),
        get_devices=get_devices,
        on_agent_device_snapshot=on_agent_device_snapshot,
        on_agent_send_residents = on_agent_send_residents,
        on_agent_room_snapshot=on_agent_room_snapshot,
        on_satellite_change=on_satellite_change,
        resolve_satellite_room=resolve_satellite_room,
        upsert_satellite=upsert_satellite,
        get_rooms=get_rooms,
        get_groups=get_groups,
        create_group=create_group,
        update_group=update_group,
        delete_group=delete_group,
        set_group_rooms=set_group_rooms,
        get_db_satellites=get_db_satellites,
        set_satellite_room=set_satellite_room,
        set_satellite_display_name=set_satellite_display_name,
        set_satellite_owner=set_satellite_owner,
        get_trigger_records=get_trigger_records,
        create_trigger=create_trigger,
        update_trigger=update_trigger,
        delete_trigger=delete_trigger,
        get_alarm_records=get_alarm_records,
        create_alarm=create_alarm,
        update_alarm=update_alarm,
        delete_alarm=delete_alarm,
        get_categories=get_categories,
        get_settings_records=get_settings_records,
        update_setting_value=update_setting_value,
        get_ble_tag_records=get_ble_tag_records,
        create_ble_tag=create_ble_tag,
        update_ble_tag=update_ble_tag,
        delete_ble_tag=delete_ble_tag,
        get_car_records=get_car_records,
        create_car=create_car,
        update_car=update_car,
        delete_car=delete_car,
        get_residents=get_residents,
    )

def test_device_snapshot_dispatched():
    client = IoBrokerClient({"host": "localhost", "port": 8093})
    servicer = _make_server(on_agent_device_snapshot=client.handle_device_snapshot)

    devices = [
        AgentDevice(
            state_id="javascript.0.virtualDevice.Licht.EG.Wohnzimmer.Decke.on",
            room="wohnzimmer",
            device="Decke",
            functions=["Licht"],
            value=AgentStateValue(value="true", ack=True),
            room_names={"de": "Wohnzimmer", "en": "Living Room"},
        )
    ]
    servicer._on_agent_device_snapshot(devices)
    assert "wohnzimmer" in client.rooms

def test_get_devices_includes_state_types_and_enum_values():
    """#117 — GetDevices reicht state_type/enum_values aus dem IoBrokerClient-Cache
    unverändert bis in die DeviceInfo-Response durch."""
    client = IoBrokerClient({"host": "localhost", "port": 8093})
    servicer = _make_server(get_devices=client.get_devices_snapshot)

    devices = [
        AgentDevice(
            state_id="javascript.0.virtualDevice.Licht.EG.Wohnzimmer.Decke.on",
            room="wohnzimmer",
            device="Decke",
            device_type="light",
            value=AgentStateValue(value="true", ack=True),
            room_names={"de": "Wohnzimmer"},
            state_type=StateType.BOOLEAN,
        ),
        AgentDevice(
            state_id="javascript.0.virtualDevice.Licht.EG.Wohnzimmer.Decke.mode",
            room="wohnzimmer",
            device="Decke",
            device_type="light",
            value=AgentStateValue(value="1", ack=True),
            room_names={"de": "Wohnzimmer"},
            state_type=StateType.ENUM,
            enum_values=EnumValues(values={"0": "Aus", "1": "An"}),
        ),
    ]
    client.handle_device_snapshot(devices)

    response = servicer.GetDevices(None, None)

    [room] = response.rooms
    [device] = room.devices
    assert device.state_types["on"] == StateType.BOOLEAN
    assert device.state_types["mode"] == StateType.ENUM
    assert dict(device.state_enum_values["mode"].values) == {"0": "Aus", "1": "An"}
    assert "on" not in device.state_enum_values

def test_get_devices_includes_state_writable():
    """#144 — GetDevices reicht writable aus dem IoBrokerClient-Cache unverändert
    bis in die DeviceInfo-Response durch."""
    client = IoBrokerClient({"host": "localhost", "port": 8093})
    servicer = _make_server(get_devices=client.get_devices_snapshot)

    devices = [
        AgentDevice(
            state_id="javascript.0.virtualDevice.Licht.EG.Wohnzimmer.Decke.on",
            room="wohnzimmer",
            device="Decke",
            device_type="light",
            value=AgentStateValue(value="true", ack=True),
            room_names={"de": "Wohnzimmer"},
            writable=True,
        ),
        AgentDevice(
            state_id="javascript.0.virtualDevice.Licht.EG.Wohnzimmer.Decke.power",
            room="wohnzimmer",
            device="Decke",
            device_type="light",
            value=AgentStateValue(value="4.2", ack=True),
            room_names={"de": "Wohnzimmer"},
            writable=False,
        ),
    ]
    client.handle_device_snapshot(devices)

    response = servicer.GetDevices(None, None)

    [room] = response.rooms
    [device] = room.devices
    assert dict(device.state_writable) == {"on": True, "power": False}

def test_room_snapshot_dispatched():
    sync_rooms = MagicMock()
    servicer = _make_server(on_agent_room_snapshot=sync_rooms)
    rooms = [
        AgentRoom(room_id="wohnzimmer", display_names={"de": "Wohnzimmer", "en": "Living Room"}),
    ]
    servicer._on_agent_room_snapshot(rooms)
    sync_rooms.assert_called_once_with(rooms)

def test_notify_satellite_registered_does_not_double_send():
    on_satellite_change = MagicMock()
    servicer = _make_server(on_satellite_change=on_satellite_change, resolve_satellite_room=lambda _d: "wohnzimmer")
    servicer.agent_satellite_update = MagicMock()

    request = SatelliteRegistration(device_id="wz-sat", address="192.168.1.50")
    response = servicer.NotifySatelliteRegistered(request, None)

    assert response.ok
    servicer.agent_satellite_update.assert_not_called()
    on_satellite_change.assert_called_once_with({"wz-sat": "wohnzimmer"})

def test_notify_satellite_registered_upserts_last_seen():
    """Regression: NotifySatelliteRegistered never refreshed RoomManager's last_seen,
    unlike the UDP "register" path — satellites connected via the proxy showed a
    last_seen frozen at whatever it last was before they switched to proxy routing."""
    upsert = MagicMock()
    servicer = _make_server(resolve_satellite_room=lambda _d: "wohnzimmer", upsert_satellite=upsert)

    servicer.NotifySatelliteRegistered(SatelliteRegistration(device_id="wz-sat", address="192.168.1.50"), None)

    upsert.assert_called_once_with("wz-sat")

def test_register_proxy_heartbeat_upserts_known_satellites():
    """Regression: the proxy never forwards individual satellite heartbeats, only one
    heartbeat per proxy connection — without touching last_seen here too, it would
    freeze again right after the registration-time upsert for the whole proxy session."""
    upsert = MagicMock()
    servicer = _make_server(resolve_satellite_room=lambda _d: "wohnzimmer", upsert_satellite=upsert)
    servicer.NotifySatelliteRegistered(SatelliteRegistration(device_id="wz-sat", address="192.168.1.50"), None)
    upsert.reset_mock()

    class _FakeContext:
        def __init__(self):
            self._active = True
        def is_active(self):
            active = self._active
            self._active = False
            return active

    list(servicer.RegisterProxy(iter([ProxyHeartbeat(proxy_id="proxy-1")]), _FakeContext()))

    upsert.assert_called_once_with("wz-sat")

def test_register_proxy_heartbeat_pushes_last_seen_to_adapter():
    """#185: last_seen in ioBroker froze after the initial registration because only
    NotifySatelliteRegistered/Gone pushed agent_satellite_update, never the recurring
    proxy heartbeat — GetSatellites' last_seen kept advancing in Core's DB, but nothing
    told the adapter. The heartbeat loop must now push a live keep-alive too."""
    servicer = _make_server(
        resolve_satellite_room=lambda _d: "wohnzimmer",
        get_db_satellites=lambda: [{"device_id": "wz-sat", "display_name": "Wohnzimmer01"}],
    )
    servicer.NotifySatelliteRegistered(SatelliteRegistration(device_id="wz-sat", address="192.168.1.50"), None)
    servicer.agent_satellite_update = MagicMock()

    class _FakeContext:
        def __init__(self):
            self._active = True
        def is_active(self):
            active = self._active
            self._active = False
            return active

    list(servicer.RegisterProxy(iter([ProxyHeartbeat(proxy_id="proxy-1")]), _FakeContext()))

    servicer.agent_satellite_update.assert_called_once_with(
        "wz-sat", "wohnzimmer", "192.168.1.50", True, display_name="Wohnzimmer01",
    )

class TestRoomsGroupsRpcs:
    """#27 Phase 1 — reine Verdrahtung, RoomManager selbst ist in test_room_manager.py
    abgedeckt. Hier nur: ruft die RPC den richtigen Callback auf und baut die richtige
    Response-Shape."""

    def test_get_rooms(self):
        get_rooms = MagicMock(return_value=[{"room_id": "wohnzimmer", "display_name": "Wohnzimmer"}])
        servicer = _make_server(get_rooms=get_rooms)

        response = servicer.GetRooms(None, None)

        get_rooms.assert_called_once_with()
        assert len(response.rooms) == 1
        assert response.rooms[0].room_id == "wohnzimmer"
        assert response.rooms[0].display_name == "Wohnzimmer"

    def test_get_groups(self):
        get_groups = MagicMock(return_value=[{
            "group_id": "og", "display_name": "Obergeschoss",
            "rooms": [{"room_id": "bad", "display_name": "Bad"}],
        }])
        servicer = _make_server(get_groups=get_groups)

        response = servicer.GetGroups(None, None)

        assert len(response.groups) == 1
        group = response.groups[0]
        assert group.group_id == "og"
        assert group.display_name == "Obergeschoss"
        assert len(group.rooms) == 1
        assert group.rooms[0].room_id == "bad"

    def test_create_group_ok(self):
        create_group = MagicMock(return_value=True)
        servicer = _make_server(create_group=create_group)

        response = servicer.CreateGroup(CreateGroupRequest(group_id="og", display_name="Obergeschoss"), None)

        create_group.assert_called_once_with("og", "Obergeschoss")
        assert response.ok is True

    def test_create_group_duplicate(self):
        servicer = _make_server(create_group=MagicMock(return_value=False))

        response = servicer.CreateGroup(CreateGroupRequest(group_id="og", display_name="Obergeschoss"), None)

        assert response.ok is False

    def test_update_group(self):
        update_group = MagicMock(return_value=True)
        servicer = _make_server(update_group=update_group)

        response = servicer.UpdateGroup(UpdateGroupRequest(group_id="og", display_name="OG neu"), None)

        update_group.assert_called_once_with("og", "OG neu")
        assert response.ok is True

    def test_delete_group(self):
        delete_group = MagicMock(return_value=True)
        servicer = _make_server(delete_group=delete_group)

        response = servicer.DeleteGroup(DeleteGroupRequest(group_id="og"), None)

        delete_group.assert_called_once_with("og")
        assert response.ok is True

    def test_set_group_rooms(self):
        set_group_rooms = MagicMock()
        servicer = _make_server(set_group_rooms=set_group_rooms)

        response = servicer.SetGroupRooms(SetGroupRoomsRequest(group_id="og", room_ids=["bad", "schlafzimmer"]), None)

        set_group_rooms.assert_called_once_with("og", ["bad", "schlafzimmer"])
        assert response.ok is True

class TestSatelliteRpcs:
    """#27 Phase 2 — GetSatellites merged DB-Status (RoomManager.get_satellites) mit
    Live-Status (get_satellites-Closure aus udp_server/Proxy); SetSatelliteRoom/
    SetSatelliteDisplayName sind reine Verdrahtung auf RoomManager."""

    def test_get_satellites_connected_no_mismatch(self):
        servicer = _make_server(
            get_satellites=lambda: {"wz-sat": {"room": "wohnzimmer", "addr": "10.0.0.5:7775"}},
            get_db_satellites=lambda: [{"device_id": "wz-sat", "display_name": "Wohnzimmer-Sat", "room_id": "wohnzimmer", "room_display_name": "Wohnzimmer", "last_seen": "2026-06-27 10:00:00", "owner_user_id": 3, "owner_display_name": "Leonie", "smalltalk_followup_listen": True}],
        )

        response = servicer.GetSatellites(None, None)

        assert len(response.satellites) == 1
        sat = response.satellites[0]
        assert sat.device_id == "wz-sat"
        assert sat.connected is True
        assert sat.room == "wohnzimmer"
        assert sat.address == "10.0.0.5:7775"
        assert sat.display_name == "Wohnzimmer-Sat"
        assert sat.room_id == "wohnzimmer"
        assert sat.room_display_name == "Wohnzimmer"
        assert sat.last_seen == "2026-06-27 10:00:00"
        assert sat.room_mismatch is False
        assert sat.owner_user_id == 3
        assert sat.owner_display_name == "Leonie"
        assert sat.smalltalk_followup_listen is True

    def test_get_satellites_no_owner_defaults(self):
        servicer = _make_server(
            get_satellites=lambda: {},
            get_db_satellites=lambda: [{"device_id": "wz-sat", "display_name": "", "room_id": "", "room_display_name": "", "last_seen": "", "owner_user_id": None, "owner_display_name": None}],
        )

        response = servicer.GetSatellites(None, None)

        sat = response.satellites[0]
        assert sat.owner_user_id == 0
        assert sat.owner_display_name == ""

    def test_get_satellites_connected_with_mismatch(self):
        servicer = _make_server(
            get_satellites=lambda: {"wz-sat": {"room": "kueche", "addr": "10.0.0.5:7775"}},
            get_db_satellites=lambda: [{"device_id": "wz-sat", "display_name": "", "room_id": "wohnzimmer", "room_display_name": "Wohnzimmer", "last_seen": None}],
        )

        response = servicer.GetSatellites(None, None)

        assert response.satellites[0].room_mismatch is True

    def test_get_satellites_disconnected_db_only(self):
        servicer = _make_server(
            get_satellites=lambda: {},
            get_db_satellites=lambda: [{"device_id": "bad-sat", "display_name": "Bad-Sat", "room_id": "bad", "room_display_name": "Bad", "last_seen": "2026-06-20 08:00:00"}],
        )

        response = servicer.GetSatellites(None, None)

        assert len(response.satellites) == 1
        sat = response.satellites[0]
        assert sat.connected is False
        assert sat.room == ""
        assert sat.address == ""
        assert sat.room_id == "bad"
        assert sat.room_mismatch is False

    def test_get_satellites_connected_but_unknown_to_db_gets_upserted(self):
        upsert = MagicMock()
        servicer = _make_server(
            get_satellites=lambda: {"new-sat": {"room": "flur", "addr": "10.0.0.9:7775"}},
            get_db_satellites=lambda: [],
            upsert_satellite=upsert,
        )

        response = servicer.GetSatellites(None, None)

        upsert.assert_called_once_with("new-sat")
        assert len(response.satellites) == 1
        sat = response.satellites[0]
        assert sat.connected is True
        assert sat.room_mismatch is True
        assert sat.device_id == "new-sat"

    def test_set_satellite_room_ok(self):
        set_satellite_room = MagicMock(return_value=True)
        upsert = MagicMock()
        servicer = _make_server(
            set_satellite_room=set_satellite_room, upsert_satellite=upsert,
            get_satellites=lambda: {"wz-sat": {"room": "wohnzimmer", "addr": "10.0.0.5:7775"}},
        )

        response = servicer.SetSatelliteRoom(SetSatelliteRoomRequest(device_id="wz-sat", room_id="wohnzimmer", requestor_id=1), None)

        upsert.assert_called_once_with("wz-sat")
        set_satellite_room.assert_called_once_with("wz-sat", "wohnzimmer", requestor_id=1)
        assert response.ok is True

    def test_set_satellite_room_pushes_agent_update_when_connected(self):
        """#109: eine Raumänderung muss sofort an verbundene Adapter gepusht werden,
        nicht erst beim nächsten Connect/Disconnect-Event des Satelliten."""
        set_satellite_room = MagicMock(return_value=True)
        servicer = _make_server(
            set_satellite_room=set_satellite_room,
            get_satellites=lambda: {"wz-sat": {"room": "wohnzimmer", "addr": "10.0.0.5:7775"}},
        )
        servicer.agent_satellite_update = MagicMock()

        servicer.SetSatelliteRoom(SetSatelliteRoomRequest(device_id="wz-sat", room_id="wohnzimmer", requestor_id=1), None)

        servicer.agent_satellite_update.assert_called_once_with("wz-sat", "wohnzimmer", "10.0.0.5:7775", True)

    def test_set_satellite_room_pushes_agent_update_when_disconnected(self):
        """Der betroffene Satellit muss nicht live verbunden sein, damit der Adapter
        die Raumänderung mitbekommt (#109 Schritt 2)."""
        set_satellite_room = MagicMock(return_value=True)
        servicer = _make_server(set_satellite_room=set_satellite_room, get_satellites=lambda: {})
        servicer.agent_satellite_update = MagicMock()

        servicer.SetSatelliteRoom(SetSatelliteRoomRequest(device_id="wz-sat", room_id="wohnzimmer", requestor_id=1), None)

        servicer.agent_satellite_update.assert_called_once_with("wz-sat", "wohnzimmer", "", False)

    def test_set_satellite_room_unassign(self):
        set_satellite_room = MagicMock(return_value=True)
        servicer = _make_server(set_satellite_room=set_satellite_room, get_satellites=lambda: {})

        servicer.SetSatelliteRoom(SetSatelliteRoomRequest(device_id="wz-sat", room_id="", requestor_id=1), None)

        set_satellite_room.assert_called_once_with("wz-sat", None, requestor_id=1)

    def test_set_satellite_room_not_found(self):
        servicer = _make_server(set_satellite_room=MagicMock(return_value=False))

        response = servicer.SetSatelliteRoom(SetSatelliteRoomRequest(device_id="unknown", room_id="bad", requestor_id=1), None)

        assert response.ok is False

    def test_set_satellite_room_forbidden(self):
        """Permission-Logik selbst lebt in SatelliteManager (siehe test_satellite_manager.py)
        — hier wird nur geprüft, dass die RPC eine SatellitePermissionError sauber in
        ok=False/"forbidden" übersetzt."""
        set_satellite_room = MagicMock(side_effect=SatellitePermissionError("nope"))
        servicer = _make_server(set_satellite_room=set_satellite_room)

        response = servicer.SetSatelliteRoom(SetSatelliteRoomRequest(device_id="wz-sat", room_id="wohnzimmer", requestor_id=1), None)

        assert response.ok is False
        assert response.message == "forbidden"

    def test_set_satellite_display_name_ok(self):
        set_satellite_display_name = MagicMock(return_value=True)
        servicer = _make_server(set_satellite_display_name=set_satellite_display_name)

        response = servicer.SetSatelliteDisplayName(SetSatelliteDisplayNameRequest(device_id="wz-sat", display_name="Wohnzimmer-Sat", requestor_id=1), None)

        set_satellite_display_name.assert_called_once_with("wz-sat", "Wohnzimmer-Sat", requestor_id=1)
        assert response.ok is True

    def test_set_satellite_display_name_rejects_empty(self):
        set_satellite_display_name = MagicMock()
        servicer = _make_server(set_satellite_display_name=set_satellite_display_name)

        response = servicer.SetSatelliteDisplayName(SetSatelliteDisplayNameRequest(device_id="wz-sat", display_name="", requestor_id=1), None)

        set_satellite_display_name.assert_not_called()

    def test_set_satellite_display_name_forbidden(self):
        set_satellite_display_name = MagicMock(side_effect=SatellitePermissionError("nope"))
        servicer = _make_server(set_satellite_display_name=set_satellite_display_name)

        response = servicer.SetSatelliteDisplayName(SetSatelliteDisplayNameRequest(device_id="wz-sat", display_name="Neu", requestor_id=1), None)

        assert response.ok is False
        assert response.message == "forbidden"

    def test_set_satellite_owner_ok(self):
        set_satellite_owner = MagicMock(return_value=True)
        upsert = MagicMock()
        servicer = _make_server(set_satellite_owner=set_satellite_owner, upsert_satellite=upsert)

        response = servicer.SetSatelliteOwner(SetSatelliteOwnerRequest(device_id="wz-sat", user_id=3, requestor_id=1), None)

        upsert.assert_called_once_with("wz-sat")
        set_satellite_owner.assert_called_once_with("wz-sat", 3, requestor_id=1)
        assert response.ok is True

    def test_set_satellite_owner_unassign(self):
        set_satellite_owner = MagicMock(return_value=True)
        servicer = _make_server(set_satellite_owner=set_satellite_owner)

        servicer.SetSatelliteOwner(SetSatelliteOwnerRequest(device_id="wz-sat", user_id=0, requestor_id=1), None)

        set_satellite_owner.assert_called_once_with("wz-sat", None, requestor_id=1)

    def test_set_satellite_owner_not_found(self):
        servicer = _make_server(set_satellite_owner=MagicMock(return_value=False))

        response = servicer.SetSatelliteOwner(SetSatelliteOwnerRequest(device_id="unknown", user_id=3, requestor_id=1), None)

        assert response.ok is False

    def test_set_satellite_owner_forbidden(self):
        set_satellite_owner = MagicMock(side_effect=SatellitePermissionError("nope"))
        servicer = _make_server(set_satellite_owner=set_satellite_owner)

        response = servicer.SetSatelliteOwner(SetSatelliteOwnerRequest(device_id="wz-sat", user_id=3, requestor_id=1), None)

        assert response.ok is False
        assert response.message == "forbidden"

    def test_set_satellite_smalltalk_followup_ok(self):
        satellite_manager = MagicMock(set_satellite_smalltalk_followup=MagicMock(return_value=True))
        upsert = MagicMock()
        servicer = _make_server(satellite_manager=satellite_manager, upsert_satellite=upsert)

        response = servicer.SetSatelliteSmalltalkFollowup(
            SetSatelliteSmalltalkFollowupRequest(device_id="wz-sat", enabled=True, requestor_id=1), None,
        )

        upsert.assert_called_once_with("wz-sat")
        satellite_manager.set_satellite_smalltalk_followup.assert_called_once_with("wz-sat", True, requestor_id=1)
        assert response.ok is True

    def test_set_satellite_smalltalk_followup_not_found(self):
        satellite_manager = MagicMock(set_satellite_smalltalk_followup=MagicMock(return_value=False))
        servicer = _make_server(satellite_manager=satellite_manager)

        response = servicer.SetSatelliteSmalltalkFollowup(
            SetSatelliteSmalltalkFollowupRequest(device_id="unknown", enabled=True, requestor_id=1), None,
        )

        assert response.ok is False

    def test_set_satellite_smalltalk_followup_forbidden(self):
        satellite_manager = MagicMock(
            set_satellite_smalltalk_followup=MagicMock(side_effect=SatellitePermissionError("nope")),
        )
        servicer = _make_server(satellite_manager=satellite_manager)

        response = servicer.SetSatelliteSmalltalkFollowup(
            SetSatelliteSmalltalkFollowupRequest(device_id="wz-sat", enabled=True, requestor_id=1), None,
        )

        assert response.ok is False
        assert response.message == "forbidden"

    def test_delete_satellite_ok(self):
        satellite_manager = MagicMock(
            get_satellite=MagicMock(return_value=SimpleNamespace(device_id="wz-sat", room_id="wohnzimmer")),
            delete_satellite=MagicMock(return_value=True),
        )
        servicer = _make_server(satellite_manager=satellite_manager)

        response = servicer.DeleteSatellite(DeleteSatelliteRequest(device_id="wz-sat", requestor_id=1), None)

        satellite_manager.delete_satellite.assert_called_once_with("wz-sat", requestor_id=1)
        assert response.ok is True

    def test_delete_satellite_not_found(self):
        satellite_manager = MagicMock(get_satellite=MagicMock(return_value=None))
        servicer = _make_server(satellite_manager=satellite_manager)

        response = servicer.DeleteSatellite(DeleteSatelliteRequest(device_id="unknown", requestor_id=1), None)

        satellite_manager.delete_satellite.assert_not_called()
        assert response.ok is False

    def test_delete_satellite_forbidden(self):
        satellite_manager = MagicMock(
            get_satellite=MagicMock(return_value=SimpleNamespace(device_id="wz-sat", room_id="wohnzimmer")),
            delete_satellite=MagicMock(side_effect=SatellitePermissionError("nope")),
        )
        servicer = _make_server(satellite_manager=satellite_manager)

        response = servicer.DeleteSatellite(DeleteSatelliteRequest(device_id="wz-sat", requestor_id=1), None)

        assert response.ok is False
        assert response.message == "forbidden"


class TestAnnounceRpc:
    def test_announce_forwards_device_and_text(self):
        announce = MagicMock()
        servicer = _make_server(announce=announce)

        response = servicer.Announce(AnnounceRequest(device="wz-sat", text="Hallo"), None)

        announce.assert_called_once_with("wz-sat", "Hallo", room_id="", user_id=0)
        assert response.ok is True

    def test_announce_forwards_room_id_and_user_id(self):
        announce = MagicMock()
        servicer = _make_server(announce=announce)

        servicer.Announce(AnnounceRequest(text="Hallo", room_id="wohnzimmer", user_id=3), None)

        announce.assert_called_once_with("", "Hallo", room_id="wohnzimmer", user_id=3)

    def test_announce_failure_returns_not_ok(self):
        announce = MagicMock(side_effect=RuntimeError("boom"))
        servicer = _make_server(announce=announce)

        response = servicer.Announce(AnnounceRequest(device="wz-sat", text="Hallo"), None)

        assert response.ok is False
        assert response.ok is False

class TestLoginRpc:
    """#27 Phase 3 — reine Verdrahtung auf UserManager.login_user(), kein eigener Callback
    (user_manager ist bereits Pflichtparameter)."""

    def test_login_ok(self):
        user = User(row={"id": 1, "username": "leonie", "display_name": "Leonie", "trust_level": 8,
                          "is_active": True, "system_messages": True, "email": "leonie@example.com",
                          "type": "roomie"}, db=None)
        user_manager = MagicMock(login_user=MagicMock(return_value=user))
        servicer = _make_server(user_manager=user_manager)

        response = servicer.Login(LoginRequest(username="leonie", password="geheim"), MagicMock())

        user_manager.login_user.assert_called_once_with("leonie", "geheim")
        assert response.found is True
        assert response.user.user_name == "leonie"

    def test_login_wrong_password(self):
        user_manager = MagicMock(login_user=MagicMock(return_value=None))
        servicer = _make_server(user_manager=user_manager)
        context = MagicMock()

        response = servicer.Login(LoginRequest(username="leonie", password="falsch"), context)

        assert response.found is False
        context.set_code.assert_called_once()

class TestUserManagementRpcs:
    """#27 Phase 6 — CreateUser/DeleteUser sind reine Verdrahtung auf UserManager;
    UpdateUser mutiert das User-Objekt direkt + save(), analog zu SetTrustLevel/
    SetSystemMessages (keine eigene UserManager-Methode dafür)."""

    def test_create_user_ok(self):
        created = MagicMock(id=5)
        user_manager = MagicMock(create_user=MagicMock(return_value=created))
        servicer = _make_server(user_manager=user_manager)

        response = servicer.CreateUser(CreateUserRequest(
            username="rene", password="geheim", email="rene@example.com",
            display_name="René", type="roomie",
        ), MagicMock())

        assert response.ok is True
        assert response.id == 5
        args, kwargs = user_manager.create_user.call_args
        assert args[0] == "rene"
        assert args[1] != "geheim"  # Passwort muss gehasht sein, nicht im Klartext ankommen
        assert kwargs["email"] == "rene@example.com"
        assert kwargs["display_name"] == "René"
        assert kwargs["type"] == "roomie"

    def test_create_user_duplicate(self):
        user_manager = MagicMock(create_user=MagicMock(side_effect=sqlite3.IntegrityError))
        servicer = _make_server(user_manager=user_manager)

        response = servicer.CreateUser(CreateUserRequest(username="rene", password="x", email="r@x.de"), MagicMock())

        assert response.ok is False

    def test_create_user_invalid_email(self):
        user_manager = MagicMock(create_user=MagicMock(side_effect=ValueError("Ungültige E-Mail-Adresse")))
        servicer = _make_server(user_manager=user_manager)

        response = servicer.CreateUser(CreateUserRequest(username="rene", password="x", email="x"), MagicMock())

        assert response.ok is False

    def test_update_user_ok(self):
        user = MagicMock()
        user_manager = MagicMock(get_user_by_id=MagicMock(return_value=user))
        servicer = _make_server(user_manager=user_manager)

        response = servicer.UpdateUser(UpdateUserRequest(
            user_id=1, display_name="Leonie neu", email="leonie@neu.de", type="roomie",
            is_active=True, password="neugeheim",
        ), MagicMock())

        assert response.ok is True
        assert user.display_name == "Leonie neu"
        assert user.email == "leonie@neu.de"
        assert user.is_active == 1
        user.save.assert_called_once()

    def test_update_user_not_found(self):
        user_manager = MagicMock(get_user_by_id=MagicMock(return_value=None))
        servicer = _make_server(user_manager=user_manager)
        context = MagicMock()

        response = servicer.UpdateUser(UpdateUserRequest(user_id=999), context)

        assert response.ok is False
        context.set_code.assert_called_once()

    def test_update_user_password_unchanged_when_blank(self):
        user = MagicMock(password_hash="old-hash")
        user_manager = MagicMock(get_user_by_id=MagicMock(return_value=user))
        servicer = _make_server(user_manager=user_manager)

        servicer.UpdateUser(UpdateUserRequest(user_id=1, display_name="x", password=""), MagicMock())

        assert user.password_hash == "old-hash"

    def test_delete_user_ok(self):
        delete = MagicMock(return_value=True)
        servicer = _make_server(user_manager=MagicMock(delete_user=delete))

        response = servicer.DeleteUser(DeleteUserRequest(user_id=1), None)

        delete.assert_called_once_with(1)
        assert response.ok is True

    def test_get_residents(self):
        resident = Roomie("leonie", "Leonie", presence_state=1)
        servicer = _make_server(get_residents=lambda: [resident])

        response = servicer.GetResidents(None, None)

        assert len(response.residents) == 1
        r = response.residents[0]
        assert r.id == "leonie_roomie"
        assert r.roomie_id == "leonie"
        assert r.display_name == "Leonie"
        assert r.type == "roomie"
        assert r.home is True

class TestTriggerRpcs:
    """#27 Phase 4 — Verdrahtung auf TriggerEngine.get_trigger_records/create_trigger/
    update_trigger/delete_trigger."""

    def test_get_triggers(self):
        get_trigger_records = MagicMock(return_value=[{
            "id": "aussentuer_abend", "when": {"time": "23:00"}, "cancel_when": None, "on_response": [],
            "actions": [{"say": "Denk an die Außentüren."}],
            "say": "Denk an die Außentüren.", "ask": "", "rephrase": 1, "room": "all", "cooldown": 3600, "delay": "",
        }])
        servicer = _make_server(get_trigger_records=get_trigger_records)

        response = servicer.GetTriggers(None, None)

        assert len(response.triggers) == 1
        t = response.triggers[0]
        assert t.id == "aussentuer_abend"
        assert json.loads(t.when_json) == {"time": "23:00"}
        assert t.cancel_when_json == ""
        assert t.rephrase is True
        assert json.loads(t.actions_json) == [{"say": "Denk an die Außentüren."}]

    def test_create_trigger_ok(self):
        create_trigger = MagicMock(return_value=True)
        servicer = _make_server(create_trigger=create_trigger)

        response = servicer.CreateTrigger(CreateTriggerRequest(
            id="fenster_kalt", when_json=json.dumps({"state": "x", "value": True}), say="Fenster offen.",
        ), None)

        create_trigger.assert_called_once_with("fenster_kalt", {"state": "x", "value": True}, None, [], [],
                                                "Fenster offen.", "", False, "all", 3600, "")
        assert response.ok is True

    def test_create_trigger_actions_round_trip(self):
        create_trigger = MagicMock(return_value=True)
        servicer = _make_server(create_trigger=create_trigger)
        actions = [{"say": "Licht im Flur ist an.", "room": "all"},
                   {"set_state": {"id": "javascript.0.virtualDevice.Licht.EG.Flur.on", "value": False}}]

        response = servicer.CreateTrigger(CreateTriggerRequest(
            id="flur_licht", when_json=json.dumps([{"state": "x", "value": True}, {"state": "y", "value": True}]),
            actions_json=json.dumps(actions),
        ), None)

        create_trigger.assert_called_once_with(
            "flur_licht", [{"state": "x", "value": True}, {"state": "y", "value": True}], None, [], actions,
            "", "", False, "all", 3600, "")
        assert response.ok is True

    def test_update_trigger_actions_round_trip(self):
        update_trigger = MagicMock(return_value=True)
        servicer = _make_server(update_trigger=update_trigger)
        actions = [{"say": "Aktualisiert."}]

        response = servicer.UpdateTrigger(UpdateTriggerRequest(
            id="flur_licht", when_json="{}", actions_json=json.dumps(actions),
        ), None)

        update_trigger.assert_called_once_with("flur_licht", {}, None, [], actions,
                                                "", "", False, "all", 3600, "")
        assert response.ok is True

    def test_create_trigger_duplicate_id(self):
        servicer = _make_server(create_trigger=MagicMock(return_value=False))

        response = servicer.CreateTrigger(CreateTriggerRequest(id="x", when_json="{}"), None)

        assert response.ok is False

    def test_create_trigger_invalid_json(self):
        servicer = _make_server(create_trigger=MagicMock())

        response = servicer.CreateTrigger(CreateTriggerRequest(id="x", when_json="{not json"), None)

        assert response.ok is False

    def test_update_trigger_not_found(self):
        servicer = _make_server(update_trigger=MagicMock(return_value=False))

        response = servicer.UpdateTrigger(UpdateTriggerRequest(id="unknown", when_json="{}"), None)

        assert response.ok is False

    def test_delete_trigger_ok(self):
        delete_trigger = MagicMock(return_value=True)
        servicer = _make_server(delete_trigger=delete_trigger)

        response = servicer.DeleteTrigger(DeleteTriggerRequest(id="fenster_kalt"), None)

        delete_trigger.assert_called_once_with("fenster_kalt")
        assert response.ok is True

class TestAlarmRpcs:
    """#4 — Verdrahtung auf AlarmManager.get_alarm_records/create_alarm/update_alarm/
    delete_alarm. Wie bei Routine/Trigger reine Weiterleitung, Berechtigungs-/Fachlogik
    lebt im Manager (siehe test_alarm_manager.py)."""

    def test_get_alarms(self):
        get_alarm_records = MagicMock(return_value=[{
            "id": 1, "satellite_id": "wz-sat", "time": "08:00", "weekdays": [0, 1, 2, 3, 4],
            "skip_dates": [], "one_shot_date": "", "enabled": True, "label": "Aufstehen", "user_id": 3,
        }])
        servicer = _make_server(get_alarm_records=get_alarm_records)

        response = servicer.GetAlarms(None, None)

        assert len(response.alarms) == 1
        a = response.alarms[0]
        assert a.id == 1
        assert a.satellite_id == "wz-sat"
        assert list(a.weekdays) == [0, 1, 2, 3, 4]
        assert a.label == "Aufstehen"
        assert a.user_id == 3

    def test_create_alarm_ok(self):
        create_alarm = MagicMock(return_value={"id": 7})
        servicer = _make_server(create_alarm=create_alarm)

        response = servicer.CreateAlarm(CreateAlarmRequest(
            satellite_id="wz-sat", time="08:00", weekdays=[0], one_shot_date="", label="", user_id=3,
        ), None)

        create_alarm.assert_called_once_with("wz-sat", "08:00", [0], None, 3, "")
        assert response.ok is True
        assert response.id == 7

    def test_create_alarm_one_off_no_weekdays(self):
        create_alarm = MagicMock(return_value={"id": 8})
        servicer = _make_server(create_alarm=create_alarm)

        servicer.CreateAlarm(CreateAlarmRequest(
            satellite_id="wz-sat", time="08:00", one_shot_date="2026-07-06", user_id=3,
        ), None)

        create_alarm.assert_called_once_with("wz-sat", "08:00", None, "2026-07-06", 3, "")

    def test_create_alarm_invalid(self):
        servicer = _make_server(create_alarm=MagicMock(return_value=None))

        response = servicer.CreateAlarm(CreateAlarmRequest(satellite_id="wz-sat", time="08:00"), None)

        assert response.ok is False

    def test_update_alarm_not_found(self):
        servicer = _make_server(update_alarm=MagicMock(return_value=False))

        response = servicer.UpdateAlarm(UpdateAlarmRequest(id=99, satellite_id="wz-sat", time="08:00"), None)

        assert response.ok is False

    def test_delete_alarm_ok(self):
        delete_alarm = MagicMock(return_value=True)
        servicer = _make_server(delete_alarm=delete_alarm)

        response = servicer.DeleteAlarm(DeleteAlarmRequest(id=7), None)

        delete_alarm.assert_called_once_with(7)
        assert response.ok is True

class TestSettingsRpcs:
    """#27 Phase 5 — Verdrahtung auf SettingsManager.get_categories/get_settings/
    update_setting_value. CreateSetting/DeleteSetting wurden mit #115 entfernt
    (ble.tags/cars haben jetzt eigene Modelle, siehe TestBleTagRpcs/TestCarRpcs)."""

    def test_get_settings(self):
        get_categories = MagicMock(return_value=[
            {"id": 1, "name": "ble", "parent": None},
            {"id": 2, "name": "ble.tags", "parent": 1},
        ])
        get_settings_records = MagicMock(return_value=[
            {"id": 1, "category": 2, "name": "leonie", "value": {"mac": "aa:bb", "username": "leonie"}},
        ])
        servicer = _make_server(get_categories=get_categories, get_settings_records=get_settings_records)

        response = servicer.GetSettings(None, None)

        assert len(response.categories) == 2
        assert response.categories[0].parent_id == 0
        assert response.categories[1].parent_id == 1
        assert len(response.settings) == 1
        s = response.settings[0]
        assert s.id == 1
        assert s.category_id == 2
        assert s.name == "leonie"
        assert json.loads(s.value) == {"mac": "aa:bb", "username": "leonie"}

    def test_update_config_ok(self):
        update_setting_value = MagicMock(return_value=True)
        servicer = _make_server(update_setting_value=update_setting_value)

        response = servicer.UpdateConfig(UpdateConfigRequest(updates=[
            SettingUpdate(setting_id=1, value=json.dumps({"mac": "aa:bb"})),
        ]), None)

        update_setting_value.assert_called_once_with(1, {"mac": "aa:bb"})
        assert response.ok is True

    def test_update_config_not_found(self):
        servicer = _make_server(update_setting_value=MagicMock(return_value=False))

        response = servicer.UpdateConfig(UpdateConfigRequest(updates=[
            SettingUpdate(setting_id=99, value="{}"),
        ]), None)

        assert response.ok is False

    def test_update_config_invalid_json(self):
        update_setting_value = MagicMock()
        servicer = _make_server(update_setting_value=update_setting_value)

        response = servicer.UpdateConfig(UpdateConfigRequest(updates=[
            SettingUpdate(setting_id=1, value="{not json"),
        ]), None)

        update_setting_value.assert_not_called()
        assert response.ok is False

class TestBleTagRpcs:
    """#115 — Verdrahtung auf BleTagManager.get_tag_records/create_tag/update_tag/
    delete_tag (eigenes Modell statt Settings-JSON-Blob)."""

    def test_get_ble_tags(self):
        get_ble_tag_records = MagicMock(return_value=[
            {"id": 1, "mac_address": "aa:bb:cc:dd:ee:ff", "label": "leonie", "user_id": 3},
        ])
        servicer = _make_server(get_ble_tag_records=get_ble_tag_records)

        response = servicer.GetBleTags(None, None)

        assert len(response.tags) == 1
        t = response.tags[0]
        assert t.id == 1
        assert t.mac_address == "aa:bb:cc:dd:ee:ff"
        assert t.label == "leonie"
        assert t.user_id == 3

    def test_create_ble_tag_ok(self):
        create_ble_tag = MagicMock(return_value={"id": 5})
        servicer = _make_server(create_ble_tag=create_ble_tag)

        response = servicer.CreateBleTag(CreateBleTagRequest(
            mac_address="aa:bb:cc:dd:ee:ff", label="leonie", user_id=3,
        ), None)

        create_ble_tag.assert_called_once_with("aa:bb:cc:dd:ee:ff", "leonie", 3)
        assert response.ok is True
        assert response.id == 5

    def test_create_ble_tag_duplicate_mac(self):
        servicer = _make_server(create_ble_tag=MagicMock(return_value=None))

        response = servicer.CreateBleTag(CreateBleTagRequest(mac_address="aa:bb:cc:dd:ee:ff", label="leonie"), None)

        assert response.ok is False

    def test_update_ble_tag_not_found(self):
        servicer = _make_server(update_ble_tag=MagicMock(return_value=False))

        response = servicer.UpdateBleTag(UpdateBleTagRequest(id=99, mac_address="aa:bb", label="x"), None)

        assert response.ok is False

    def test_delete_ble_tag_ok(self):
        delete_ble_tag = MagicMock(return_value=True)
        servicer = _make_server(delete_ble_tag=delete_ble_tag)

        response = servicer.DeleteBleTag(DeleteBleTagRequest(id=5), None)

        delete_ble_tag.assert_called_once_with(5)
        assert response.ok is True

class TestCarRpcs:
    """#115 — Verdrahtung auf CarRegistry.get_car_records/create_car/update_car/
    delete_car (eigenes Modell + user_to_car-Pivot statt Settings-JSON-Blob)."""

    def test_get_cars(self):
        get_car_records = MagicMock(return_value=[
            {"id": 1, "name": "Mein Auto", "topic_prefix": "javascript/0/virtualDevice/Auto/Leonie/Auto1",
             "home_address": "Musterstr. 1", "owner_user_ids": [3, 4]},
        ])
        servicer = _make_server(get_car_records=get_car_records)

        response = servicer.GetCars(None, None)

        assert len(response.cars) == 1
        c = response.cars[0]
        assert c.id == 1
        assert c.name == "Mein Auto"
        assert c.topic_prefix == "javascript/0/virtualDevice/Auto/Leonie/Auto1"
        assert list(c.owner_user_ids) == [3, 4]

    def test_create_car_ok(self):
        create_car = MagicMock(return_value={"id": 7})
        servicer = _make_server(create_car=create_car)

        response = servicer.CreateCar(CreateCarRequest(
            name="Mein Auto", topic_prefix="auto1", home_address="Musterstr. 1", owner_user_ids=[3],
        ), None)

        create_car.assert_called_once_with("Mein Auto", "auto1", "Musterstr. 1", [3])
        assert response.ok is True
        assert response.id == 7

    def test_create_car_duplicate_topic_prefix(self):
        servicer = _make_server(create_car=MagicMock(return_value=None))

        response = servicer.CreateCar(CreateCarRequest(topic_prefix="auto1", home_address=""), None)

        assert response.ok is False

    def test_update_car_not_found(self):
        servicer = _make_server(update_car=MagicMock(return_value=False))

        response = servicer.UpdateCar(UpdateCarRequest(id=99, topic_prefix="auto1"), None)

        assert response.ok is False

    def test_delete_car_ok(self):
        delete_car = MagicMock(return_value=True)
        servicer = _make_server(delete_car=delete_car)

        response = servicer.DeleteCar(DeleteCarRequest(id=7), None)

        delete_car.assert_called_once_with(7)
        assert response.ok is True

def test_notify_satellite_gone_does_not_double_send():
    on_satellite_change = MagicMock()
    servicer = _make_server(on_satellite_change=on_satellite_change, resolve_satellite_room=lambda _d: "wohnzimmer")
    servicer.agent_satellite_update = MagicMock()
    servicer.NotifySatelliteRegistered(SatelliteRegistration(device_id="wz-sat", address="192.168.1.50"), None)
    on_satellite_change.reset_mock()

    request = SatelliteRegistration(device_id="wz-sat")
    response = servicer.NotifySatelliteGone(request, None)

    assert response.ok
    servicer.agent_satellite_update.assert_not_called()
    on_satellite_change.assert_called_once_with({})

def _make_user_manager_with_leonie(tmp_path):
    """Real (non-mocked) UserManager against a throwaway SQLite DB. User/LinkedAccount
    bugs only show up against the real model layer — a MagicMock hides them.

    hannah.utils.db.DB_PATH is read once at import time, so the env var alone
    only takes effect on the very first import in this test session — patch
    the module attribute directly to get a fresh DB per test.
    """
    import hannah.utils.db as db_module
    db_module.DB_PATH = os.path.join(str(tmp_path), "h.db")
    db_module.init_db()
    User.create(
        db_module.get_db(), username="leonie", display_name="Leonie", email="leonie@example.com",
        password_hash=generate_password_hash("x"), trust_level=10, mood_level=5,
        system_messages=0, type="roomie", is_active=1,
    )
    get_db = db_module.get_db
    return UserManager(get_db), get_db

def test_link_account_accepts_int32_user_id(tmp_path):
    """Regression: get_user_by_id() must accept the int32 proto user_id and find the
    just-cached user — used to KeyError because the cache is int-keyed but a
    digit-string slipped through before user_id became int32 on the wire."""
    user_manager, _get_db = _make_user_manager_with_leonie(tmp_path)
    user = user_manager.get_user_by_username("leonie")
    servicer = _make_server(user_manager=user_manager)

    request = LinkAccountRequest(user_id=user.id, service="telegram", account_id="99999")
    response = servicer.LinkAccount(request, MagicMock())

    assert response.ok is True

def test_link_account_with_json_provider_payload_round_trips_as_dict(tmp_path):
    """Regression: the webui sends provider_payload pre-serialized as a JSON string
    (proto has no open-ended object type). LinkAccount used to pass that string straight
    to LinkedAccount.create(), which double-JSON-encodes it (the model's __json_fields__
    already does its own json.dumps()/json.loads()) — on read back, provider_payload was
    still a string, not a dict, and UserManager._resident_link() crashed the whole boot
    with AttributeError: 'str' object has no attribute 'get' (#206 follow-up)."""
    user_manager, get_db = _make_user_manager_with_leonie(tmp_path)
    user = user_manager.get_user_by_username("leonie")
    servicer = _make_server(user_manager=user_manager)

    payload = json.dumps({"resident_type": "roomie", "roomie_id": "leonie"})
    request = LinkAccountRequest(user_id=user.id, service="residents", account_id="leonie", provider_payload=payload)
    response = servicer.LinkAccount(request, MagicMock())
    assert response.ok is True

    fresh = User.get(get_db(), id=user.id)
    la = fresh.get_linked_account("residents")
    assert isinstance(la.provider_payload, dict)
    assert la.provider_payload["roomie_id"] == "leonie"

    assert user_manager._resident_link(fresh) == ("leonie", "roomie")

def test_resident_link_ignores_malformed_string_provider_payload(tmp_path):
    """Defensive: a legacy/corrupted provider_payload (e.g. from the double-encoding bug
    above, before it was fixed) decodes to a plain string instead of a dict.
    _resident_link() must treat that as "no usable roomie_id", not crash (#206 follow-up)."""
    user_manager, get_db = _make_user_manager_with_leonie(tmp_path)
    user = user_manager.get_user_by_username("leonie")
    # Simulate a pre-existing double-encoded row: the raw text stored via the old buggy
    # path decodes (once) to a JSON *string*, not a dict.
    user.link_account("residents", "leonie", provider_payload='{"resident_type": "roomie", "roomie_id": "leonie"}')

    fresh = User.get(get_db(), id=user.id)
    assert user_manager._resident_link(fresh) is None

def test_user_to_pb_with_linked_account(tmp_path):
    """Regression: _user_to_pb crashed with AttributeError on acc.service (the model
    attribute is .provider) for any user with a linked account; also covers
    provider_payload="" round-tripping without a JSONDecodeError."""
    user_manager, get_db = _make_user_manager_with_leonie(tmp_path)
    user = user_manager.get_user_by_username("leonie")
    user.link_account("telegram", "99999")

    fresh = User.get(get_db(), id=user.id)
    pb_user = _user_to_pb(fresh)

    assert pb_user.linked_accounts["telegram"] == "99999"

def _connect_fake_timer_service(servicer):
    """Simulates a connected Timer Service by directly wiring the internal queue —
    bypasses the real TimerConnect stream, which the sync unary bridge below doesn't
    otherwise interact with."""
    servicer._timer_queue = queue.Queue()

def _respond_with_timer_list(servicer, timers, delay=0.05):
    """Simulates the Timer Service pushing back a TimerListResponse after `delay`
    seconds, exactly like TimerConnect's drain loop would on a real "list" message."""
    def _respond():
        time.sleep(delay)
        with servicer._timer_lock:
            waiters, servicer._timer_list_waiters = servicer._timer_list_waiters, []
        for waiter in waiters:
            waiter.put(timers)
    threading.Thread(target=_respond, daemon=True).start()

def test_get_timers_without_timer_service_returns_empty():
    servicer = _make_server()
    response = servicer.GetTimers(GetTimersRequest(user_id=1), MagicMock())
    assert list(response.timers) == []

def test_get_timers_filters_by_user_and_computes_remaining_seconds():
    servicer = _make_server()
    _connect_fake_timer_service(servicer)
    now = int(time.time())
    timers = [
        TimerInfo(timer_id="a", label="Kaffee", fire_at=now + 600, metadata={"user_id": "1", "room": "kueche"}),
        TimerInfo(timer_id="b", label="Wäsche", fire_at=now + 60, metadata={"user_id": "2", "room": "keller"}),
    ]
    _respond_with_timer_list(servicer, timers)

    response = servicer.GetTimers(GetTimersRequest(user_id=1), MagicMock())

    assert len(response.timers) == 1
    result = response.timers[0]
    assert result.timer_id == "a"
    assert result.room_id == "kueche"
    assert result.user_id == 1
    assert 590 <= result.seconds <= 600

def test_get_timers_times_out_without_response():
    servicer = _make_server()
    _connect_fake_timer_service(servicer)
    # No _respond_with_timer_list call — nothing ever answers the list request.
    timers = servicer._request_timer_list(timeout=0.1)
    assert timers is None

def test_delete_timer_without_timer_service_returns_not_ok():
    servicer = _make_server()
    response = servicer.DeleteTimer(DeleteTimerRequest(timer_id="a", requestor_id=1), MagicMock())
    assert response.ok is False

def test_delete_timer_pushes_cancel_command():
    servicer = _make_server()
    _connect_fake_timer_service(servicer)

    response = servicer.DeleteTimer(DeleteTimerRequest(timer_id="a", requestor_id=1), MagicMock())

    assert response.ok is True
    cmd = servicer._timer_queue.get_nowait()
    assert cmd.cancel.timer_id == "a"