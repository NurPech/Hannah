import os

import numpy as np
from pyorm import Database, create_table

import hannah.activity_log as activity_log
from hannah.models.activity_log import ActivityLog
from hannah.nlu import Intent
from hannah.utils.activity_db import ACTIVITY_TABLE


def _sqlite_activity_db(tmp_path, monkeypatch):
    """Activity-Log ist MySQL-only in Produktion, aber pyorm abstrahiert die
    Query-Ebene vollständig — Tests laufen daher dialekt-agnostisch gegen eine
    SQLite-Datei statt einen echten MySQL-Server zu brauchen (analog zu den
    pyorm-eigenen Tests).

    log_activity() schließt seine Connection nach jedem Aufruf (wie get_db() liefert
    get_activity_db() in Produktion pro Aufruf eine frische Connection) — die Mock-
    Fixture muss das nachbilden, sonst ist die Connection bei der Assertion schon zu.
    """
    path = os.path.join(str(tmp_path), "activity.db")
    create_table(Database.sqlite(path), ACTIVITY_TABLE)
    monkeypatch.setattr(activity_log, "get_activity_db", lambda: Database.sqlite(path))
    return path


class TestLogActivity:
    def test_writes_row_with_intent_metadata(self, tmp_path, monkeypatch):
        path = _sqlite_activity_db(tmp_path, monkeypatch)
        intent = Intent(name="TurnOn", room="Wohnzimmer", device="Licht")

        activity_log.log_activity(
            channel_type="satellite", channel_id="kueche-esp", user_id="3",
            raw_text="Mach das Licht an", intent=intent, answer_text="OK, 1 Gerät geschaltet.",
        )

        rows = ActivityLog.select(Database.sqlite(path)).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.channel_type == "satellite"
        assert row.channel_id == "kueche-esp"
        assert str(row.user_id) == "3"
        assert row.raw_text == "Mach das Licht an"
        assert row.intent_name == "TurnOn"
        assert row.intent_meta["room"] == "Wohnzimmer"
        assert row.intent_meta["device"] == "Licht"
        assert row.answer_text == "OK, 1 Gerät geschaltet."
        assert row.audio_path is None
        assert row.ts is not None

    def test_without_intent_stores_empty_metadata(self, tmp_path, monkeypatch):
        path = _sqlite_activity_db(tmp_path, monkeypatch)

        activity_log.log_activity(channel_type="iobroker", raw_text="Wetter?", answer_text="Sonnig.")

        row = ActivityLog.select(Database.sqlite(path)).all()[0]
        assert row.intent_name is None
        assert row.intent_meta == {}

    def test_with_audio_array_writes_wav_and_stores_path(self, tmp_path, monkeypatch):
        path = _sqlite_activity_db(tmp_path, monkeypatch)
        activity_log.configure(str(tmp_path / "audio"))
        audio = np.zeros(1600, dtype=np.float32)

        activity_log.log_activity(
            channel_type="satellite", channel_id="kueche-esp", raw_text="x", answer_text="y",
            audio_array=audio, sample_rate=16000,
        )

        row = ActivityLog.select(Database.sqlite(path)).all()[0]
        assert row.audio_path is not None
        assert os.path.isfile(row.audio_path)

    def test_noop_when_not_configured(self, monkeypatch):
        """get_activity_db() gibt None zurück, wenn kein Host konfiguriert ist —
        log_activity() darf dann nichts tun und keinesfalls werfen."""
        monkeypatch.setattr(activity_log, "get_activity_db", lambda: None)

        activity_log.log_activity(channel_type="iobroker", raw_text="x", answer_text="y")

    def test_db_error_is_swallowed(self, tmp_path, monkeypatch):
        """Monitoring darf die Sprachpipeline nie stören — ein Schreibfehler wird
        geloggt, nicht weitergeworfen."""
        _sqlite_activity_db(tmp_path, monkeypatch)
        closed_db = Database.sqlite(os.path.join(str(tmp_path), "activity.db"))
        closed_db.close()  # nachfolgende execute()-Aufrufe schlagen jetzt fehl
        monkeypatch.setattr(activity_log, "get_activity_db", lambda: closed_db)

        activity_log.log_activity(channel_type="satellite", raw_text="x", answer_text="y")


def _seed(db, **kwargs):
    defaults = dict(
        channel_type="grpc_text", channel_id="", user_id=None,
        raw_text="hi", answer_text="ok",
    )
    defaults.update(kwargs)
    return ActivityLog.create(db, **defaults)


class _FakeSatellite:
    def __init__(self, device_id):
        self.device_id = device_id


