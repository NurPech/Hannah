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
