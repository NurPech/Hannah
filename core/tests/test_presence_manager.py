import os
import time

import pytest
from werkzeug.security import generate_password_hash

import hannah.utils.db as db_module
from hannah.presence_manager import PresenceManager
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


class _FakeUser:
    """Test-Double für hannah.models.user.User — presence_manager kennt nur die
    presence-Property, nicht arrival/departure-Events (die hängen an der echten
    User.presence-Property, siehe models/user.py, hier nicht relevant)."""
    def __init__(self, asleep=False):
        self.presence = False
        self.asleep = asleep


@pytest.fixture
def sources(db):
    return PresenceSourceManager(db)


class TestGetReferencedStateIds:
    def test_only_iobroker_state_sources(self, db, sources):
        user_id = _create_user(db)
        sources.create_source(user_id, "iobroker_state", "some.state.id", 1.0, 0.3)
        sources.create_source(user_id, "ble_tag", "AA:BB", 0.3, 1.0)

        pm = PresenceManager(db, user_lookup=lambda _uid: None)

        assert pm.get_referenced_state_ids() == {"some.state.id"}

    def test_ignores_disabled_sources(self, db, sources):
        user_id = _create_user(db)
        sources.create_source(user_id, "iobroker_state", "some.state.id", 1.0, 0.3, enabled=False)

        pm = PresenceManager(db, user_lookup=lambda _uid: None)

        assert pm.get_referenced_state_ids() == set()


