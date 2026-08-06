import datetime
import os
import threading

import pytest
from werkzeug.security import generate_password_hash

import hannah.utils.db as db_module
from hannah.alarms import AlarmManager
from hannah.models.satellite import Satellite
from hannah.models.user import User


@pytest.fixture
def db(tmp_path):
    """Echte (nicht gemockte) SQLite-DB — gleiches Muster wie test_satellite_manager.py."""
    db_module.DB_PATH = os.path.join(str(tmp_path), "h.db")
    db_module.init_db()
    return db_module.get_db


def _create_satellite(db, device_id="wz-sat") -> str:
    Satellite.create(db(), device_id=device_id, display_name=device_id)
    return device_id


def _create_user(db, username="leonie", trust_level=5) -> int:
    User.create(
        db(), username=username, display_name=username, email=f"{username}@example.com",
        password_hash=generate_password_hash("x"), trust_level=trust_level, mood_level=5,
        system_messages=0, type="roomie", is_active=1,
    )
    return User.get(db(), username=username).id


def _never_done(device, timeout):
    """Fake wait_playback_done_fn — blockt den _ringing_loop-Background-Thread für die
    Dauer des Tests (Event wird nie gesetzt), analog zum alten cycle_seconds=999-Trick:
    Tests rufen _ringing_cycle() direkt und synchron auf, der Loop-Thread soll währenddessen
    nicht dazwischenfunken."""
    return threading.Event().wait(timeout)


@pytest.fixture
def manager(db):
    fired, played, volumes, announced = [], [], [], []
    mgr = AlarmManager(
        db=db,
        on_fire=lambda record: fired.append(record),
        play_asset_fn=lambda device, asset: played.append((device, asset)),
        set_volume_fn=lambda device, level: volumes.append((device, level)),
        get_volume_fn=lambda device: 50,
        reset_playback_done_fn=lambda device: None,
        wait_playback_done_fn=_never_done,
        announce_fn=lambda device, text: announced.append((device, text)),
        cycle_seconds=999,  # real delay irrelevant — tests invoke cycles directly
    )
    mgr.fired = fired
    mgr.played = played
    mgr.volumes = volumes
    mgr.announced = announced
    return mgr


class TestCRUD:
    def test_create_and_get(self, db, manager):
        sat = _create_satellite(db)
        user_id = _create_user(db)

        record = manager.create_alarm(sat, "08:00", [0, 1, 2, 3, 4], None, user_id, label="Aufstehen")

        assert record["satellite_id"] == sat
        assert record["time"] == "08:00"
        assert record["weekdays"] == [0, 1, 2, 3, 4]
        assert record["label"] == "Aufstehen"
        assert [r["id"] for r in manager.get_alarm_records()] == [record["id"]]

    def test_create_one_off(self, db, manager):
        sat = _create_satellite(db)
        user_id = _create_user(db)
        tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()

        record = manager.create_alarm(sat, "08:00", None, tomorrow, user_id)

        assert record["weekdays"] is None
        assert record["one_shot_date"] == tomorrow

    def test_update(self, db, manager):
        sat = _create_satellite(db)
        user_id = _create_user(db)
        record = manager.create_alarm(sat, "08:00", [0], None, user_id)

        ok = manager.update_alarm(record["id"], sat, "09:00", [0, 1], [], None, True, label="Neu")

        assert ok is True
        updated = manager.get_alarm_records()[0]
        assert updated["time"] == "09:00"
        assert updated["weekdays"] == [0, 1]
        assert updated["label"] == "Neu"

    def test_update_unknown_returns_false(self, manager):
        assert manager.update_alarm(999, "sat", "08:00", None, [], None, True) is False

    def test_delete(self, db, manager):
        sat = _create_satellite(db)
        user_id = _create_user(db)
        record = manager.create_alarm(sat, "08:00", [0], None, user_id)

        ok = manager.delete_alarm(record["id"])

        assert ok is True
        assert manager.get_alarm_records() == []

    def test_delete_unknown_returns_false(self, manager):
        assert manager.delete_alarm(999) is False

    def test_satellite_delete_cascades(self, db, manager):
        """FK ON DELETE CASCADE — löscht der Satellit, verschwindet auch der Alarm."""
        sat = _create_satellite(db)
        user_id = _create_user(db)
        manager.create_alarm(sat, "08:00", [0], None, user_id)

        Satellite.get(db(), device_id=sat).delete()

        assert manager.get_alarm_records() == []

    def test_user_delete_cascades(self, db, manager):
        sat = _create_satellite(db)
        user_id = _create_user(db)
        manager.create_alarm(sat, "08:00", [0], None, user_id)

        User.get(db(), id=user_id).delete()

        assert manager.get_alarm_records() == []


