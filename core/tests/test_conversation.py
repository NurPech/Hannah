from hannah.conversation import ConversationContext


class TestIsAddressedToHannah:
    """#158 — Stub für den späteren Relevanzcheck während offenem Smalltalk-Follow-up-Mic.
    Aktuell immer True; der Silence-Timeout der Satelliten-Firmware übernimmt die Abgrenzung."""

    def test_stub_always_true(self):
        ctx = ConversationContext()

        assert ctx.is_addressed_to_hannah("wz-esp", "irgendwas") is True

    def test_stub_true_even_without_active_context(self):
        ctx = ConversationContext()

        assert ctx.is_addressed_to_hannah("unknown-source", "") is True
