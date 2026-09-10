import datetime

import pytest

from hannah.iobroker import Device
from hannah.nlu import NLU, build_category_clarification_question, resolve_clarification_answer, resolve_yes_no


@pytest.fixture
def nlu():
    return NLU(cfg={}, rooms={}, devices={})


@pytest.fixture
def nlu_with_satellite():
    return NLU(cfg={}, rooms={}, devices={}, satellites={"flur01_id": "Flur01"})


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


class TestTimeDateQuery:
    """Uhrzeit/Datum rein regelbasiert beantwortbar, kein LLM nötig."""

    def test_wie_spaet(self, nlu):
        intent = nlu.parse("wie spaet ist es")

        assert intent.name == "TimeQuery"

    def test_uhrzeit(self, nlu):
        intent = nlu.parse("welche uhrzeit haben wir")

        assert intent.name == "TimeQuery"

    def test_datum(self, nlu):
        intent = nlu.parse("welches datum haben wir heute")

        assert intent.name == "DateQuery"

    def test_wochentag(self, nlu):
        intent = nlu.parse("welcher wochentag ist heute")

        assert intent.name == "DateQuery"

    def test_time_takes_priority_over_date_when_both_present(self, nlu):
        intent = nlu.parse("sag mir uhrzeit und datum")

        assert intent.name == "TimeQuery"


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


class TestCaptureIntents:
    """#230 — StartCapture/StopCapture: Ziel ist immer der Satellitenname, nie ein Raum."""

    def test_start_capture_resolves_satellite_and_defaults_to_noise(self, nlu_with_satellite):
        intent = nlu_with_satellite.parse("starte die hintergrundaufnahme auf flur01")

        assert intent.name == "StartCapture"
        assert intent.satellite_id == "flur01_id"
        assert intent.capture_sample_type == "noise"
        assert intent.capture_mode is None

    def test_start_capture_recognizes_explicit_noise(self, nlu_with_satellite):
        intent = nlu_with_satellite.parse("starte noise aufnahme auf flur01")
        assert intent.name == "StartCapture"
        assert intent.capture_sample_type == "noise"

    def test_start_capture_recognizes_hey_hannah_via_weckwort(self, nlu_with_satellite):
        intent = nlu_with_satellite.parse("starte weckwort aufnahme auf flur01")
        assert intent.name == "StartCapture"
        assert intent.capture_sample_type == "hey_hannah"

    def test_start_capture_treats_manuell_as_ptt(self, nlu_with_satellite):
        """CAPTURE_MODE_MANUAL ist deprecated (identisch zu PTT, hannah-proto#15) —
        "manuell" mappt bewusst auf "ptt", nicht auf einen eigenen Modus."""
        intent = nlu_with_satellite.parse("starte weckwort aufnahme auf flur01 manuell")
        assert intent.capture_mode == "ptt"

    def test_stop_capture_resolves_satellite(self, nlu_with_satellite):
        intent = nlu_with_satellite.parse("stoppe die aufnahme auf flur01")

        assert intent.name == "StopCapture"
        assert intent.satellite_id == "flur01_id"

    def test_no_satellite_mentioned_does_not_trigger_capture(self, nlu_with_satellite):
        intent = nlu_with_satellite.parse("starte die aufnahme")
        assert intent.name != "StartCapture"


def _make_device(key: str, room: str, category: str = "window") -> Device:
    return Device(id=f"{room}.{key}", name=key, key=key, room=room,
                  room_display_name=room.title(), floor="EG", category=category)


