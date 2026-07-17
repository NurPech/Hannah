import os

import pytest
from werkzeug.security import generate_password_hash

import hannah.utils.db as db_module
from hannah.satellite_manager import SatelliteManager, SatellitePermissionError
from hannah.user_manager import UserManager
from hannah.models.user import User


@pytest.fixture
def manager(tmp_path):
    """Real (non-mocked) SatelliteManager against a throwaway SQLite DB — see
    hannah.utils.db.DB_PATH docstring note in test_grpc_server.py for why this
    has to patch the module attribute directly rather than just an env var."""
    db_module.DB_PATH = os.path.join(str(tmp_path), "h.db")
    db_module.init_db()
    yield SatelliteManager(db_module.get_db, {"seed_ttl_days": 7})


@pytest.fixture
def manager_with_users(tmp_path):
    """Real SatelliteManager + real UserManager (real SQLite) — die Trust-Level-/
    Eigentümer-Prüfung (#111) läuft gegen echte User-Objekte statt Mocks."""
    db_module.DB_PATH = os.path.join(str(tmp_path), "h.db")
    db_module.init_db()
    db = db_module.get_db
    user_manager = UserManager(db)
    yield SatelliteManager(db, {"seed_ttl_days": 7}, user_manager=user_manager), db


def _create_user(db_callable, username, trust_level) -> int:
    User.create(
        db_callable(), username=username, display_name=username, email=f"{username}@example.com",
        password_hash=generate_password_hash("x"), trust_level=trust_level, mood_level=5,
        system_messages=0, type="roomie", is_active=1,
    )
    return User.get(db_callable(), username=username).id


def _insert_room(db_callable, room_id, display_name):
    with db_callable().connection as conn:
        conn.execute("INSERT INTO rooms (room_id, display_name) VALUES (?, ?)", (room_id, display_name))


def _insert_satellite(mgr, device_id, seed, days_old):
    with mgr._db().connection as conn:
        conn.execute(
            """INSERT INTO satellites (device_id, seed, display_name, created_at)
               VALUES (?, ?, ?, datetime('now', ?))""",
            (device_id, seed, device_id, f"-{days_old} days"),
        )


class TestCleanupStaleSeeds:
    def test_stale_unpaired_seed_removed(self, manager):
        _insert_satellite(manager, "old-seed", "seed-abc", days_old=8)

        removed = manager.cleanup_stale_seeds()

        assert removed == 1
        assert manager.get_satellite("old-seed") is None

    def test_fresh_unpaired_seed_kept(self, manager):
        _insert_satellite(manager, "new-seed", "seed-def", days_old=1)

        removed = manager.cleanup_stale_seeds()

        assert removed == 0
        assert manager.get_satellite("new-seed") is not None

    def test_paired_satellite_kept_regardless_of_age(self, manager):
        with manager._db().connection as conn:
            conn.execute(
                """INSERT INTO satellites (device_id, seed, display_name, paired_at, created_at)
                   VALUES (?, NULL, ?, datetime('now', '-30 days'), datetime('now', '-30 days'))""",
                ("paired-sat", "paired-sat"),
            )

        removed = manager.cleanup_stale_seeds()

        assert removed == 0
        assert manager.get_satellite("paired-sat") is not None


class TestOwner:
    def test_set_and_get_owner(self, manager):
        _insert_satellite(manager, "wz-esp", "seed-1", days_old=0)

        ok = manager.set_satellite_owner("wz-esp", 1)

        assert ok is True
        assert manager.get_satellite_owner("wz-esp") == 1

    def test_clear_owner(self, manager):
        _insert_satellite(manager, "wz-esp", "seed-1", days_old=0)
        manager.set_satellite_owner("wz-esp", 1)

        manager.set_satellite_owner("wz-esp", None)

        assert manager.get_satellite_owner("wz-esp") is None

    def test_unknown_device_returns_false(self, manager):
        assert manager.set_satellite_owner("unknown", 1) is False


