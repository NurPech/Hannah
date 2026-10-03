import time

from hannah.conversation import ConversationContext
from hannah.nlu import Intent


class TestFillIntentDevice:
    """#354 — "Licht aus" nach "Computer an" hat den Computer ausgeschaltet: fill_intent()
    hat das Gerät aus dem Kontext geerbt, obwohl der User eine Kategorie genannt hat."""

    def _ctx_after_computer_on(self):
        ctx = ConversationContext(ttl=120.0)
        ctx.update_from_intent("sat01", Intent(
            name="TurnOn", room="OG Zimmer Süd", room_id="og zimmer süd",
            category_filter="Licht",
        ))
        ctx.update_from_intent("sat01", Intent(
            name="TurnOn", room="OG Zimmer Süd", room_id="og zimmer süd",
            device="Computer", device_id="javascript.0.virtualDevice.Computer",
        ))
        return ctx

    def test_category_named_does_not_inherit_device(self):
        ctx = self._ctx_after_computer_on()
        intent = Intent(name="TurnOff", category_filter="Licht")
        ctx.fill_intent("sat01", intent)

        assert intent.device is None
        assert intent.device_id is None
        assert intent.category_filter == "Licht"
        assert intent.room_id == "og zimmer süd"

    def test_no_target_named_still_inherits_device(self):
        """"Und wieder aus" — weder Gerät noch Kategorie noch Raum: meint das letzte Gerät."""
        ctx = self._ctx_after_computer_on()
        intent = Intent(name="TurnOff")
        ctx.fill_intent("sat01", intent)

        assert intent.device == "Computer"
        assert intent.device_id == "javascript.0.virtualDevice.Computer"

    def test_room_named_does_not_inherit_device(self):
        ctx = self._ctx_after_computer_on()
        intent = Intent(name="TurnOff", room="Küche", room_id="küche")
        ctx.fill_intent("sat01", intent)

        assert intent.device is None


class TestFillIntentCategory:
    """#355 — "Computer an" nach "Licht an" lief intern mit category_filter = Licht:
    fill_intent() hat die Kategorie aus dem Kontext geerbt, obwohl ein Gerät genannt war."""

    def _ctx_after_light_on(self):
        ctx = ConversationContext(ttl=120.0)
        ctx.update_from_intent("sat01", Intent(
            name="TurnOn", room="OG Zimmer Süd", room_id="og zimmer süd",
            category_filter="Licht",
        ))
        return ctx

    def _ctx_after_computer_on(self):
        ctx = self._ctx_after_light_on()
        ctx.update_from_intent("sat01", Intent(
            name="TurnOn", room="OG Zimmer Süd", room_id="og zimmer süd",
            device="Computer", device_id="javascript.0.virtualDevice.Computer",
        ))
        return ctx

    def test_device_named_does_not_inherit_category(self):
        ctx = self._ctx_after_light_on()
        intent = Intent(name="TurnOn", device="Computer", device_id="javascript.0.virtualDevice.Computer")
        ctx.fill_intent("sat01", intent)

        assert intent.device == "Computer"
        assert intent.category_filter is None
        assert intent.room_id == "og zimmer süd"

    def test_device_from_context_does_not_bring_a_stale_category(self):
        """"Und wieder aus" nach "Computer an": das Gerät kommt aus dem Kontext, die Kategorie
        Licht stammt aus einem früheren Befehl und gehört nicht zum Computer."""
        ctx = self._ctx_after_computer_on()
        intent = Intent(name="TurnOff")
        ctx.fill_intent("sat01", intent)

        assert intent.device == "Computer"
        assert intent.category_filter is None

    def test_ambiguous_device_does_not_inherit_category(self):
        ctx = self._ctx_after_light_on()
        intent = Intent(name="TurnOn", device_candidates=[("a.Seite1", "Seite1"), ("a.Seite2", "Seite2")])
        ctx.fill_intent("sat01", intent)

        assert intent.category_filter is None

    def test_device_key_does_not_inherit_category(self):
        ctx = self._ctx_after_light_on()
        intent = Intent(name="TurnOn", device_key="decke seite")
        ctx.fill_intent("sat01", intent)

        assert intent.category_filter is None

    def test_without_a_device_the_category_is_still_inherited(self):
        """"Und die Küche auch?" nach "Licht an": kein Gerät, die Kategorie bleibt."""
        ctx = self._ctx_after_light_on()
        intent = Intent(name="Unknown", room="Küche", room_id="küche")
        ctx.fill_intent("sat01", intent)

        assert intent.category_filter == "Licht"

    def test_category_named_is_kept(self):
        ctx = self._ctx_after_computer_on()
        intent = Intent(name="TurnOff", category_filter="Stecker")
        ctx.fill_intent("sat01", intent)

        assert intent.category_filter == "Stecker"
        assert intent.device is None