class TestFindDeviceRoomScoped:
    """#263 — bei explizit genanntem Raum ohne passendes Gerät darf _find_device()
    nicht in andere Räume ausweichen (Forenbericht manne01: "Fenster im WC" fand
    stattdessen das Fenster im Kinderzimmer, weil WC kein Fenster-Gerät hat)."""

    @pytest.fixture
    def nlu_rooms(self):
        rooms = {"wc": "WC", "kinderzimmer": "Kinderzimmer"}
        devices = {
            "wc": {"tuer": _make_device("tuer", "wc")},
            "kinderzimmer": {"fenster": _make_device("fenster", "kinderzimmer")},
        }
        return NLU(cfg={}, rooms=rooms, devices=devices)

    def test_no_cross_room_fallback_when_room_named(self, nlu_rooms):
        key, dev, candidates = nlu_rooms._find_device("fenster im wc", "wc")
        assert key is None
        assert dev is None
        assert candidates == []

    def test_matches_within_named_room(self, nlu_rooms):
        key, dev, candidates = nlu_rooms._find_device("tuer im wc", "wc")
        assert key == "tuer"
        assert dev.room == "wc"
        assert candidates == []

    def test_cross_room_search_when_no_room_named(self, nlu_rooms):
        """Ohne erkannten Raum bleibt die raumübergreifende Suche erhalten."""
        key, dev, candidates = nlu_rooms._find_device("fenster", None)
        assert key == "fenster"
        assert dev.room == "kinderzimmer"
        assert candidates == []

    def test_satellite_name_without_context_word_does_not_trigger_capture(self, nlu_with_satellite):
        intent = nlu_with_satellite.parse("starte flur01")
        assert intent.name != "StartCapture"

    def test_plain_stop_without_satellite_stays_stop_intent(self, nlu_with_satellite):
        """Generisches 'stopp' (Wiedergabe) darf durch die neue Capture-Logik nicht
        umgebogen werden, nur weil irgendein Satellit registriert ist."""
        intent = nlu_with_satellite.parse("stopp")
        assert intent.name == "StopIntent"


class TestResolveDeviceInRoom:
    """#274 — nach einer Raum-Rückfrage muss die Geräte-Suche im jetzt bekannten Raum
    wiederholt werden können. Beim ersten parse()-Durchlauf lief sie nur im per
    Tie-Break geratenen (ggf. falschen) Raum und fand dort ggf. nichts."""

    @pytest.fixture
    def nlu_rooms(self):
        rooms = {"bad_oben": "Bad oben", "og_zimmer_sued": "OG Zimmer Süd"}
        devices = {
            "bad_oben": {"tuer": _make_device("tuer", "bad_oben", category="door")},
            "og_zimmer_sued": {"licht": _make_device("licht", "og_zimmer_sued", category="light")},
        }
        return NLU(cfg={}, rooms=rooms, devices=devices)

    def test_finds_device_in_confirmed_room(self, nlu_rooms):
        """Reproduziert #274: erster parse()-Durchlauf riet 'OG Zimmer Süd' (keine Tür
        dort), Nutzer bestätigt danach 'Bad oben' — Suche muss dort erneut laufen."""
        device_key, dev = nlu_rooms.resolve_device_in_room("Ist die Tür vom Bad OG offen?", "bad_oben")
        assert device_key == "tuer"
        assert dev.room == "bad_oben"

    def test_no_device_mentioned_returns_none(self, nlu_rooms):
        """Kein Gerätename im Text → weiterhin (None, None), kein falsches Match."""
        device_key, dev = nlu_rooms.resolve_device_in_room("wie ist es im Bad oben", "bad_oben")
        assert device_key is None
        assert dev is None

    def test_device_not_in_confirmed_room_returns_none(self, nlu_rooms):
        """Gerätename passt zu keinem Gerät im bestätigten Raum → kein Cross-Room-Fallback,
        analog zu _find_device()s bestehendem Verhalten bei explizit genanntem Raum (#263)."""
        device_key, dev = nlu_rooms.resolve_device_in_room("Ist die Tür offen?", "og_zimmer_sued")
        assert device_key is None
        assert dev is None


class TestFindDeviceCrossRoomAmbiguity:
    """#268 — gleicher Gerätename in mehreren Räumen ohne Raumangabe im Satz muss
    als Mehrdeutigkeit erkannt werden statt per Dict-Reihenfolge zufällig ein
    Gerät zu gewinnen."""

    @pytest.fixture
    def nlu_rooms(self):
        rooms = {"schlafzimmer_1": "Schlafzimmer 1", "schlafzimmer_2": "Schlafzimmer 2",
                  "kinderzimmer": "Kinderzimmer"}
        devices = {
            "schlafzimmer_1": {"nachtlicht": _make_device("nachtlicht", "schlafzimmer_1")},
            "schlafzimmer_2": {"nachtlicht": _make_device("nachtlicht", "schlafzimmer_2")},
            "kinderzimmer": {"fenster": _make_device("fenster", "kinderzimmer")},
        }
        return NLU(cfg={"turn_on_words": ["an"]}, rooms=rooms, devices=devices)

    def test_ambiguous_device_returns_room_candidates(self, nlu_rooms):
        key, dev, candidates = nlu_rooms._find_device("nachtlicht", None)
        assert dev is None
        assert key == "nachtlicht"
        assert {c[0] for c in candidates} == {"schlafzimmer_1", "schlafzimmer_2"}

    def test_unambiguous_cross_room_device_still_resolves(self, nlu_rooms):
        key, dev, candidates = nlu_rooms._find_device("fenster", None)
        assert key == "fenster"
        assert dev.room == "kinderzimmer"
        assert candidates == []

    def test_ambiguous_device_intent_sets_candidates_not_device_id(self, nlu_rooms):
        intent = nlu_rooms.parse("nachtlicht an")
        assert intent.name == "TurnOn"
        assert intent.device_id is None
        assert intent.device_key == "nachtlicht"
        assert len(intent.candidates) == 2