class _FakeUser:
    def __init__(self, id, trust_level, satellites=()):
        self.id = id
        self.trust_level = trust_level
        self.satellites = list(satellites)


class _FakeUserManager:
    """Stellt nur die zwei Methoden bereit, die ActivityLogManager tatsächlich braucht —
    kein echtes hannah.db/SatelliteManager-Setup nötig für diese Tests."""

    def __init__(self, users):
        self._users = {u.id: u for u in users}

    def get_user_by_id(self, user_id):
        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            return None
        return self._users.get(user_id)


class TestActivityLogManagerListActivity:
    def test_own_direct_entries_visible(self, tmp_path, monkeypatch):
        path = _sqlite_activity_db(tmp_path, monkeypatch)
        db = Database.sqlite(path)
        _seed(db, user_id=1)
        db.close()

        mgr = activity_log.ActivityLogManager(_FakeUserManager([_FakeUser(1, trust_level=0)]))
        entries, has_more = mgr.list_activity(requestor_id=1, filter_user_id=0, page_size=10, before_id=0)

        assert len(entries) == 1
        assert has_more is False

    def test_other_users_direct_entry_invisible(self, tmp_path, monkeypatch):
        path = _sqlite_activity_db(tmp_path, monkeypatch)
        db = Database.sqlite(path)
        _seed(db, user_id=2)
        db.close()

        mgr = activity_log.ActivityLogManager(
            _FakeUserManager([_FakeUser(1, trust_level=0), _FakeUser(2, trust_level=0)])
        )
        entries, _ = mgr.list_activity(requestor_id=1, filter_user_id=0, page_size=10, before_id=0)

        assert entries == []

    def test_satellite_owner_fallback_visible_when_unattributed(self, tmp_path, monkeypatch):
        path = _sqlite_activity_db(tmp_path, monkeypatch)
        db = Database.sqlite(path)
        _seed(db, channel_type="satellite", channel_id="dev1", user_id=None)
        db.close()

        owner = _FakeUser(1, trust_level=0, satellites=[_FakeSatellite("dev1")])
        mgr = activity_log.ActivityLogManager(_FakeUserManager([owner]))
        entries, _ = mgr.list_activity(requestor_id=1, filter_user_id=0, page_size=10, before_id=0)

        assert len(entries) == 1

    def test_satellite_owner_fallback_hidden_once_attributed_to_other_user(self, tmp_path, monkeypatch):
        path = _sqlite_activity_db(tmp_path, monkeypatch)
        db = Database.sqlite(path)
        _seed(db, channel_type="satellite", channel_id="dev1", user_id=2)
        db.close()

        owner = _FakeUser(1, trust_level=0, satellites=[_FakeSatellite("dev1")])
        other = _FakeUser(2, trust_level=0)
        mgr = activity_log.ActivityLogManager(_FakeUserManager([owner, other]))
        entries, _ = mgr.list_activity(requestor_id=1, filter_user_id=0, page_size=10, before_id=0)

        assert entries == []

    def test_satellite_fallback_does_not_apply_to_non_satellite_channels(self, tmp_path, monkeypatch):
        """Regel (b) gilt nur für channel_type='satellite' — Telegram trägt user_id
        immer direkt, andere Kanäle senden per Design kein Audio (siehe Plan)."""
        path = _sqlite_activity_db(tmp_path, monkeypatch)
        db = Database.sqlite(path)
        _seed(db, channel_type="telegram", channel_id="dev1", user_id=None)
        db.close()

        owner = _FakeUser(1, trust_level=0, satellites=[_FakeSatellite("dev1")])
        mgr = activity_log.ActivityLogManager(_FakeUserManager([owner]))
        entries, _ = mgr.list_activity(requestor_id=1, filter_user_id=0, page_size=10, before_id=0)

        assert entries == []

    def test_trust10_sees_everything_by_default(self, tmp_path, monkeypatch):
        path = _sqlite_activity_db(tmp_path, monkeypatch)
        db = Database.sqlite(path)
        _seed(db, user_id=1)
        _seed(db, user_id=2)
        db.close()

        mgr = activity_log.ActivityLogManager(_FakeUserManager([_FakeUser(99, trust_level=10)]))
        entries, _ = mgr.list_activity(requestor_id=99, filter_user_id=0, page_size=10, before_id=0)

        assert len(entries) == 2

    def test_trust10_filter_user_id_applies_same_fallback_logic(self, tmp_path, monkeypatch):
        path = _sqlite_activity_db(tmp_path, monkeypatch)
        db = Database.sqlite(path)
        _seed(db, channel_type="satellite", channel_id="dev1", user_id=None)  # sichtbar via Ziel-Users Satelliten-Fallback
        _seed(db, user_id=2)  # gehört einem anderen User, nicht dem Ziel
        db.close()

        target = _FakeUser(1, trust_level=0, satellites=[_FakeSatellite("dev1")])
        admin = _FakeUser(99, trust_level=10)
        mgr = activity_log.ActivityLogManager(_FakeUserManager([admin, target]))
        entries, _ = mgr.list_activity(requestor_id=99, filter_user_id=1, page_size=10, before_id=0)

        assert len(entries) == 1
        assert entries[0]["channel_id"] == "dev1"

    def test_pagination_cursor_and_has_more(self, tmp_path, monkeypatch):
        path = _sqlite_activity_db(tmp_path, monkeypatch)
        db = Database.sqlite(path)
        ids = [_seed(db, user_id=1).id for _ in range(5)]
        db.close()

        mgr = activity_log.ActivityLogManager(_FakeUserManager([_FakeUser(1, trust_level=0)]))

        page1, more1 = mgr.list_activity(requestor_id=1, filter_user_id=0, page_size=2, before_id=0)
        assert [e["id"] for e in page1] == sorted(ids, reverse=True)[:2]
        assert more1 is True

        page2, more2 = mgr.list_activity(requestor_id=1, filter_user_id=0, page_size=2, before_id=page1[-1]["id"])
        assert len(page2) == 2
        assert more2 is True

        page3, more3 = mgr.list_activity(requestor_id=1, filter_user_id=0, page_size=2, before_id=page2[-1]["id"])
        assert len(page3) == 1
        assert more3 is False