class TestRoomAndUserLookup:
    """get_room_satellite_ids/get_user_satellites — Grundlage für #31s Announce-Routing."""

    def test_get_room_satellite_ids(self, manager):
        with manager._db().connection as conn:
            conn.execute("INSERT INTO rooms (room_id, display_name) VALUES ('wohnzimmer', 'Wohnzimmer')")
        _insert_satellite(manager, "wz-esp", "seed-1", days_old=0)
        _insert_satellite(manager, "ku-esp", "seed-2", days_old=0)
        manager.set_satellite_room("wz-esp", "wohnzimmer")

        assert manager.get_room_satellite_ids("wohnzimmer") == ["wz-esp"]

    def test_get_room_satellite_ids_empty_room(self, manager):
        with manager._db().connection as conn:
            conn.execute("INSERT INTO rooms (room_id, display_name) VALUES ('keller', 'Keller')")

        assert manager.get_room_satellite_ids("keller") == []

    def test_get_user_satellites(self, manager):
        _insert_satellite(manager, "wz-esp", "seed-1", days_old=0)
        _insert_satellite(manager, "ku-esp", "seed-2", days_old=0)
        manager.set_satellite_owner("wz-esp", 1)

        result = manager.get_user_satellites(1)

        assert [s["device_id"] for s in result] == ["wz-esp"]

    def test_get_user_satellites_none_assigned(self, manager):
        assert manager.get_user_satellites(1) == []


class TestFirmware:
    """#157 — Firmware-Version/Update-Verfügbarkeit werden im Satellite-Modell
    persistiert statt nur im flüchtigen In-Memory-Dict aus main.py."""

    def test_set_firmware_persists_version(self, manager):
        _insert_satellite(manager, "wz-esp", "seed-1", days_old=0)

        manager.set_satellite_firmware("wz-esp", "0.60.7")

        sat = manager.get_satellite("wz-esp")
        assert sat.firmware_version == "0.60.7"
        assert bool(sat.update_available) is False

    def test_set_firmware_creates_unknown_satellite(self, manager):
        manager.set_satellite_firmware("new-esp", "0.60.7")

        sat = manager.get_satellite("new-esp")
        assert sat is not None
        assert sat.firmware_version == "0.60.7"

    def test_set_update_available(self, manager):
        _insert_satellite(manager, "wz-esp", "seed-1", days_old=0)
        manager.set_satellite_firmware("wz-esp", "0.60.6")

        manager.set_satellite_update_available("wz-esp", "0.60.7")

        sat = manager.get_satellite("wz-esp")
        assert bool(sat.update_available) is True
        assert sat.new_version == "0.60.7"
        assert sat.firmware_version == "0.60.6"  # unverändert — nur die Zielversion ist neu

    def test_firmware_report_matching_pending_clears_update_available(self, manager):
        """Meldet der Satellit nach dem OTA genau die zuvor als new_version gemerkte
        Version, ist das Update installiert — update_available/new_version räumen sich weg."""
        _insert_satellite(manager, "wz-esp", "seed-1", days_old=0)
        manager.set_satellite_firmware("wz-esp", "0.60.6")
        manager.set_satellite_update_available("wz-esp", "0.60.7")

        manager.set_satellite_firmware("wz-esp", "0.60.7")

        sat = manager.get_satellite("wz-esp")
        assert sat.firmware_version == "0.60.7"
        assert bool(sat.update_available) is False
        assert sat.new_version is None

    def test_firmware_report_not_matching_pending_keeps_update_available(self, manager):
        """Zwischenzeitlicher Report einer anderen Version (z.B. Boot auf altem Stand
        nach Hard-Reset) darf ein bestehendes update_available nicht fälschlich löschen."""
        _insert_satellite(manager, "wz-esp", "seed-1", days_old=0)
        manager.set_satellite_firmware("wz-esp", "0.60.6")
        manager.set_satellite_update_available("wz-esp", "0.60.7")

        manager.set_satellite_firmware("wz-esp", "0.60.6")

        sat = manager.get_satellite("wz-esp")
        assert bool(sat.update_available) is True
        assert sat.new_version == "0.60.7"


