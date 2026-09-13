import os

import pytest
from werkzeug.security import generate_password_hash

import hannah.utils.db as db_module
from hannah.presence_sources import PresenceSourceManager
from hannah.models.user import User


@pytest.fixture
def db(tmp_path):
    db_module.DB_PATH = os.path.join(str(tmp_path), "h.db")
    db_module.init_db()
    return db_module.get_db


def _create_user(db, username="leonie") -> int:
    User.create(
        db(), username=username, display_name=username, email=f"{username}@example.com",
        password_hash=generate_password_hash("x"), trust_level=5, mood_level=5,
        system_messages=0, type="roomie", is_active=1,
    )
    return User.get(db(), username=username).id


@pytest.fixture
def manager(db):
    return PresenceSourceManager(db)


class TestCRUD:
    """hannah#294: PresenceSource als eigenes Modell (user_id/source_type/reference/
    home_confidence/away_confidence/enabled) statt JSON-Blob im generischen Settings-
    System — analog zu BleTag/#115."""

    def test_create_and_get(self, manager, db):
        user_id = _create_user(db)

        created = manager.create_source(user_id, "ble_tag", "AA:BB", 0.3, 1.0)

        assert created is not None
        records = manager.get_source_records()
        assert len(records) == 1
        assert records[0]["user_id"] == user_id
        assert records[0]["source_type"] == "ble_tag"
        assert records[0]["reference"] == "AA:BB"
        assert records[0]["home_confidence"] == 0.3
        assert records[0]["away_confidence"] == 1.0
        assert records[0]["enabled"] == 1

    def test_create_disabled(self, manager, db):
        user_id = _create_user(db)

        created = manager.create_source(user_id, "iobroker_state", "some.state", 1.0, 0.3, enabled=False)

        assert created["enabled"] == 0

    def test_update(self, manager, db):
        user_id = _create_user(db)
        created = manager.create_source(user_id, "ble_tag", "AA:BB", 0.3, 1.0)

        ok = manager.update_source(created["id"], user_id, "ble_tag", "11:22", 0.5, 0.8, True)

        assert ok is True
        records = manager.get_source_records()
        assert records[0]["reference"] == "11:22"
        assert records[0]["home_confidence"] == 0.5
        assert records[0]["away_confidence"] == 0.8

    def test_update_not_found(self, manager, db):
        user_id = _create_user(db)
        assert manager.update_source(999, user_id, "ble_tag", "x", 1.0, 1.0, True) is False

    def test_delete(self, manager, db):
        user_id = _create_user(db)
        created = manager.create_source(user_id, "ble_tag", "AA:BB", 0.3, 1.0)

        assert manager.delete_source(created["id"]) is True
        assert manager.get_source_records() == []

    def test_delete_not_found(self, manager):
        assert manager.delete_source(999) is False

    def test_delete_cascades_when_user_deleted(self, manager, db):
        """presence_sources.user_id hat ON DELETE CASCADE (wie ble_tags/messages) —
        eine gelöschte Person nimmt ihre Presence-Quellen mit."""
        user_id = _create_user(db)
        manager.create_source(user_id, "ble_tag", "AA:BB", 0.3, 1.0)

        User.get(db(), id=user_id).delete()

        assert manager.get_source_records() == []
