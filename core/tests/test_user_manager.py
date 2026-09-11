import os
from unittest.mock import MagicMock

from werkzeug.security import generate_password_hash

import hannah.utils.db as db_module
from hannah.user_manager import UserManager


def _make_user_manager(tmp_path):
    """Real (non-mocked) UserManager against a throwaway SQLite DB — see
    test_grpc_server.py's _make_user_manager_with_leonie for why DB_PATH has to be
    patched as a module attribute rather than just an env var."""
    db_module.DB_PATH = os.path.join(str(tmp_path), "h.db")
    db_module.init_db()
    return UserManager(db_module.get_db)


class TestGetUserById:
    def test_unknown_sentinel_returns_none_instead_of_raising(self, tmp_path):
        """Regression: Voice-ID returns the literal string "unknown" as speaker_user_id
        when recognition confidence is too low (app.py: best_match = "unknown"). That flows
        straight into get_user_by_id() via main.py's _speaker_context()/_resolve_roomie_id() —
        int("unknown") must not crash, it has to resolve like any other unknown user: None."""
        user_manager = _make_user_manager(tmp_path)

        assert user_manager.get_user_by_id("unknown") is None

    def test_numeric_string_still_resolves(self, tmp_path):
        user_manager = _make_user_manager(tmp_path)
        user = user_manager.create_user("leonie", generate_password_hash("x"), email="leonie@example.com")

        assert user_manager.get_user_by_id(str(user.id)).id == user.id


class TestDumpPresentUsers:
    def test_present_user_with_residents_link_gets_pushed(self, tmp_path):
        user_manager = _make_user_manager(tmp_path)
        pusher = MagicMock()
        user_manager.set_residents_pusher(pusher)
        user = user_manager.create_user("leonie", generate_password_hash("x"), email="leonie@example.com")
        user.link_account("residents", "leonie_roomie", provider_payload={"roomie_id": "leonie", "resident_type": "roomie"})
        user.presence = True

        user_manager.dump_present_users()

        pusher.assert_called_once_with("leonie", True, "roomie")

    def test_absent_user_is_not_pushed(self, tmp_path):
        user_manager = _make_user_manager(tmp_path)
        pusher = MagicMock()
        user_manager.set_residents_pusher(pusher)
        user = user_manager.create_user("leonie", generate_password_hash("x"), email="leonie@example.com")
        user.link_account("residents", "leonie_roomie", provider_payload={"roomie_id": "leonie", "resident_type": "roomie"})
        # presence bleibt False (Default) — kein Aufruf erwartet

        user_manager.dump_present_users()

        pusher.assert_not_called()

    def test_present_user_without_residents_link_is_skipped(self, tmp_path):
        """Regression: ein User ohne residents-Link darf keinen Pusher-Aufruf auslösen
        (z.B. roomie_id=None würde sonst als 'anwesend' Richtung ioBroker gepusht)."""
        user_manager = _make_user_manager(tmp_path)
        pusher = MagicMock()
        user_manager.set_residents_pusher(pusher)
        user = user_manager.create_user("leonie", generate_password_hash("x"), email="leonie@example.com")
        user.presence = True

        user_manager.dump_present_users()

        pusher.assert_not_called()

    def test_no_pusher_set_does_not_crash(self, tmp_path):
        user_manager = _make_user_manager(tmp_path)
        user = user_manager.create_user("leonie", generate_password_hash("x"), email="leonie@example.com")
        user.presence = True

        user_manager.dump_present_users()


class TestResidentLinkDoubleEncodedPayload:
    """#281 root cause: eine per historischem (inzwischen gefixtem) LinkAccount-RPC-Bug
    doppelt-JSON-kodierte provider_payload deckt __json_fields__'s Auto-Decode nur einmal
    ab — der Roomie fiel dadurch komplett aus get_roomie_ids() raus (nicht presence_state
    war je das Problem), unabhängig von jedem Live-Presence-Wert. _resident_link() muss
    einen so betroffenen String noch einmal parsen statt den Roomie stillschweigend zu
    verwerfen. Reproduziert über einen echten DB-Roundtrip: ein als String übergebener
    provider_payload wird von __json_fields__ beim Schreiben noch einmal JSON-kodiert,
    beim Lesen kommt exakt der reale, in der Produktions-DB gefundene Zustand zurück
    (ein String statt einem Dict)."""

    def test_double_encoded_payload_is_still_recognized_as_roomie(self, tmp_path):
        user_manager = _make_user_manager(tmp_path)
        user = user_manager.create_user("leonie", generate_password_hash("x"), email="leonie@example.com")
        user.link_account("residents", "leonie_roomie", provider_payload='{"roomie_id": "leonie", "resident_type": "roomie"}')

        assert user_manager.get_roomie_ids() == {"leonie"}

    def test_double_encoded_payload_still_reaches_dump_present_users(self, tmp_path):
        user_manager = _make_user_manager(tmp_path)
        pusher = MagicMock()
        user_manager.set_residents_pusher(pusher)
        user = user_manager.create_user("leonie", generate_password_hash("x"), email="leonie@example.com")
        user.link_account("residents", "leonie_roomie", provider_payload='{"roomie_id": "leonie", "resident_type": "roomie"}')
        user.presence = True

        user_manager.dump_present_users()

        pusher.assert_called_once_with("leonie", True, "roomie")

    def test_genuinely_unparseable_payload_still_excludes_roomie(self, tmp_path):
        """Kein blindes Verschlucken jedes Fehlers — ein Payload, der auch nach dem
        zweiten json.loads() kein Dict ergibt, bleibt weiterhin ausgeschlossen."""
        user_manager = _make_user_manager(tmp_path)
        user = user_manager.create_user("leonie", generate_password_hash("x"), email="leonie@example.com")
        user.link_account("residents", "leonie_roomie", provider_payload="not json at all")

        assert user_manager.get_roomie_ids() == set()
