import os

from werkzeug.security import generate_password_hash

import hannah.utils.db as db_module
from hannah.messages import MessageManager
from hannah.pending_updates import PendingUpdateNotifier
from hannah.user_manager import UserManager
from hannah.version_check import PublicVersions

HOUR = 3600


class _World:
    """Echte Manager gegen eine Wegwerf-SQLite-DB; Instanzen, öffentliche Versionen, Settings und
    Uhr sind von außen steuerbar."""

    def __init__(self, tmp_path):
        db_module.DB_PATH = os.path.join(str(tmp_path), "h.db")
        db_module.init_db()
        self.user_manager = UserManager(db_module.get_db)
        self.messages = MessageManager(db_module.get_db, user_manager=self.user_manager)
        # init_db() legt zwei Trust-Level-10-User an: den First-Run-Admin (stört nur beim Zählen) und
        # Hannahs eigenen User, der keine Nachricht bekommen darf.
        self.user_manager.get_user_by_username("admin").update(trust_level=0)
        self.hannah = self.user_manager.get_user_by_username("hannah")
        assert self.hannah.trust_level == 10
        self.instances = [{"component": "telegram", "instance_id": "pi", "version": "1.0.0"}]
        self.public = PublicVersions({"telegram": "1.1.0"}, True, 1)
        self.settings = {"enabled": True, "notify_admins": True, "notify_after_h": 24}
        self.now = 1_700_000_000.0
        self.notifier = self._make_notifier()

    def _make_notifier(self):
        return PendingUpdateNotifier(
            db_module.get_db, self.user_manager, self.messages,
            get_instances=lambda: self.instances,
            get_public_versions=lambda: self.public,
            get_settings=lambda: self.settings,
            clock=lambda: self.now,
        )

    def restart(self):
        self.notifier = self._make_notifier()

    def admin(self, username="leonie", trust_level=10):
        user = self.user_manager.create_user(username, generate_password_hash("x"), email=f"{username}@example.com")
        user.update(trust_level=trust_level)
        return user

    def inbox(self, user):
        return self.messages.get_messages(user.id)


def test_no_message_before_the_threshold(tmp_path):
    w = _World(tmp_path)
    admin = w.admin()

    w.notifier.check()
    w.now += 23 * HOUR
    w.notifier.check()

    assert w.inbox(admin) == []


def test_message_after_the_threshold_names_component_and_versions(tmp_path):
    w = _World(tmp_path)
    admin = w.admin()

    w.notifier.check()
    w.now += 24 * HOUR
    assert w.notifier.check() == 1

    [message] = w.inbox(admin)
    assert "telegram" in message["content"]
    assert "1.0.0" in message["content"] and "1.1.0" in message["content"]
    assert "24" in message["content"]


def test_only_trust_level_10_and_never_hannahs_own_user(tmp_path):
    w = _World(tmp_path)
    admin = w.admin("leonie")
    roomie = w.admin("rene", trust_level=8)

    w.notifier.check()
    w.now += 25 * HOUR
    w.notifier.check()

    assert len(w.inbox(admin)) == 1
    assert w.inbox(w.hannah) == []
    assert w.inbox(roomie) == []


def test_each_admin_is_notified_once_per_version(tmp_path):
    w = _World(tmp_path)
    admin = w.admin()

    w.notifier.check()
    w.now += 25 * HOUR
    w.notifier.check()
    w.now += 10 * HOUR
    w.notifier.check()

    assert len(w.inbox(admin)) == 1


def test_core_restart_does_not_resend_and_keeps_the_timer(tmp_path):
    w = _World(tmp_path)
    admin = w.admin()

    w.notifier.check()
    w.now += 12 * HOUR
    w.restart()
    w.notifier.check()
    assert w.inbox(admin) == []

    w.now += 13 * HOUR
    w.notifier.check()
    assert len(w.inbox(admin)) == 1

    w.restart()
    w.notifier.check()
    assert len(w.inbox(admin)) == 1


def test_a_newer_public_version_restarts_the_timer(tmp_path):
    w = _World(tmp_path)
    admin = w.admin()

    w.notifier.check()
    w.now += 20 * HOUR
    w.public = PublicVersions({"telegram": "1.2.0"}, True, 2)
    w.notifier.check()
    w.now += 20 * HOUR
    w.notifier.check()
    assert w.inbox(admin) == []

    w.now += 5 * HOUR
    w.notifier.check()
    [message] = w.inbox(admin)
    assert "1.2.0" in message["content"]


