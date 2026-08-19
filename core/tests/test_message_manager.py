import os
import sqlite3

import pytest
from werkzeug.security import generate_password_hash

import hannah.utils.db as db_module
from hannah.messages import MessageManager
from hannah.models.message import Message
from hannah.models.user import User
from hannah.user_manager import UserManager


def _create_user(db, username="leonie", trust_level=5) -> int:
    User.create(
        db(), username=username, display_name=username, email=f"{username}@example.com",
        password_hash=generate_password_hash("x"), trust_level=trust_level, mood_level=5,
        system_messages=0, type="roomie", is_active=1,
    )
    return User.get(db(), username=username).id


@pytest.fixture
def db(tmp_path):
    db_module.DB_PATH = os.path.join(str(tmp_path), "h.db")
    db_module.init_db()
    return db_module.get_db


@pytest.fixture
def manager(db):
    return MessageManager(db=db, user_manager=UserManager(db=db))


class TestDeleteMessage:
    """#238: Löschen einer Message, auf die eine andere per reply_to_id verweist,
    darf nicht mit FOREIGN KEY constraint failed abbrechen."""

    def test_delete_message_with_reply_sets_reply_to_id_null(self, db, manager):
        user_id = _create_user(db)
        parent = manager.create_message(user_id, "parent")
        manager.create_message(user_id, "reply", sender_user_id=user_id, reply_to_id=parent["id"])

        assert manager.delete_message(user_id, parent["id"]) is True

        remaining = manager.get_messages(user_id)
        assert len(remaining) == 1
        assert remaining[0]["content"] == "reply"
        assert remaining[0]["reply_to_id"] is None


class TestMessagesFkMigration:
    """#238: Reproduziert eine DB, die den additiven ALTER-TABLE-Migrationspfad
    durchlaufen hat (messages-Tabelle existierte schon vor sender_user_id/reply_to_id) —
    genau der Zustand, in dem die FK bislang ohne ON DELETE SET NULL landete."""

    @pytest.fixture
    def migrated_db(self, tmp_path):
        db_path = os.path.join(str(tmp_path), "h.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE "messages" (
                "id"	INTEGER NOT NULL,
                "user_id"	INTEGER NOT NULL,
                "content"	TEXT NOT NULL,
                "source"	TEXT,
                "created_at"	TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY("id" AUTOINCREMENT)
            )
            """
        )
        conn.commit()
        conn.close()

        db_module.DB_PATH = db_path
        db_module.init_db()
        return db_module.get_db

    def test_reply_to_id_fk_has_on_delete_set_null_after_migration(self, migrated_db):
        fks = migrated_db().execute('PRAGMA foreign_key_list("messages")').fetchall()
        by_column = {fk[3]: fk[6] for fk in fks}

        assert by_column["reply_to_id"] == "SET NULL"
        assert by_column["sender_user_id"] == "SET NULL"

    def test_delete_referenced_message_does_not_raise(self, migrated_db):
        db = migrated_db
        user_id = _create_user(db)
        manager = MessageManager(db=db, user_manager=UserManager(db=db))
        parent = manager.create_message(user_id, "parent")
        manager.create_message(user_id, "reply", reply_to_id=parent["id"])

        assert manager.delete_message(user_id, parent["id"]) is True
