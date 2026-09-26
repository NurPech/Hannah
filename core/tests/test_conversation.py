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
