import datetime

import pytest

from hannah.nlu import NLU, resolve_clarification_answer, resolve_yes_no


@pytest.fixture
def nlu():
    return NLU(cfg={}, rooms={}, devices={})


class TestSetAlarmWeekday:
    """#4 — SetAlarm erkennt jetzt zusätzlich zur Uhrzeit einen einzelnen Wochentag."""

    def test_single_weekday_parsed(self, nlu):
        intent = nlu.parse("stelle einen wecker fuer montag 8 uhr")

        assert intent.name == "SetAlarm"
        assert intent.value == "08:00"
        assert intent.weekdays == [0]

    def test_no_weekday_falls_back_to_time_only(self, nlu):
        intent = nlu.parse("stelle einen wecker um 8 uhr")

        assert intent.name == "SetAlarm"
        assert intent.value == "08:00"
        assert intent.weekdays == []


class TestDeleteAlarm:
    """#4 — 'lösche'/'entferne' im Wecker-Kontext geht vor SetAlarm, auch wenn eine
    Uhrzeit im Satz steckt."""

    def test_relative_date_and_time(self, nlu):
        intent = nlu.parse("loesche meinen wecker fuer morgen 8 uhr")

        assert intent.name == "DeleteAlarm"
        assert intent.value == "08:00"
        assert intent.resolved_date == datetime.date.today() + datetime.timedelta(days=1)

    def test_weekday_resolves_to_concrete_date(self, nlu):
        intent = nlu.parse("loesche meinen wecker fuer montag")

        assert intent.name == "DeleteAlarm"
        assert intent.resolved_date is not None
        assert intent.resolved_date.weekday() == 0

    def test_takes_priority_over_set_alarm(self, nlu):
        """Ohne die Priorisierung würde das enthaltene alarm_time='08:00' das als
        SetAlarm durchgehen lassen."""
        intent = nlu.parse("entferne den wecker fuer heute 8 uhr")

        assert intent.name == "DeleteAlarm"
        assert intent.resolved_date == datetime.date.today()


class TestQueryAlarms:
    def test_welche_wecker(self, nlu):
        intent = nlu.parse("welche wecker habe ich gestellt")

        assert intent.name == "QueryAlarms"

    def test_non_alarm_query_unaffected(self, nlu):
        """Ohne Wecker-Kontext darf 'welche' keine QueryAlarms triggern."""
        intent = nlu.parse("welche temperatur haben wir")

        assert intent.name != "QueryAlarms"


class TestFindQueryStatePower:
    """#121 — "Strom"/"Watt"/"Leistung" müssen auf den Watt-Wert (qs="power") zielen,
    nicht auf den generischen on/off-Fallback, damit Steckdosen ihre Kategorie-Antwort
    (Watt) bekommen statt der einfachen an/aus-Antwort."""

    def test_watt(self, nlu):
        assert nlu._find_query_state("wie viel watt braucht der pc") == "power"

    def test_leistung(self, nlu):
        assert nlu._find_query_state("wie hoch ist die leistung vom pc") == "power"

    def test_strom_takes_priority_over_on(self, nlu):
        """'mein' enthält als Substring 'ein', das sonst schon die on/off-Erkennung
        triggern würde — 'strom' muss vorher greifen."""
        assert nlu._find_query_state("wie viel strom braucht mein pc") == "power"

    def test_plain_on_off_unaffected(self, nlu):
        assert nlu._find_query_state("ist der pc an") == "on"


class TestSetVolume:
    """#63 — SetVolume-Intent: absolut ("Lautstärke auf 50") oder relativ ("lauter"/"leiser")."""

    def test_absolute_level(self, nlu):
        intent = nlu.parse("stell die lautstärke auf 50 prozent")

        assert intent.name == "SetVolume"
        assert intent.value == 50.0
        assert intent.unit == "%"

    def test_louder(self, nlu):
        intent = nlu.parse("mach lauter")

        assert intent.name == "SetVolume"
        assert intent.value == 10
        assert intent.unit == "relative"

    def test_quieter(self, nlu):
        intent = nlu.parse("mach leiser")

        assert intent.name == "SetVolume"
        assert intent.value == -10
        assert intent.unit == "relative"

    def test_light_level_unaffected(self, nlu):
        """Ohne Lautstärke-Kontextwort bleibt eine Prozentangabe SetLevel (z.B. Dimmer)."""
        intent = nlu.parse("stell das licht auf 50 prozent")

        assert intent.name == "SetLevel"


class TestResolveYesNo:
    def test_yes(self):
        assert resolve_yes_no("ja gerne") is True

    def test_no(self):
        assert resolve_yes_no("nein danke") is False

    def test_unrecognized(self):
        assert resolve_yes_no("was meinst du") is None

    def test_trailing_punctuation(self):
        """#190 — STT-Transkript "Ja." darf nicht an der Satzzeichen-Klebung scheitern."""
        assert resolve_yes_no("Ja.") is True
        assert resolve_yes_no("Nein.") is False


class TestResolveClarificationAnswer:
    """#190 — Live-Vorfall: 'OK, Zimmer Süd.' wurde fälschlich 'OG Zimmer Ost' zugeordnet."""

    _candidates = [("og_zimmer_ost", "OG Zimmer Ost"), ("og_zimmer_sued", "OG Zimmer Süd")]

    def test_trailing_punctuation_matches_correct_candidate(self):
        resolved = resolve_clarification_answer("OK, Zimmer Süd.", self._candidates)
        assert resolved == ("og_zimmer_sued", "OG Zimmer Süd")

    def test_true_tie_returns_none_instead_of_first_candidate(self):
        # "Zimmer" matcht beide Kandidaten gleich stark, kein Wort grenzt ein.
        assert resolve_clarification_answer("Zimmer.", self._candidates) is None

    def test_no_match_returns_none(self):
        assert resolve_clarification_answer("Küche bitte.", self._candidates) is None

    def test_ordinal_still_works_with_punctuation(self):
        resolved = resolve_clarification_answer("die erste.", self._candidates)
        assert resolved == ("og_zimmer_ost", "OG Zimmer Ost")
