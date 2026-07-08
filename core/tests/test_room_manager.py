import os

import pytest

import hannah.utils.db as db_module
from hannah.room_manager import RoomManager
from hannah.satellite_manager import SatelliteManager


@pytest.fixture
def manager(tmp_path):
    """Real (non-mocked) RoomManager against a throwaway SQLite DB — see
    hannah.utils.db.DB_PATH docstring note in test_grpc_server.py for why this
    has to patch the module attribute directly rather than just an env var."""
    db_module.DB_PATH = os.path.join(str(tmp_path), "h.db")
    db_module.init_db()
    yield RoomManager(db_module.get_db)


@pytest.fixture
def satellites(manager):
    """SatelliteManager auf derselben DB wie `manager` — sync_rooms() orphant
    Satelliten direkt im Satellite-Model, die Sichtbarkeit wird hier über
    SatelliteManager geprüft (siehe #108)."""
    return SatelliteManager(db_module.get_db, {"seed_ttl_days": 7})


def _insert_satellite(mgr, device_id, seed, days_old):
    with mgr._db().connection as conn:
        conn.execute(
            """INSERT INTO satellites (device_id, seed, display_name, created_at)
               VALUES (?, ?, ?, datetime('now', ?))""",
            (device_id, seed, device_id, f"-{days_old} days"),
        )


class TestSyncRooms:
    def test_empty_snapshot_is_noop(self, manager):
        manager.sync_rooms({"wohnzimmer": "Wohnzimmer"})

        orphaned = manager.sync_rooms({})

        assert orphaned == []
        assert manager.get_rooms() == [{"room_id": "wohnzimmer", "display_name": "Wohnzimmer"}]

    def test_vanished_room_orphans_its_satellite(self, manager, satellites):
        manager.sync_rooms({"wohnzimmer": "Wohnzimmer"})
        _insert_satellite(manager, "wz-esp", "seed-1", days_old=0)
        satellites.set_satellite_room("wz-esp", "wohnzimmer")

        orphaned = manager.sync_rooms({"kueche": "Küche"})

        assert orphaned == [("wz-esp", "wohnzimmer")]
        assert satellites.get_satellite_room("wz-esp") is None
        assert manager.get_rooms() == [{"room_id": "kueche", "display_name": "Küche"}]

    def test_vanished_room_without_satellites_just_removed(self, manager):
        manager.sync_rooms({"keller": "Keller"})

        orphaned = manager.sync_rooms({"kueche": "Küche"})

        assert orphaned == []
        assert manager.get_rooms() == [{"room_id": "kueche", "display_name": "Küche"}]

    def test_room_still_present_keeps_its_satellite(self, manager, satellites):
        manager.sync_rooms({"wohnzimmer": "Wohnzimmer"})
        _insert_satellite(manager, "wz-esp", "seed-1", days_old=0)
        satellites.set_satellite_room("wz-esp", "wohnzimmer")

        orphaned = manager.sync_rooms({"wohnzimmer": "Wohnzimmer"})

        assert orphaned == []
        assert satellites.get_satellite_room("wz-esp") == "wohnzimmer"
