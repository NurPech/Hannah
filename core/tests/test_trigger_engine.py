import datetime as datetime_module
import os

import pytest

import hannah.utils.db as db_module
from hannah import trigger_engine as trigger_engine_module
from hannah.trigger_engine import TriggerEngine


@pytest.fixture
def engine(tmp_path):
    """Echte (nicht gemockte) TriggerEngine gegen eine Wegwerf-SQLite-DB — gleiches
    Muster wie tests/test_room_manager.py."""
    db_module.DB_PATH = os.path.join(str(tmp_path), "h.db")
    db_module.init_db()
    eng = TriggerEngine(
        db_module.get_db,
        announce_fn=lambda room, text: eng.announced.append((room, text)),
        set_state_fn=lambda state_id, value: eng.states_set.append((state_id, value)),
        state_cache_path=os.path.join(str(tmp_path), "state_cache.json"),
    )
    eng.announced = []
    eng.states_set = []
    return eng


def _create(engine, id, when, *, actions=None, say="", unless=None, cooldown=0):
    if unless is not None:
        when = dict(when)
        when["unless"] = unless
    ok = engine.create_trigger(id, when, None, [], actions or [], say, "", False, "all", cooldown, "")
    assert ok
    return ok


class _FixedDatetime:
    """Ersetzt trigger_engine.datetime für _check_time_triggers()-Tests."""

    def __init__(self, year, month, day, hour, minute):
        self._fixed = datetime_module.datetime(year, month, day, hour, minute)

    def now(self):
        return self._fixed


class TestWhenAltFormatRegression:
    """Einzelnes when-Dict (Alt-Format) muss sich exakt wie vorher verhalten."""

    def test_single_state_condition_fires(self, engine):
        _create(engine, "t1", {"state": "s1", "value": True}, say="Hallo")

        engine.on_state_update("s1", "true")

        assert engine.announced == [("all", "Hallo")]

    def test_single_time_condition_fires(self, engine, monkeypatch):
        _create(engine, "t1", {"time": "23:00"}, say="Gute Nacht")
        monkeypatch.setattr(trigger_engine_module, "datetime", _FixedDatetime(2026, 6, 28, 23, 0))

        engine._check_time_triggers()

        assert engine.announced == [("all", "Gute Nacht")]

    def test_also_plain_list_is_and(self, engine):
        _create(engine, "t1", {
            "state": "s1", "value": True,
            "also": [{"state": "a1", "value": True}, {"state": "a2", "value": True}],
        }, say="Beide")

        engine.on_state_update("a1", "true")
        engine.on_state_update("s1", "true")
        assert engine.announced == []  # a2 fehlt noch

        engine.on_state_update("a2", "true")
        engine.on_state_update("s1", "false")
        engine.on_state_update("s1", "true")
        assert engine.announced == [("all", "Beide")]

    def test_bare_say_without_actions(self, engine):
        _create(engine, "t1", {"state": "s1", "value": True}, say="Nur Say")

        engine.on_state_update("s1", "true")

        assert engine.announced == [("all", "Nur Say")]


class TestWhenOrList:
    def test_fires_on_either_condition(self, engine):
        _create(engine, "t1", [{"state": "s1", "value": True}, {"state": "s2", "value": True}], say="Fire")

        engine.on_state_update("s1", "true")
        engine.on_state_update("s2", "true")

        assert engine.announced == [("all", "Fire"), ("all", "Fire")]

    def test_time_or_list_fires_on_either(self, engine, monkeypatch):
        _create(engine, "t1", [{"time": "07:00"}, {"time": "23:00"}], say="Zeit")
        monkeypatch.setattr(trigger_engine_module, "datetime", _FixedDatetime(2026, 6, 28, 23, 0))

        engine._check_time_triggers()

        assert engine.announced == [("all", "Zeit")]


class TestTimeTriggerAlso:
    """_check_time_triggers() muss 'also' genauso auswerten wie on_state_update()/match_phrase() (#146)."""

    def test_time_condition_with_also_requires_state_match(self, engine, monkeypatch):
        _create(engine, "t1", {
            "time": "10:00", "also": [{"state": "a1", "value": True}],
        }, say="Zeit und Zustand")
        monkeypatch.setattr(trigger_engine_module, "datetime", _FixedDatetime(2026, 6, 28, 10, 0))

        engine._check_time_triggers()
        assert engine.announced == []  # a1 noch nie gesetzt

        engine.on_state_update("a1", "true")
        engine._check_time_triggers()
        assert engine.announced == [("all", "Zeit und Zustand")]