class TestPermissions:
    """#111 — DeleteSatellite/SetSatelliteOwner: nur Trustlevel 10. SetSatelliteRoom/
    SetSatelliteDisplayName: ab Trustlevel 5, aber nur für "eigene" Satelliten
    (owner_user_id == requestor_id); Trustlevel 10 ist uneingeschränkt. requestor_id=None
    (interner/systemseitiger Aufruf, z.B. sync_rooms-Orphaning) umgeht die Prüfung."""

    def test_admin_can_delete(self, manager_with_users):
        mgr, db = manager_with_users
        admin_id = _create_user(db, "trustadmin1", 10)
        _insert_satellite(mgr, "wz-esp", "seed-1", days_old=0)

        assert mgr.delete_satellite("wz-esp", requestor_id=admin_id) is True
        assert mgr.get_satellite("wz-esp") is None

    def test_non_admin_cannot_delete(self, manager_with_users):
        mgr, db = manager_with_users
        user_id = _create_user(db, "user", 9)
        _insert_satellite(mgr, "wz-esp", "seed-1", days_old=0)

        with pytest.raises(SatellitePermissionError):
            mgr.delete_satellite("wz-esp", requestor_id=user_id)
        assert mgr.get_satellite("wz-esp") is not None

    def test_non_admin_cannot_set_owner(self, manager_with_users):
        mgr, db = manager_with_users
        user_id = _create_user(db, "user", 9)
        _insert_satellite(mgr, "wz-esp", "seed-1", days_old=0)

        with pytest.raises(SatellitePermissionError):
            mgr.set_satellite_owner("wz-esp", user_id, requestor_id=user_id)

    def test_owner_with_trust5_can_rename_own_satellite(self, manager_with_users):
        mgr, db = manager_with_users
        owner_id = _create_user(db, "owner", 5)
        _insert_satellite(mgr, "wz-esp", "seed-1", days_old=0)
        mgr.set_satellite_owner("wz-esp", owner_id)  # interner Aufruf, kein requestor_id nötig

        assert mgr.set_satellite_display_name("wz-esp", "Mein Sat", requestor_id=owner_id) is True
        assert mgr.get_satellite("wz-esp").display_name == "Mein Sat"

    def test_trust5_cannot_touch_others_satellite(self, manager_with_users):
        mgr, db = manager_with_users
        owner_id = _create_user(db, "owner", 10)
        other_id = _create_user(db, "other", 5)
        _insert_room(db, "wohnzimmer", "Wohnzimmer")
        _insert_satellite(mgr, "wz-esp", "seed-1", days_old=0)
        mgr.set_satellite_owner("wz-esp", owner_id)

        with pytest.raises(SatellitePermissionError):
            mgr.set_satellite_room("wz-esp", "wohnzimmer", requestor_id=other_id)

    def test_trust5_cannot_touch_unowned_satellite(self, manager_with_users):
        mgr, db = manager_with_users
        user_id = _create_user(db, "user5", 5)
        _insert_room(db, "wohnzimmer", "Wohnzimmer")
        _insert_satellite(mgr, "wz-esp", "seed-1", days_old=0)

        with pytest.raises(SatellitePermissionError):
            mgr.set_satellite_room("wz-esp", "wohnzimmer", requestor_id=user_id)

    def test_trust_below_5_denied(self, manager_with_users):
        mgr, db = manager_with_users
        user_id = _create_user(db, "user3", 3)
        _insert_room(db, "wohnzimmer", "Wohnzimmer")
        _insert_satellite(mgr, "wz-esp", "seed-1", days_old=0)

        with pytest.raises(SatellitePermissionError):
            mgr.set_satellite_room("wz-esp", "wohnzimmer", requestor_id=user_id)

    def test_admin_can_rename_others_satellite(self, manager_with_users):
        mgr, db = manager_with_users
        owner_id = _create_user(db, "owner", 5)
        admin_id = _create_user(db, "trustadmin2", 10)
        _insert_room(db, "wohnzimmer", "Wohnzimmer")
        _insert_satellite(mgr, "wz-esp", "seed-1", days_old=0)
        mgr.set_satellite_owner("wz-esp", owner_id)

        assert mgr.set_satellite_room("wz-esp", "wohnzimmer", requestor_id=admin_id) is True

    def test_unknown_requestor_denied(self, manager_with_users):
        mgr, db = manager_with_users
        _insert_satellite(mgr, "wz-esp", "seed-1", days_old=0)

        with pytest.raises(SatellitePermissionError):
            mgr.delete_satellite("wz-esp", requestor_id=999)

    def test_no_requestor_bypasses_check(self, manager_with_users):
        mgr, db = manager_with_users
        _insert_room(db, "wohnzimmer", "Wohnzimmer")
        _insert_satellite(mgr, "wz-esp", "seed-1", days_old=0)

        assert mgr.set_satellite_room("wz-esp", "wohnzimmer") is True
