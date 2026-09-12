import time

from hannah.conversation import ConversationContext


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