class TestBlindOpenClose:
    """#260 — 'öffnen'/'schließen'-Vokabular soll für Rolladen/Markisen (category
    'blind') auf SetLevel 100/0 gemappt werden, analog zu den TurnOn/TurnOff-
    Synonymen. Muss auf 'blind'-Geräte beschränkt bleiben, sonst würde z.B.
    "öffne die Tür" (reiner Sensor, kein steuerbarer State) fälschlich einen
    SetLevel-Intent erzeugen."""

    @pytest.fixture
    def nlu_rooms(self):
        rooms = {"kueche": "Küche"}
        devices = {
            "kueche": {
                "rolladenlinks": _make_device("rolladenlinks", "kueche", category="blind"),
                "tuer": _make_device("tuer", "kueche", category="door"),
            },
        }
        return NLU(cfg={}, rooms=rooms, devices=devices)

    def test_open_word_maps_to_setlevel_100(self, nlu_rooms):
        intent = nlu_rooms.parse("oeffne den rolladenlinks in der kueche")
        assert intent.name == "SetLevel"
        assert intent.value == 100

    def test_close_word_maps_to_setlevel_0(self, nlu_rooms):
        intent = nlu_rooms.parse("schliesse den rolladenlinks in der kueche")
        assert intent.name == "SetLevel"
        assert intent.value == 0

    def test_rauf_runter_synonyms_work_too(self, nlu_rooms):
        assert nlu_rooms.parse("rolladenlinks rauf").value == 100
        assert nlu_rooms.parse("rolladenlinks runter").value == 0

    def test_hoch_resolves_via_device_category(self, nlu_rooms):
        """#272: 'hoch' ist jetzt zugleich Rolladen-öffnen- und Lüfterstufe-Wort (kehrt
        den #260-Workaround um) — bei explizit genanntem Rolladen-Gerät legt dessen
        Kategorie die Bedeutung eindeutig fest, keine Rückfrage nötig."""
        intent = nlu_rooms.parse("rolladenlinks hoch")
        assert intent.name == "SetLevel"
        assert intent.value == 100
        assert intent.category_candidates == []

    def test_category_bulk_command_also_works(self, nlu_rooms):
        """Ohne konkreten Gerätenamen, nur über den Kategorie-Sammelbefehl."""
        intent = nlu_rooms.parse("schliesse alle rollladen")
        assert intent.name == "SetLevel"
        assert intent.value == 0
        assert intent.category_filter == "blind"

    def test_open_word_on_non_blind_device_is_not_setlevel(self, nlu_rooms):
        """Regression guard: 'öffne die Tür' darf nicht auf SetLevel abbiegen —
        Türen sind reine Sensoren ohne steuerbaren State."""
        intent = nlu_rooms.parse("oeffne die tuer in der kueche")
        assert intent.name != "SetLevel"

    def test_query_takes_priority_over_open_close(self, nlu_rooms):
        intent = nlu_rooms.parse("ist der rolladenlinks in der kueche offen?")
        assert intent.name == "Query"

    def test_open_word_sets_is_open_close_flag(self, nlu_rooms):
        """#270: Core braucht dieses Flag, um nur die semantischen open/close-
        Grenzwerte zu invertieren, nie einen explizit genannten Prozentwert."""
        intent = nlu_rooms.parse("oeffne den rolladenlinks in der kueche")
        assert intent.is_open_close is True

    def test_explicit_percent_does_not_set_is_open_close_flag(self, nlu_rooms):
        intent = nlu_rooms.parse("rolladenlinks in der kueche auf 100 prozent")
        assert intent.name == "SetLevel"
        assert intent.value == 100
        assert intent.is_open_close is False