class TestActivityLogManagerAudio:
    def test_get_audio_chunks_returns_none_when_unauthorized(self, tmp_path, monkeypatch):
        path = _sqlite_activity_db(tmp_path, monkeypatch)
        db = Database.sqlite(path)
        row = _seed(db, user_id=2, audio_path="/does/not/matter")
        db.close()

        mgr = activity_log.ActivityLogManager(
            _FakeUserManager([_FakeUser(1, trust_level=0), _FakeUser(2, trust_level=0)])
        )

        assert mgr.get_audio_chunks(requestor_id=1, activity_log_id=row.id) is None

    def test_get_audio_chunks_returns_none_without_audio(self, tmp_path, monkeypatch):
        path = _sqlite_activity_db(tmp_path, monkeypatch)
        db = Database.sqlite(path)
        row = _seed(db, user_id=1, audio_path=None)
        db.close()

        mgr = activity_log.ActivityLogManager(_FakeUserManager([_FakeUser(1, trust_level=0)]))

        assert mgr.get_audio_chunks(requestor_id=1, activity_log_id=row.id) is None

    def test_get_audio_chunks_streams_pcm_for_authorized_entry(self, tmp_path, monkeypatch):
        path = _sqlite_activity_db(tmp_path, monkeypatch)
        activity_log.configure(str(tmp_path / "audio"))
        audio = np.zeros(1600, dtype=np.float32)
        wav_path = activity_log.save_audio_wav(audio, 16000, "test-entry")

        db = Database.sqlite(path)
        row = _seed(db, user_id=1, audio_path=wav_path)
        db.close()

        mgr = activity_log.ActivityLogManager(_FakeUserManager([_FakeUser(1, trust_level=0)]))
        chunks = list(mgr.get_audio_chunks(requestor_id=1, activity_log_id=row.id))

        assert chunks
        pcm, sample_rate = chunks[0]
        assert sample_rate == 16000
        assert isinstance(pcm, bytes)

    def test_get_audio_chunks_via_satellite_owner_fallback(self, tmp_path, monkeypatch):
        path = _sqlite_activity_db(tmp_path, monkeypatch)
        activity_log.configure(str(tmp_path / "audio"))
        audio = np.zeros(1600, dtype=np.float32)
        wav_path = activity_log.save_audio_wav(audio, 16000, "test-entry-2")

        db = Database.sqlite(path)
        row = _seed(db, channel_type="satellite", channel_id="dev1", user_id=None, audio_path=wav_path)
        db.close()

        owner = _FakeUser(1, trust_level=0, satellites=[_FakeSatellite("dev1")])
        mgr = activity_log.ActivityLogManager(_FakeUserManager([owner]))

        chunks = list(mgr.get_audio_chunks(requestor_id=1, activity_log_id=row.id))
        assert chunks
