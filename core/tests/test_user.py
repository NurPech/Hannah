from hannah.models.user import User


class TestPresenceStateDefault:
    def test_defaults_to_away(self):
        user = User()
        assert user.presence_state == "away"
        assert user.is_home is False
        assert user.is_asleep is False


class TestPresenceStateTransitions:
    """hannah#309 (Nachfolger von #298) — presence_state ersetzt die früheren zwei
    unabhängigen Booleans presence/asleep durch ein einziges Tristate-Feld, das ihre
    Events (arrival/departure/fell_asleep/woke_up) aus dem Vorher/Nachher-Vergleich
    herleitet, analog zu Resident.update()."""

    def test_away_to_home_fires_arrival_only(self):
        user = User()
        events = []
        user.on("arrival", lambda _u: events.append("arrival"))
        user.on("departure", lambda _u: events.append("departure"))
        user.on("fell_asleep", lambda _u: events.append("fell_asleep"))
        user.on("woke_up", lambda _u: events.append("woke_up"))

        user.presence_state = "home"

        assert events == ["arrival"]

    def test_home_to_away_fires_departure_only(self):
        user = User()
        user.presence_state = "home"
        events = []
        user.on("arrival", lambda _u: events.append("arrival"))
        user.on("departure", lambda _u: events.append("departure"))
        user.on("fell_asleep", lambda _u: events.append("fell_asleep"))
        user.on("woke_up", lambda _u: events.append("woke_up"))

        user.presence_state = "away"

        assert events == ["departure"]

    def test_home_to_asleep_fires_fell_asleep_only(self):
        """Bleibt "zuhause" — kein arrival/departure, nur der Schlaf-Übergang."""
        user = User()
        user.presence_state = "home"
        events = []
        user.on("arrival", lambda _u: events.append("arrival"))
        user.on("departure", lambda _u: events.append("departure"))
        user.on("fell_asleep", lambda _u: events.append("fell_asleep"))
        user.on("woke_up", lambda _u: events.append("woke_up"))

        user.presence_state = "asleep"

        assert events == ["fell_asleep"]
        assert user.is_home is True
        assert user.is_asleep is True

    def test_asleep_to_home_fires_woke_up_only(self):
        user = User()
        user.presence_state = "asleep"
        events = []
        user.on("arrival", lambda _u: events.append("arrival"))
        user.on("departure", lambda _u: events.append("departure"))
        user.on("fell_asleep", lambda _u: events.append("fell_asleep"))
        user.on("woke_up", lambda _u: events.append("woke_up"))

        user.presence_state = "home"

        assert events == ["woke_up"]

    def test_away_to_asleep_fires_both_arrival_and_fell_asleep(self):
        """Direkter Sprung ohne Zwischenstopp bei 'home' — z.B. eine erste Snapshot-
        Zustellung, bei der die Person laut Adapter bereits schläft. Beide Übergänge
        (Anwesenheit + Schlaf) betreffen denselben Vorher/Nachher-Vergleich."""
        user = User()
        events = []
        user.on("arrival", lambda _u: events.append("arrival"))
        user.on("departure", lambda _u: events.append("departure"))
        user.on("fell_asleep", lambda _u: events.append("fell_asleep"))
        user.on("woke_up", lambda _u: events.append("woke_up"))

        user.presence_state = "asleep"

        assert events == ["arrival", "fell_asleep"]

    def test_asleep_to_away_fires_both_departure_and_woke_up(self):
        user = User()
        user.presence_state = "asleep"
        events = []
        user.on("arrival", lambda _u: events.append("arrival"))
        user.on("departure", lambda _u: events.append("departure"))
        user.on("fell_asleep", lambda _u: events.append("fell_asleep"))
        user.on("woke_up", lambda _u: events.append("woke_up"))

        user.presence_state = "away"

        assert events == ["departure", "woke_up"]

    def test_setting_same_value_is_a_noop(self):
        user = User()
        user.presence_state = "home"
        events = []
        user.on("arrival", lambda _u: events.append("arrival"))

        user.presence_state = "home"

        assert events == []

    def test_invalid_value_raises(self):
        user = User()
        try:
            user.presence_state = "somewhere"
            assert False, "expected ValueError"
        except ValueError:
            pass