class TestTimeStateSiblingMerge:
    """Ein when mit time- und state-Geschwister-Bedingungen (statt explizitem 'also')
    muss die state-Bedingung(en) automatisch UND-verknüpfen, nicht als unabhängige
    OR-Alternative behandeln — genau das Format, das die WebUI aktuell erzeugt (#147)."""

    def test_sibling_state_condition_no_longer_fires_independently(self, engine, monkeypatch):
        _create(engine, "t1", [
            {"time": "10:00", "days": ["mon", "tue", "wed", "thu", "fri"]},
            {"state": "cat_feeded", "value": "false"},
        ], say="Katze füttern")
        monkeypatch.setattr(trigger_engine_module, "datetime", _FixedDatetime(2026, 6, 29, 10, 0))  # Montag

        # State wird gesetzt, aber es ist nicht 10 Uhr laut Tick-Loop-Check -> darf
        # nicht mehr unabhängig über on_state_update feuern.
        engine.on_state_update("cat_feeded", "false")
        assert engine.announced == []

        engine._check_time_triggers()
        assert engine.announced == [("all", "Katze füttern")]

    def test_time_check_requires_state_to_match_too(self, engine, monkeypatch):
        _create(engine, "t1", [
            {"time": "10:00"},
            {"state": "cat_feeded", "value": "false"},
        ], say="Katze füttern")
        monkeypatch.setattr(trigger_engine_module, "datetime", _FixedDatetime(2026, 6, 29, 10, 0))

        engine._check_time_triggers()
        assert engine.announced == []  # cat_feeded noch nie gesetzt -> also-Check schlägt fehl

    def test_multiple_state_siblings_stay_or_linked_against_each_other(self, engine, monkeypatch):
        _create(engine, "t1", [
            {"time": "10:00"},
            {"state": "s1", "value": True},
            {"state": "s2", "value": True},
        ], say="Zeit oder-States")
        monkeypatch.setattr(trigger_engine_module, "datetime", _FixedDatetime(2026, 6, 29, 10, 0))

        engine.on_state_update("s1", "true")
        engine._check_time_triggers()
        assert engine.announced == [("all", "Zeit oder-States")]


class TestAlsoOpFormat:
    def test_op_or_fires_if_any_matches(self, engine):
        _create(engine, "t1", {
            "state": "s1", "value": True,
            "also": {"op": "or", "conditions": [{"state": "a1", "value": True}, {"state": "a2", "value": True}]},
        }, say="Or-Match")

        engine.on_state_update("a1", "true")
        engine.on_state_update("s1", "true")

        assert engine.announced == [("all", "Or-Match")]

    def test_op_and_requires_all(self, engine):
        _create(engine, "t1", {
            "state": "s1", "value": True,
            "also": {"op": "and", "conditions": [{"state": "a1", "value": True}, {"state": "a2", "value": True}]},
        }, say="And-Match")

        engine.on_state_update("a1", "true")
        engine.on_state_update("s1", "true")
        assert engine.announced == []  # a2 fehlt noch

        engine.on_state_update("a2", "true")
        engine.on_state_update("s1", "false")
        engine.on_state_update("s1", "true")
        assert engine.announced == [("all", "And-Match")]


class TestActionsList:
    def test_multiple_actions_executed_in_order(self, engine):
        _create(engine, "t1", {"state": "s1", "value": True}, say="Sollte ignoriert werden", actions=[
            {"say": "Eins"},
            {"set_state": {"id": "x.y", "value": False}},
            {"say": "Zwei", "room": "kueche"},
        ])

        engine.on_state_update("s1", "true")

        assert engine.announced == [("all", "Eins"), ("kueche", "Zwei")]
        assert engine.states_set == [("x.y", False)]

    def test_empty_actions_falls_back_to_say(self, engine):
        _create(engine, "t1", {"state": "s1", "value": True}, say="Legacy", actions=[])

        engine.on_state_update("s1", "true")

        assert engine.announced == [("all", "Legacy")]