class TestFindOccurrences:
    def test_recurring_matches_weekday_and_skips_excluded_date(self, db, manager):
        sat = _create_satellite(db)
        user_id = _create_user(db)
        record = manager.create_alarm(sat, "08:00", [0, 1, 2, 3, 4], None, user_id)
        monday = datetime.date(2026, 7, 6)  # ein Montag
        assert monday.weekday() == 0

        assert [r["id"] for r in manager.find_occurrences(sat, monday)] == [record["id"]]

        manager.skip_occurrence(record["id"], monday.isoformat())
        assert manager.find_occurrences(sat, monday) == []
        # nächster Dienstag bleibt unberührt
        tuesday = monday + datetime.timedelta(days=1)
        assert [r["id"] for r in manager.find_occurrences(sat, tuesday)] == [record["id"]]

    def test_one_off_matches_exact_date_only(self, db, manager):
        sat = _create_satellite(db)
        user_id = _create_user(db)
        target = datetime.date.today() + datetime.timedelta(days=1)
        record = manager.create_alarm(sat, "08:00", None, target.isoformat(), user_id)

        assert [r["id"] for r in manager.find_occurrences(sat, target)] == [record["id"]]
        assert manager.find_occurrences(sat, target + datetime.timedelta(days=1)) == []

    def test_no_satellite_filter_matches_across_satellites(self, db, manager):
        sat_a = _create_satellite(db, "sat-a")
        sat_b = _create_satellite(db, "sat-b")
        user_id = _create_user(db)
        target = datetime.date.today() + datetime.timedelta(days=1)
        a = manager.create_alarm(sat_a, "08:00", None, target.isoformat(), user_id)
        b = manager.create_alarm(sat_b, "08:00", None, target.isoformat(), user_id)

        matches = {r["id"] for r in manager.find_occurrences(None, target)}

        assert matches == {a["id"], b["id"]}


class TestComputeNextFire:
    def test_recurring_lands_on_matching_weekday_in_the_future(self, db, manager):
        sat = _create_satellite(db)
        user_id = _create_user(db)
        record = manager.create_alarm(sat, "08:00", [0], None, user_id)  # nur Montag

        next_fire = manager._compute_next_fire(manager.get_alarm_records()[0])

        assert next_fire is not None
        assert next_fire.weekday() == 0
        assert next_fire > datetime.datetime.now()

    def test_one_off_in_the_past_returns_none(self, db, manager):
        sat = _create_satellite(db)
        user_id = _create_user(db)
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        record = {"time": "08:00", "weekdays": None, "one_shot_date": yesterday, "skip_dates": []}

        assert manager._compute_next_fire(record) is None


class TestFireOneOff:
    def test_fire_calls_on_fire_and_deletes_row(self, db, manager):
        sat = _create_satellite(db)
        user_id = _create_user(db)
        tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        record = manager.create_alarm(sat, "08:00", None, tomorrow, user_id)

        manager._fire(record["id"])

        assert len(manager.fired) == 1
        assert manager.fired[0]["id"] == record["id"]
        assert manager.get_alarm_records() == []

    def test_fire_starts_ringing(self, db, manager):
        sat = _create_satellite(db)
        user_id = _create_user(db)
        tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        record = manager.create_alarm(sat, "08:00", None, tomorrow, user_id)

        manager._fire(record["id"])

        assert manager.is_ringing(sat) is True
        assert manager.played == [(sat, "alarm_ring")]


class TestFireRecurring:
    def test_fire_reschedules_instead_of_deleting(self, db, manager):
        sat = _create_satellite(db)
        user_id = _create_user(db)
        record = manager.create_alarm(sat, "08:00", list(range(7)), None, user_id)

        manager._fire(record["id"])

        assert len(manager.fired) == 1
        assert manager.get_alarm_records() != []
        assert record["id"] in manager._timers


