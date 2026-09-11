from unittest.mock import MagicMock

import pytest

from hannah.residents_manager import ResidentsClient
from hannah.residents import Roomie


@pytest.fixture
def user_manager():
    um = MagicMock()
    um.get_roomie_ids.return_value = {"leonie"}
    return um


@pytest.fixture
def residents(user_manager):
    return ResidentsClient({}, user_manager)


class TestIsHomeUnknownPresence:
    """#281 — nach jedem Core-Neustart ist self._residents leer und presence_state
    unbekannt, bis ein frisches Update reinkommt. is_home() muss "unbekannt"
    konservativ als "könnte zuhause sein" werten, nicht als "weg" — sonst gibt
    die OTA-Freigabe (main.py:_on_ota_pending) sofort frei, egal wer zuhause ist."""

    def test_no_residents_known_yet_is_conservatively_home(self, residents):
        assert residents.is_home() is True

    def test_resident_never_updated_is_conservatively_home(self, residents):
        residents.get_or_create("leonie", Roomie)  # erzeugt, aber presence_state bleibt None

        assert residents.is_home() is True

    def test_first_update_after_restart_with_presence_home(self, residents):
        """Repro der genauen Boot-Sequenz: frisch erzeugter Resident (old_presence=None),
        erstes Update trägt presence_state=1 — muss trotz unterdrücktem
        arrival-Event (Resident.update()) korrekt als "zuhause" gewertet werden."""
        r = residents.get_or_create("leonie", Roomie)
        r.update(display_name="Leonie", presence_state=1, mood=5)

        assert residents.is_home() is True

    def test_confirmed_away_is_not_home(self, residents):
        r = residents.get_or_create("leonie", Roomie)
        r.update(display_name="Leonie", presence_state=0, mood=None)

        assert residents.is_home() is False

    def test_confirmed_away_then_unknown_again_is_not_reset(self, residents):
        """Einmal bestätigt 'weg' bleibt 'weg' — presence_state wird durch ein
        späteres Update nicht auf None zurückgesetzt (nur echte neue Werte setzen)."""
        r = residents.get_or_create("leonie", Roomie)
        r.update(display_name="Leonie", presence_state=0, mood=None)

        assert residents.is_home() is False

    def test_no_roomies_configured_is_not_home(self, user_manager):
        user_manager.get_roomie_ids.return_value = set()
        residents = ResidentsClient({}, user_manager)

        assert residents.is_home() is False

    def test_roomie_id_filter_variant_matches_aggregate_behavior(self, residents):
        assert residents.is_home("leonie") is True

        r = residents.get_or_create("leonie", Roomie)
        r.update(display_name="Leonie", presence_state=0, mood=None)

        assert residents.is_home("leonie") is False