class TestUnlessUnchanged:
    def test_unless_blocks_firing(self, engine):
        _create(engine, "t1", {"state": "s1", "value": True}, say="Bedingt",
                unless={"state": "u1", "value": True})

        engine.on_state_update("u1", "true")
        engine.on_state_update("s1", "true")
        assert engine.announced == []

        engine.on_state_update("u1", "false")
        engine.on_state_update("s1", "false")
        engine.on_state_update("s1", "true")
        assert engine.announced == [("all", "Bedingt")]


class TestPhraseTrigger:
    """#139 — when.phrase ersetzt die alte RoutineManager.match()-Funktionalität:
    synchroner Substring-Match, Actions als Seiteneffekt, say als direkte Antwort."""

    def test_substring_match_returns_say_as_reply(self, engine):
        _create(engine, "t1", {"phrase": "gute nacht"}, say="Gute Nacht.")

        assert engine.match_phrase("sag mal gute nacht zu mir") == "Gute Nacht."

    def test_no_match_returns_none(self, engine):
        _create(engine, "t1", {"phrase": "gute nacht"}, say="Gute Nacht.")

        assert engine.match_phrase("wie ist das Wetter") is None

    def test_matches_capitalized_phrase_against_lowercased_transcript(self, engine):
        """Regression: die konfigurierte Phrase wurde nie durch _normalize() gejagt,
        nur der erkannte Text — ein Trigger mit großgeschriebener Phrase (normal für
        deutsche Substantive, z.B. 'Schlafzimmer aus') matchte deshalb nie, unabhängig
        von Satzzeichen. STT liefert immer klein+Punkt normalisierten Text."""
        _create(engine, "t1", {"phrase": "Schlafzimmer aus"}, say="Ok.")

        assert engine.match_phrase("Schlafzimmer aus.") == "Ok."

    def test_or_list_fires_on_either_phrase(self, engine):
        ok = engine.create_trigger(
            "t1", [{"phrase": "nachtlicht"}, {"phrase": "nacht licht"}], None, [], [],
            "Nachtlicht aktiviert.", "", False, "all", 0, "",
        )
        assert ok

        assert engine.match_phrase("mach das nacht licht an") == "Nachtlicht aktiviert."

    def test_fires_every_time_no_cooldown(self, engine):
        _create(engine, "t1", {"phrase": "regenbogen"}, say="Regenbogen-Modus aktiviert.")

        first = engine.match_phrase("regenbogen")
        second = engine.match_phrase("regenbogen")

        assert first == second == "Regenbogen-Modus aktiviert."

    def test_actions_executed_as_side_effect(self, engine):
        _create(engine, "t1", {"phrase": "gute nacht"}, say="Gute Nacht.", actions=[
            {"set_state": {"id": "javascript.0.virtualDevice.Licht.EG.Flur.on", "value": False}},
        ])

        reply = engine.match_phrase("gute nacht")

        assert reply == "Gute Nacht."
        assert engine.states_set == [("javascript.0.virtualDevice.Licht.EG.Flur.on", False)]

    def test_also_condition_must_match(self, engine):
        when = {"phrase": "gute nacht", "also": {"state": "0_userdata.0.zuhause", "value": True}}
        _create(engine, "t1", when, say="Gute Nacht.")

        assert engine.match_phrase("gute nacht") is None  # State unbekannt -> also nicht erfüllt

        engine.on_state_update("0_userdata.0.zuhause", "true")
        assert engine.match_phrase("gute nacht") == "Gute Nacht."

    def test_unless_condition_blocks(self, engine):
        _create(engine, "t1", {"phrase": "gute nacht"}, say="Gute Nacht.",
                unless={"state": "0_userdata.0.abwesend", "value": True})
        engine.on_state_update("0_userdata.0.abwesend", "true")

        assert engine.match_phrase("gute nacht") is None

    def test_no_say_returns_default_ok(self, engine):
        _create(engine, "t1", {"phrase": "nachtlicht"}, say="", actions=[
            {"set_state": {"id": "x.y", "value": True}},
        ])

        assert engine.match_phrase("nachtlicht") == "Ok."

    def test_rephrase_applied_to_say(self, tmp_path):
        import hannah.utils.db as db_module
        db_module.DB_PATH = str(tmp_path / "h2.db")
        db_module.init_db()
        eng = TriggerEngine(
            db_module.get_db,
            announce_fn=lambda room, text: None,
            rephrase_fn=lambda text: f"[rephrased] {text}",
            state_cache_path=str(tmp_path / "state_cache2.json"),
        )
        eng.create_trigger("t1", {"phrase": "gute nacht"}, None, [], [], "Gute Nacht.",
                            "", True, "all", 0, "")

        assert eng.match_phrase("gute nacht") == "[rephrased] Gute Nacht."