class TestFusion:
    """hannah#294: relativer Vergleich (strongest_home vs. strongest_away) statt fixem
    Schwellwert — siehe presence_manager.py-Modul-Docstring für die Herleitung."""

    def _make(self, db, user_id, fake_user, **kwargs):
        return PresenceManager(db, user_lookup=lambda uid: fake_user if uid == user_id else None, **kwargs)

    def test_solo_ble_sighting_triggers_home_immediately(self, db, sources):
        """Reproduziert das heutige Verhalten: BLE alleine löst sofort 'home' aus,
        auch bei niedriger home_confidence (0.3), solange keine Quelle aktiv 'away' meldet."""
        user_id = _create_user(db)
        sources.create_source(user_id, "ble_tag", "AA:BB", 0.3, 1.0)
        fake = _FakeUser()
        pm = self._make(db, user_id, fake)

        pm.on_ble_sighting(user_id)

        assert fake.presence is True

    def test_tie_between_home_and_away_favors_home(self, db, sources):
        user_id = _create_user(db)
        sources.create_source(user_id, "ble_tag", "AA:BB", 0.3, 1.0)
        sources.create_source(user_id, "iobroker_state", "wlan.present", 1.0, 0.3)
        fake = _FakeUser()
        pm = self._make(db, user_id, fake, grace_period_seconds=999)

        pm.on_ble_sighting(user_id)  # BLE home_confidence=0.3, fresh
        pm.on_state_update("wlan.present", "false")  # WLAN away_confidence=0.3 -> tie

        assert fake.presence is True

    def test_stronger_away_signal_starts_grace_period_not_instant_away(self, db, sources):
        user_id = _create_user(db)
        sources.create_source(user_id, "iobroker_state", "wlan.present", 1.0, 0.3)
        fake = _FakeUser()
        pm = self._make(db, user_id, fake, grace_period_seconds=999)

        pm.on_state_update("wlan.present", "true")
        assert fake.presence is True

        pm.on_state_update("wlan.present", "false")
        # Away-Signal allein flippt nicht sofort — Grace-Period (hier absichtlich lang) muss ablaufen
        assert fake.presence is True

    def test_tristate_residents_value_asleep_counts_as_home(self, db, sources):
        """Residents-Adapter-Präsenzfeld: 0=weg, 1=wach, 2=schlafend — 1 UND 2 sind
        'zuhause'. Ein reiner true/false-Parser hätte "2" fälschlich als weg gewertet."""
        user_id = _create_user(db)
        sources.create_source(user_id, "iobroker_state", "residents.0.person.leonie.state", 1.0, 0.3)
        fake = _FakeUser()
        pm = self._make(db, user_id, fake, grace_period_seconds=999)

        pm.on_state_update("residents.0.person.leonie.state", "1")
        assert fake.presence is True

        pm.on_state_update("residents.0.person.leonie.state", "2")
        assert fake.presence is True  # schlafend bleibt zuhause

    def test_tristate_residents_value_zero_counts_as_away(self, db, sources):
        user_id = _create_user(db)
        sources.create_source(user_id, "iobroker_state", "residents.0.person.leonie.state", 1.0, 0.3)
        fake = _FakeUser()
        pm = self._make(db, user_id, fake, grace_period_seconds=0.2)

        pm.on_state_update("residents.0.person.leonie.state", "1")
        pm.on_state_update("residents.0.person.leonie.state", "0")
        assert fake.presence is True  # Grace-Period läuft gerade erst an

        time.sleep(0.3)
        pm.tick()

        assert fake.presence is False

    def test_away_after_grace_period_elapses(self, db, sources):
        user_id = _create_user(db)
        sources.create_source(user_id, "iobroker_state", "wlan.present", 1.0, 0.3)
        fake = _FakeUser()
        pm = self._make(db, user_id, fake, grace_period_seconds=0.2)

        pm.on_state_update("wlan.present", "true")
        pm.on_state_update("wlan.present", "false")
        assert fake.presence is True  # Grace-Period läuft gerade erst an

        time.sleep(0.3)
        pm.tick()

        assert fake.presence is False

    def test_asleep_user_never_gets_flipped_to_away(self, db, sources):
        """"Ich gehe schlafen" läuft über residents.set_user_asleep() direkt, nicht über
        user.presence/user.asleep — die Fusion weiß nichts davon außer über user.asleep.
        Ein nächtlicher Signalausfall darf den Night-Flag nicht auf "weg" zurücksetzen."""
        user_id = _create_user(db)
        sources.create_source(user_id, "iobroker_state", "wlan.present", 1.0, 0.3)
        fake = _FakeUser(asleep=True)
        fake.presence = True  # war zuhause, bevor sie schlafen ging
        pm = self._make(db, user_id, fake, grace_period_seconds=0.2)

        pm.on_state_update("wlan.present", "true")
        pm.on_state_update("wlan.present", "false")  # Handy im Doze-Mode

        time.sleep(0.3)
        pm.tick()

        assert fake.presence is True  # bleibt "home", trotz abgelaufener Grace-Period

    def test_stale_ble_reading_counts_as_away(self, db, sources):
        """BLE ist push-only (keine explizite 'weg'-Meldung) — eine Sichtung, die älter
        als ble_staleness_seconds ist, zählt für strongest_away statt strongest_home."""
        user_id = _create_user(db)
        sources.create_source(user_id, "ble_tag", "AA:BB", 0.3, 1.0)
        fake = _FakeUser()
        pm = self._make(db, user_id, fake, grace_period_seconds=0.2, ble_staleness_seconds=0.2)

        pm.on_ble_sighting(user_id)
        assert fake.presence is True

        time.sleep(0.3)
        pm.tick()  # BLE jetzt stale -> instantaneous_home wird False, Grace-Period startet
        assert fake.presence is True  # Grace-Period läuft gerade erst an

        time.sleep(0.3)
        pm.tick()  # Grace-Period abgelaufen

        assert fake.presence is False

    def test_fresh_ble_sighting_cancels_pending_away(self, db, sources):
        user_id = _create_user(db)
        sources.create_source(user_id, "iobroker_state", "wlan.present", 1.0, 0.3)
        sources.create_source(user_id, "ble_tag", "AA:BB", 0.3, 1.0)
        fake = _FakeUser()
        pm = self._make(db, user_id, fake, grace_period_seconds=0.2)

        pm.on_state_update("wlan.present", "true")
        pm.on_state_update("wlan.present", "false")  # Grace-Period-Timer startet

        time.sleep(0.1)
        pm.on_ble_sighting(user_id)  # frisches Home-Signal -> Timer wird zurückgesetzt

        time.sleep(0.15)  # insgesamt > 0.2s seit dem ersten Away-Signal, aber < 0.2s seit BLE-Sichtung
        pm.tick()

        assert fake.presence is True

    def test_no_sources_is_a_noop(self, db):
        user_id = _create_user(db)
        fake = _FakeUser()
        pm = self._make(db, user_id, fake)

        pm.on_ble_sighting(user_id)

        assert fake.presence is False  # unverändert, kein Fehler