def test_a_newer_version_notifies_again_after_an_earlier_message(tmp_path):
    w = _World(tmp_path)
    admin = w.admin()

    w.notifier.check()
    w.now += 25 * HOUR
    w.notifier.check()
    w.public = PublicVersions({"telegram": "1.2.0"}, True, 2)
    w.notifier.check()
    w.now += 25 * HOUR
    w.notifier.check()

    assert len(w.inbox(admin)) == 2


def test_update_installed_resets_the_state(tmp_path):
    w = _World(tmp_path)
    admin = w.admin()

    w.notifier.check()
    w.now += 20 * HOUR
    w.instances = [{"component": "telegram", "instance_id": "pi", "version": "1.1.0"}]
    w.notifier.check()
    w.instances = [{"component": "telegram", "instance_id": "pi", "version": "1.0.0"}]
    w.public = PublicVersions({"telegram": "1.2.0"}, True, 2)
    w.notifier.check()
    w.now += 20 * HOUR
    w.notifier.check()

    assert w.inbox(admin) == []


def test_component_gone_for_a_while_keeps_the_timer(tmp_path):
    """Ein Neustart der Komponente darf den Zeitraum nicht von vorn beginnen lassen."""
    w = _World(tmp_path)
    admin = w.admin()

    w.notifier.check()
    w.now += 12 * HOUR
    w.instances = []
    w.notifier.check()
    w.now += 13 * HOUR
    w.instances = [{"component": "telegram", "instance_id": "pi", "version": "1.0.0"}]
    w.notifier.check()

    assert len(w.inbox(admin)) == 1


def test_unreachable_aggregator_neither_notifies_nor_resets(tmp_path):
    w = _World(tmp_path)
    admin = w.admin()

    w.notifier.check()
    w.now += 25 * HOUR
    w.public = PublicVersions({}, False, 0)
    assert w.notifier.check() == 0
    assert w.inbox(admin) == []

    w.public = PublicVersions({"telegram": "1.1.0"}, True, 3)
    w.notifier.check()
    assert len(w.inbox(admin)) == 1


def test_several_instances_of_one_component_give_one_message(tmp_path):
    w = _World(tmp_path)
    admin = w.admin()
    w.instances = [
        {"component": "telegram", "instance_id": "pi", "version": "1.0.0"},
        {"component": "telegram", "instance_id": "nuc", "version": "1.0.1"},
    ]

    w.notifier.check()
    w.now += 25 * HOUR
    w.notifier.check()

    [message] = w.inbox(admin)
    assert "1.0.0" in message["content"] and "1.0.1" in message["content"]


def test_one_message_per_component(tmp_path):
    w = _World(tmp_path)
    admin = w.admin()
    w.instances.append({"component": "voiceid", "instance_id": "pi", "version": "2.0.0"})
    w.public = PublicVersions({"telegram": "1.1.0", "voiceid": "2.1.0"}, True, 1)

    w.notifier.check()
    w.now += 25 * HOUR
    w.notifier.check()

    assert len(w.inbox(admin)) == 2


def test_admin_promoted_later_still_gets_the_message(tmp_path):
    w = _World(tmp_path)
    first = w.admin("leonie")

    w.notifier.check()
    w.now += 25 * HOUR
    w.notifier.check()
    second = w.admin("rene")
    w.notifier.check()

    assert len(w.inbox(first)) == 1
    assert len(w.inbox(second)) == 1


def test_switched_off_notifications_send_nothing_and_do_not_ask_the_aggregator(tmp_path):
    w = _World(tmp_path)
    admin = w.admin()
    w.settings["notify_admins"] = False
    asked = []
    w.notifier._get_public_versions = lambda: asked.append(1) or w.public

    w.notifier.check()
    w.now += 25 * HOUR
    w.notifier.check()

    assert w.inbox(admin) == []
    assert asked == []


def test_switched_off_version_check_sends_nothing(tmp_path):
    w = _World(tmp_path)
    admin = w.admin()
    w.settings["enabled"] = False

    w.notifier.check()
    w.now += 25 * HOUR
    w.notifier.check()

    assert w.inbox(admin) == []


def test_threshold_is_configurable(tmp_path):
    w = _World(tmp_path)
    admin = w.admin()
    w.settings["notify_after_h"] = 2

    w.notifier.check()
    w.now += 3 * HOUR
    w.notifier.check()

    [message] = w.inbox(admin)
    assert "2 Stunden" in message["content"]


def test_missing_settings_fall_back_to_the_defaults(tmp_path):
    w = _World(tmp_path)
    admin = w.admin()
    w.settings = {}

    w.notifier.check()
    w.now += 23 * HOUR
    w.notifier.check()
    assert w.inbox(admin) == []

    w.now += 2 * HOUR
    w.notifier.check()
    assert len(w.inbox(admin)) == 1