class TestPersistentStateCache:
    """State-Cache überlebt einen TriggerEngine-Neustart (#141) — kein falsches
    Feuern, wenn der erste beobachtete Wert nach dem Neustart unverändert ist."""

    def _make_engine(self, tmp_path, cache_path):
        db_module.DB_PATH = str(tmp_path / "h.db")
        db_module.init_db()
        eng = TriggerEngine(
            db_module.get_db,
            announce_fn=lambda room, text: eng.announced.append((room, text)),
            state_cache_path=cache_path,
        )
        eng.announced = []
        return eng

    def test_restart_with_unchanged_value_does_not_fire(self, tmp_path):
        cache_path = str(tmp_path / "cache.json")
        eng1 = self._make_engine(tmp_path, cache_path)
        _create(eng1, "t1", {"state": "s1", "value": False}, say="Licht aus!")
        eng1.on_state_update("s1", "true")   # Ausgangszustand: an
        eng1.on_state_update("s1", "false")  # echte Transition -> aus, feuert
        assert eng1.announced == [("all", "Licht aus!")]

        # "Neustart": neue Engine-Instanz gegen dieselbe Cache-Datei
        eng2 = self._make_engine(tmp_path, cache_path)
        eng2.create_trigger("t1", {"state": "s1", "value": False}, None, [], [], "Licht aus!",
                             "", False, "all", 0, "")
        eng2.on_state_update("s1", "false")  # unverändert -> darf NICHT feuern

        assert eng2.announced == []

    def test_restart_with_real_change_still_fires(self, tmp_path):
        cache_path = str(tmp_path / "cache.json")
        eng1 = self._make_engine(tmp_path, cache_path)
        _create(eng1, "t1", {"state": "s1", "value": False}, say="Licht aus!")
        eng1.on_state_update("s1", "true")  # Ausgangszustand: an, persistiert

        eng2 = self._make_engine(tmp_path, cache_path)
        eng2.create_trigger("t1", {"state": "s1", "value": False}, None, [], [], "Licht aus!",
                             "", False, "all", 0, "")
        eng2.on_state_update("s1", "false")  # echte Transition an->aus, muss feuern

        assert eng2.announced == [("all", "Licht aus!")]

    def test_missing_cache_file_is_not_an_error(self, tmp_path):
        eng = self._make_engine(tmp_path, str(tmp_path / "does_not_exist.json"))
        assert eng._state_cache == {}


class TestSeedFromSnapshot:
    """seed_from_snapshot() aktualisiert den Cache still, ohne Trigger zu feuern (#141)."""

    def test_seed_does_not_fire_trigger(self, engine):
        _create(engine, "t1", {"state": "s1", "value": False}, say="Licht aus!")

        engine.seed_from_snapshot({"s1": "false"})

        assert engine.announced == []
        with engine._lock:
            assert engine._state_cache["s1"] is False

    def test_subsequent_unchanged_update_after_seed_does_not_fire(self, engine):
        _create(engine, "t1", {"state": "s1", "value": False}, say="Licht aus!")

        engine.seed_from_snapshot({"s1": "false"})
        engine.on_state_update("s1", "false")  # unverändert ggü. Snapshot -> darf nicht feuern

        assert engine.announced == []

    def test_subsequent_real_change_after_seed_fires(self, engine):
        _create(engine, "t1", {"state": "s1", "value": False}, say="Licht aus!")

        engine.seed_from_snapshot({"s1": "true"})  # Ausgangszustand laut Snapshot: an
        engine.on_state_update("s1", "false")  # echte Transition an->aus, muss feuern

        assert engine.announced == [("all", "Licht aus!")]

    def test_empty_snapshot_is_noop(self, engine):
        engine.seed_from_snapshot({})
        assert engine._state_cache == {}

    def test_seed_persists_to_disk(self, tmp_path):
        db_module.DB_PATH = str(tmp_path / "h.db")
        db_module.init_db()
        cache_path = str(tmp_path / "cache.json")
        eng = TriggerEngine(db_module.get_db, announce_fn=lambda room, text: None,
                             state_cache_path=cache_path)

        eng.seed_from_snapshot({"s1": "true", "s2": "42"})

        import json
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
        assert data == {"s1": True, "s2": 42}