class TestRinging:
    def test_start_stop_alternates_volume_and_restores_on_stop(self, db, manager):
        sat = _create_satellite(db)

        manager._start_ringing(sat)
        assert manager.is_ringing(sat) is True
        assert manager.volumes == [(sat, manager._volume_high)]
        assert manager.played == [(sat, "alarm_ring")]

        manager._ringing_cycle(sat)
        assert manager.volumes[-1] == (sat, manager._volume_low)

        manager._ringing_cycle(sat)
        assert manager.volumes[-1] == (sat, manager._volume_high)

        stopped = manager.stop_ringing(sat)

        assert stopped is True
        assert manager.is_ringing(sat) is False
        assert manager.volumes[-1] == (sat, 50)  # get_volume_fn's fixed pre-ring value

    def test_start_ringing_is_idempotent_per_device(self, db, manager):
        sat = _create_satellite(db)

        manager._start_ringing(sat)
        manager._start_ringing(sat)

        assert manager.played == [(sat, "alarm_ring")]  # nur ein Zyklus gestartet

    def test_stop_ringing_on_idle_device_returns_false(self, manager):
        assert manager.stop_ringing("unknown-sat") is False

    def test_ringing_devices_lists_active(self, db, manager):
        sat_a = _create_satellite(db, "sat-a")
        sat_b = _create_satellite(db, "sat-b")

        manager._start_ringing(sat_a)
        manager._start_ringing(sat_b)

        assert set(manager.ringing_devices()) == {sat_a, sat_b}


class TestPlayResultFallback:
    """#116: play_asset war Fire-and-Forget — ein Nack vom Satelliten (Asset nicht
    im Cache o.ä.) blieb unbemerkt und der Klingel-Loop feuerte still für immer
    weiter. on_play_result() schaltet stattdessen auf eine TTS-Ansage um."""

    def test_nack_switches_next_cycle_to_announce(self, db, manager):
        sat = _create_satellite(db)
        manager._start_ringing(sat)
        assert manager.played == [(sat, "alarm_ring")]

        manager.on_play_result(sat, "alarm_ring", ok=False)
        manager._ringing_cycle(sat)

        assert manager.played == [(sat, "alarm_ring")]  # kein weiterer Play-Versuch
        assert manager.announced == [(sat, manager._fallback_text)]

    def test_ack_keeps_playing_asset(self, db, manager):
        sat = _create_satellite(db)
        manager._start_ringing(sat)

        manager.on_play_result(sat, "alarm_ring", ok=True)
        manager._ringing_cycle(sat)

        assert manager.played == [(sat, "alarm_ring"), (sat, "alarm_ring")]
        assert manager.announced == []

    def test_nack_for_non_ringing_device_is_ignored(self, manager):
        manager.on_play_result("idle-sat", "alarm_ring", ok=False)

        assert manager.announced == []

    def test_nack_for_different_asset_is_ignored(self, db, manager):
        sat = _create_satellite(db)
        manager._start_ringing(sat)

        manager.on_play_result(sat, "timer_jingle", ok=False)
        manager._ringing_cycle(sat)

        assert manager.announced == []
        assert manager.played == [(sat, "alarm_ring"), (sat, "alarm_ring")]

    def test_stop_and_restart_resets_fallback(self, db, manager):
        sat = _create_satellite(db)
        manager._start_ringing(sat)
        manager.on_play_result(sat, "alarm_ring", ok=False)
        manager.stop_ringing(sat)

        manager._start_ringing(sat)

        assert manager.played[-1] == (sat, "alarm_ring")  # wieder Asset, nicht TTS

    def test_without_announce_fn_keeps_retrying_asset(self, db):
        """Ohne announce_fn (None) bleibt das alte Verhalten: play_asset wird trotz
        Nack weiter versucht statt auf eine TTS-Ansage umzuschalten."""
        played = []
        mgr = AlarmManager(
            db=db,
            on_fire=lambda record: None,
            play_asset_fn=lambda device, asset: played.append((device, asset)),
            set_volume_fn=lambda device, level: None,
            get_volume_fn=lambda device: 50,
            reset_playback_done_fn=lambda device: None,
            wait_playback_done_fn=_never_done,
            cycle_seconds=999,
        )
        sat = _create_satellite(db)
        mgr._start_ringing(sat)

        mgr.on_play_result(sat, "alarm_ring", ok=False)
        mgr._ringing_cycle(sat)

        assert played == [(sat, "alarm_ring"), (sat, "alarm_ring")]
