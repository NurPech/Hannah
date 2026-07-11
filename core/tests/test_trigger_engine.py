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
        )
        eng.create_trigger("t1", {"phrase": "gute nacht"}, None, [], [], "Gute Nacht.",
                            "", True, "all", 0, "")

        assert eng.match_phrase("gute nacht") == "[rephrased] Gute Nacht."