class TestFillIntentSatelliteRoom:
    """#370 — "Licht an" ohne Raumangabe hat Geräte im "Balkon" geschaltet statt im
    tatsächlichen Raum des Satelliten ("OG Zimmer Süd"): fill_intent() hatte den Raum
    aus einem Stunden zurückliegenden, thematisch unabhängigen Balkon-Befehl geerbt,
    weil praktisch jede Interaktion mit der Quelle die Kontext-TTL erneuert (nicht nur
    raumtragende Befehle) und der Satelliten-Fallback in main.py nur griff, wenn
    intent.room zu diesem Zeitpunkt noch None war."""

    def _ctx_after_balkon_on(self):
        ctx = ConversationContext(ttl=120.0)
        ctx.update_from_intent("sat01", Intent(
            name="TurnOn", room="Balkon", room_id="balkon",
        ))
        return ctx

    def test_known_satellite_room_blocks_context_room_inherit(self):
        ctx = self._ctx_after_balkon_on()
        intent = Intent(name="TurnOn")
        ctx.fill_intent("sat01", intent, satellite_room="og zimmer süd")

        assert intent.room_id is None
        assert intent.room is None

    def test_known_satellite_room_blocks_device_inherit_too(self):
        ctx = ConversationContext(ttl=120.0)
        ctx.update_from_intent("sat01", Intent(
            name="TurnOn", room="Balkon", room_id="balkon",
            device="Strahler", device_id="javascript.0.virtualDevice.Strahler",
        ))
        intent = Intent(name="TurnOn")
        ctx.fill_intent("sat01", intent, satellite_room="og zimmer süd")

        assert intent.device is None
        assert intent.device_id is None

    def test_without_satellite_room_still_inherits_as_before(self):
        """Regressionsschutz: Quellen ohne bekannten Satelliten-Raum (z.B. Telegram)
        erben weiterhin wie bisher aus dem Kontext."""
        ctx = self._ctx_after_balkon_on()
        intent = Intent(name="TurnOn")
        ctx.fill_intent("sat01", intent)

        assert intent.room_id == "balkon"

    def test_explicit_room_in_text_wins_over_satellite_room(self):
        """Nennt der User selbst einen Raum, bleibt der unangetastet — satellite_room
        ist nur ein Fallback für fehlende Angaben, kein Override."""
        ctx = self._ctx_after_balkon_on()
        intent = Intent(name="TurnOn", room="Küche", room_id="küche")
        ctx.fill_intent("sat01", intent, satellite_room="og zimmer süd")

        assert intent.room_id == "küche"


class TestRecordTts:
    """#253 — proaktive Announcements/Notifications laufen nie über add_llm_exchange()
    (kein User-Turn davor). record_tts() merkt sie trotzdem als Assistant-Turn in der
    gleichen History, damit eine kurz danach eintreffende Folge-Äußerung nicht komplett
    ohne Kontext beim LLM landet."""

    def test_appends_assistant_only_message(self):
        ctx = ConversationContext(ttl=120.0)
        ctx.record_tts("kueche01", "Die Bienen sind gelb mit schwarzen Streifen.")

        history = ctx.get_llm_history("kueche01")
        assert history == [
            {"role": "assistant", "content": "Die Bienen sind gelb mit schwarzen Streifen."},
        ]

    def test_empty_text_is_ignored(self):
        ctx = ConversationContext(ttl=120.0)
        ctx.record_tts("kueche01", "")

        assert ctx.get_llm_history("kueche01") == []

    def test_expires_after_ttl_like_regular_history(self):
        ctx = ConversationContext(ttl=0.05)
        ctx.record_tts("kueche01", "Nachricht ist raus.")
        time.sleep(0.1)

        assert ctx.get_llm_history("kueche01") == []

    def test_coexists_with_existing_smalltalk_turn(self):
        """Ein laufendes Smalltalk-Gespräch (add_llm_exchange) wird durch eine
        dazwischenkommende Announcement nicht überschrieben, nur ergänzt."""
        ctx = ConversationContext(ttl=120.0)
        ctx.add_llm_exchange("kueche01", "was sind bienen", "Bienen sind gelb mit schwarzen Streifen.")
        ctx.record_tts("kueche01", "Es ist noch eine Nachricht für dich da.")

        history = ctx.get_llm_history("kueche01")
        assert history == [
            {"role": "user", "content": "was sind bienen"},
            {"role": "assistant", "content": "Bienen sind gelb mit schwarzen Streifen."},
            {"role": "assistant", "content": "Es ist noch eine Nachricht für dich da."},
        ]

    def test_scoped_per_source(self):
        ctx = ConversationContext(ttl=120.0)
        ctx.record_tts("kueche01", "Nachricht für die Küche.")

        assert ctx.get_llm_history("wohnzimmer01") == []