class TestCategoryAwareDispatch:
    """#272 — Wörter wie 'hoch'/'runter' sind je Kategorie unterschiedlich belegt
    (Rolladen: öffnen/schließen; Klima: Lüfterstufe). Die Dispatch-Tabelle wertet nur
    die zur Zielkategorie passende Wortliste aus, anhand von Gerät/category_filter
    oder — ohne beides — anhand der im Zielbereich tatsächlich vorkommenden
    Kategorien; bei echter Restambiguität (mehrere Kategorien im Zielbereich, kein
    Gerätename/Kategoriewort) wird stattdessen eine Rückfrage nötig."""

    @pytest.fixture
    def nlu_blind_only(self):
        rooms = {"kueche": "Küche"}
        devices = {"kueche": {"rolladenlinks": _make_device("rolladenlinks", "kueche", category="blind")}}
        return NLU(cfg={}, rooms=rooms, devices=devices)

    @pytest.fixture
    def nlu_climate_only(self):
        rooms = {"buero": "Büro"}
        devices = {"buero": {"klimaanlage": _make_device("klimaanlage", "buero", category="climate")}}
        return NLU(cfg={}, rooms=rooms, devices=devices)

    @pytest.fixture
    def nlu_mixed(self):
        """Küche hat sowohl Rolladen als auch Klimaanlage — der Beispielfall aus #272."""
        rooms = {"kueche": "Küche"}
        devices = {
            "kueche": {
                "rolladenlinks": _make_device("rolladenlinks", "kueche", category="blind"),
                "klimaanlage": _make_device("klimaanlage", "kueche", category="climate"),
            },
        }
        return NLU(cfg={}, rooms=rooms, devices=devices)

    def test_scope_inference_resolves_unambiguously_for_blind_only_room(self, nlu_blind_only):
        """Kein Gerätename, kein Kategoriewort — aber die Küche hat nur Rolladen,
        also eindeutig auflösbar ohne Rückfrage."""
        intent = nlu_blind_only.parse("mach die kueche hoch")
        assert intent.name == "SetLevel"
        assert intent.value == 100
        assert intent.category_filter == "blind"
        assert intent.category_candidates == []

    def test_scope_inference_resolves_unambiguously_for_climate_only_room(self, nlu_climate_only):
        intent = nlu_climate_only.parse("mach das buero hoch")
        assert intent.name == "SetFanSpeed"
        assert intent.value == "high"
        assert intent.category_filter == "climate"
        assert intent.category_candidates == []

    def test_ambiguous_room_asks_for_clarification(self, nlu_mixed):
        """Küche mit Rolladen UND Klimaanlage, 'hoch' ohne Gerätename/Kategoriewort
        — echte Restambiguität aus der #272-Beschreibung."""
        intent = nlu_mixed.parse("mach die kueche hoch")
        assert intent.name != "SetLevel"
        assert intent.name != "SetFanSpeed"
        assert len(intent.category_candidates) == 2
        categories = {c[0] for c in intent.category_candidates}
        assert categories == {"blind", "climate"}

    def test_explicit_device_bypasses_ambiguity(self, nlu_mixed):
        """Gerätename legt die Kategorie fest, auch wenn der Raum sonst mehrdeutig wäre."""
        intent = nlu_mixed.parse("rolladenlinks hoch")
        assert intent.name == "SetLevel"
        assert intent.value == 100
        assert intent.category_candidates == []

    def test_explicit_category_word_bypasses_ambiguity(self, nlu_mixed):
        """'Klimaanlage' als Kategorie-Sammelbegriff legt die Kategorie fest."""
        intent = nlu_mixed.parse("klimaanlage in der kueche hoch")
        assert intent.name == "SetFanSpeed"
        assert intent.value == "high"
        assert intent.category_candidates == []

    def test_climate_mode_words_still_dict_driven(self, nlu_climate_only):
        intent = nlu_climate_only.parse("kuehlen im buero")
        assert intent.name == "SetMode"
        assert intent.value == "cool"

    def test_category_clarification_question_and_resolution(self, nlu_mixed):
        intent = nlu_mixed.parse("mach die kueche hoch")
        question = build_category_clarification_question(intent.category_candidates)
        assert "Rolladen" in question
        assert "Klimaanlage" in question

        room_candidates = [(cat, label) for cat, label, *_ in intent.category_candidates]
        resolved = resolve_clarification_answer("die Klimaanlage", room_candidates)
        assert resolved == ("climate", "Klimaanlage")
