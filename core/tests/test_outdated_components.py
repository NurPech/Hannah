import os

from werkzeug.security import generate_password_hash

import hannah.utils.db as db_module
from hannah.messages import MessageManager
from hannah.outdated_components import OutdatedComponentNotifier
from hannah.user_manager import UserManager


def _make_notifier(tmp_path):
    """Echte (nicht gemockte) Manager gegen eine Wegwerf-SQLite-DB — gleiches Muster
    wie test_user_manager.py's _make_user_manager."""
    db_module.DB_PATH = os.path.join(str(tmp_path), "h.db")
    db_module.init_db()
    user_manager = UserManager(db_module.get_db)
    message_manager = MessageManager(db_module.get_db, user_manager=user_manager)
    notifier = OutdatedComponentNotifier(db_module.get_db, user_manager, message_manager)
    return notifier, user_manager, message_manager


def _make_admin(user_manager, username="leonie"):
    admin = user_manager.create_user(username, generate_password_hash("x"), email=f"{username}@example.com")
    admin.update(trust_level=10)
    return admin


class TestNotifyLegacyCall:
    """#358 — eine Nachricht pro (RPC-Methode, x-proto-version), nicht bei jedem Aufruf."""

    def test_messages_every_admin_once(self, tmp_path):
        notifier, user_manager, message_manager = _make_notifier(tmp_path)
        admin1 = _make_admin(user_manager, "leonie")
        admin2 = _make_admin(user_manager, "rene")

        notifier.notify_legacy_call("ChannelConnect", "3.2.0")

        assert len(message_manager.get_messages(admin1.id)) == 1
        assert len(message_manager.get_messages(admin2.id)) == 1
        assert "ChannelConnect" in message_manager.get_messages(admin1.id)[0]["content"]
        assert "3.2.0" in message_manager.get_messages(admin1.id)[0]["content"]

    def test_non_admin_gets_nothing(self, tmp_path):
        notifier, user_manager, message_manager = _make_notifier(tmp_path)
        _make_admin(user_manager, "leonie")
        guest = user_manager.create_user("guest", generate_password_hash("x"), email="guest@example.com")
        guest.update(trust_level=3)

        notifier.notify_legacy_call("ChannelConnect", "3.2.0")

        assert message_manager.get_messages(guest.id) == []

    def test_same_method_and_version_notifies_only_once(self, tmp_path):
        notifier, user_manager, message_manager = _make_notifier(tmp_path)
        admin = _make_admin(user_manager)

        notifier.notify_legacy_call("ChannelConnect", "3.2.0")
        notifier.notify_legacy_call("ChannelConnect", "3.2.0")
        notifier.notify_legacy_call("ChannelConnect", "3.2.0")

        assert len(message_manager.get_messages(admin.id)) == 1

    def test_same_method_different_version_notifies_again(self, tmp_path):
        notifier, user_manager, message_manager = _make_notifier(tmp_path)
        admin = _make_admin(user_manager)

        notifier.notify_legacy_call("ChannelConnect", "3.2.0")
        notifier.notify_legacy_call("ChannelConnect", "3.3.0")

        assert len(message_manager.get_messages(admin.id)) == 2

    def test_debounce_survives_a_restart(self, tmp_path):
        """Der Debounce-Marker muss persistiert sein — ein neuer Notifier gegen dieselbe
        DB (z.B. nach einem Core-Neustart) darf nicht erneut benachrichtigen."""
        db_module.DB_PATH = os.path.join(str(tmp_path), "h.db")
        db_module.init_db()
        user_manager = UserManager(db_module.get_db)
        message_manager = MessageManager(db_module.get_db, user_manager=user_manager)
        admin = _make_admin(user_manager)

        first = OutdatedComponentNotifier(db_module.get_db, user_manager, message_manager)
        first.notify_legacy_call("ChannelConnect", "3.2.0")

        second = OutdatedComponentNotifier(db_module.get_db, user_manager, message_manager)
        second.notify_legacy_call("ChannelConnect", "3.2.0")

        assert len(message_manager.get_messages(admin.id)) == 1


class TestNotifyCurrentCall:
    """Entwarnung: läuft dieselbe Methode wieder über den aktuellen Pfad, wird der
    Hinweis zurückgesetzt — leise, ohne zusätzliche Mailbox-Nachricht (#358)."""

    def test_clears_state_without_sending_a_message(self, tmp_path):
        notifier, user_manager, message_manager = _make_notifier(tmp_path)
        admin = _make_admin(user_manager)
        notifier.notify_legacy_call("ChannelConnect", "3.2.0")
        assert len(message_manager.get_messages(admin.id)) == 1

        notifier.notify_current_call("ChannelConnect")

        assert len(message_manager.get_messages(admin.id)) == 1  # unverändert, keine neue Nachricht

    def test_a_later_legacy_call_notifies_again_after_recovery(self, tmp_path):
        notifier, user_manager, message_manager = _make_notifier(tmp_path)
        admin = _make_admin(user_manager)
        notifier.notify_legacy_call("ChannelConnect", "3.2.0")
        notifier.notify_current_call("ChannelConnect")

        notifier.notify_legacy_call("ChannelConnect", "3.2.0")

        assert len(message_manager.get_messages(admin.id)) == 2

    def test_unrelated_method_is_untouched(self, tmp_path):
        notifier, user_manager, message_manager = _make_notifier(tmp_path)
        admin = _make_admin(user_manager)
        notifier.notify_legacy_call("ChannelConnect", "3.2.0")

        notifier.notify_current_call("AgentConnect")  # war nie legacy — no-op

        assert len(message_manager.get_messages(admin.id)) == 1

    def test_no_active_notice_is_a_cheap_noop(self, tmp_path):
        notifier, _user_manager, _message_manager = _make_notifier(tmp_path)

        notifier.notify_current_call("SubmitText")  # darf nicht crashen, nichts zu tun
