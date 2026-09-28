from hannah import responses


class TestSuccess:
    def test_no_name_always_default(self, monkeypatch):
        monkeypatch.setattr(responses.random, "random", lambda: 0.0)
        assert responses.success("OK.") == "OK."

    def test_below_threshold_uses_variant(self, monkeypatch):
        monkeypatch.setattr(responses.random, "random", lambda: 0.0)
        monkeypatch.setattr(responses.random, "choice", lambda seq: seq[0])
        assert responses.success("OK.", "Leonie") == "Sehr gerne, Leonie."

    def test_above_threshold_keeps_default(self, monkeypatch):
        monkeypatch.setattr(responses.random, "random", lambda: 0.99)
        assert responses.success("OK.", "Leonie") == "OK."

    def test_picks_among_variants(self, monkeypatch):
        monkeypatch.setattr(responses.random, "random", lambda: 0.0)
        seen = []
        monkeypatch.setattr(responses.random, "choice", lambda seq: (seen.append(seq), seq[-1])[1])
        responses.success("OK.", "Leonie")
        assert seen[0] == responses._SUCCESS_VARIANTS


class TestDenied:
    def test_no_name_always_default(self, monkeypatch):
        monkeypatch.setattr(responses.random, "random", lambda: 0.0)
        assert responses.denied("Das darfst du leider nicht steuern.") == "Das darfst du leider nicht steuern."

    def test_below_threshold_uses_variant(self, monkeypatch):
        monkeypatch.setattr(responses.random, "random", lambda: 0.0)
        monkeypatch.setattr(responses.random, "choice", lambda seq: seq[0])
        assert responses.denied("default", "Leonie") == "Tut mir leid, Leonie, das darf ich für dich nicht tun."

    def test_above_threshold_keeps_default(self, monkeypatch):
        monkeypatch.setattr(responses.random, "random", lambda: 0.99)
        assert responses.denied("default", "Leonie") == "default"


class TestOffline:
    def test_below_threshold_uses_variant_without_name(self, monkeypatch):
        monkeypatch.setattr(responses.random, "random", lambda: 0.0)
        monkeypatch.setattr(responses.random, "choice", lambda seq: seq[0])
        assert responses.offline("default text") == "Tut mir leid, das Gerät ist gerade offline."

    def test_above_threshold_keeps_default(self, monkeypatch):
        monkeypatch.setattr(responses.random, "random", lambda: 0.99)
        assert responses.offline("default text") == "default text"
