import os
import secrets
import logging
from werkzeug.security import generate_password_hash
from pyorm import Database

_log = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "hannah.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS "users" (
	"id"	INTEGER NOT NULL,
	"username"	TEXT NOT NULL,
	"display_name"	TEXT NOT NULL,
	"email"	TEXT NOT NULL,
	"password_hash"	TEXT NOT NULL,
	"trust_level"	NUMERIC NOT NULL DEFAULT 5,
	"mood_level"	NUMERIC NOT NULL DEFAULT 5,
	"system_messages"	INTEGER NOT NULL DEFAULT 0,
	"type"	TEXT NOT NULL,
	"is_active"	INTEGER NOT NULL DEFAULT 1,
	UNIQUE("email"),
	PRIMARY KEY("id" AUTOINCREMENT),
	UNIQUE("username")
);

CREATE TABLE IF NOT EXISTS "linked_accounts" (
	"id"	INTEGER,
	"user_id"	INTEGER,
	"provider"	TEXT NOT NULL,
	"external_id"	TEXT NOT NULL,
	"provider_payload"	TEXT,
	PRIMARY KEY("id" AUTOINCREMENT),
	UNIQUE("provider","external_id"),
	UNIQUE("user_id","provider"),
	FOREIGN KEY("user_id") REFERENCES "users"("id") ON UPDATE CASCADE ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS "rooms" (
	"room_id"	TEXT NOT NULL,
	"display_name"	TEXT NOT NULL,
	"created_at"	TEXT NOT NULL DEFAULT (datetime('now')),
	PRIMARY KEY("room_id")
);

CREATE TABLE IF NOT EXISTS "groups" (
	"group_id"	TEXT NOT NULL,
	"display_name"	TEXT NOT NULL,
	"created_at"	TEXT NOT NULL DEFAULT (datetime('now')),
	PRIMARY KEY("group_id")
);

CREATE TABLE IF NOT EXISTS "group_rooms" (
	"group_id"	TEXT NOT NULL,
	"room_id"	TEXT NOT NULL,
	PRIMARY KEY("group_id","room_id"),
	FOREIGN KEY("group_id") REFERENCES "groups"("group_id") ON DELETE CASCADE,
	FOREIGN KEY("room_id") REFERENCES "rooms"("room_id") ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS "satellites" (
	"device_id"	TEXT NOT NULL,
	"seed"	TEXT,
	"display_name"	TEXT,
	"room_id"	TEXT,
    "owner_user_id"	INTEGER,
	"last_seen"	TEXT,
	"paired_at"	TEXT,
	"created_at"	TEXT NOT NULL DEFAULT (datetime('now')),
	"firmware_version"	TEXT,
	"update_available"	INTEGER NOT NULL DEFAULT 0,
	"new_version"	TEXT,
	"smalltalk_followup_listen"	INTEGER NOT NULL DEFAULT 0,
	PRIMARY KEY("device_id"),
	FOREIGN KEY("room_id") REFERENCES "rooms"("room_id") ON DELETE SET NULL,
    FOREIGN KEY("owner_user_id") REFERENCES "users"("id") ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS "triggers" (
	"id"	TEXT NOT NULL,
	"when"	TEXT NOT NULL,
	"cancel_when"	TEXT,
	"on_response"	TEXT,
	"actions"	TEXT,
	"say"	TEXT,
	"ask"	TEXT,
	"rephrase"	INTEGER NOT NULL DEFAULT 0,
	"room"	TEXT NOT NULL DEFAULT 'all',
	"cooldown"	INTEGER NOT NULL DEFAULT 3600,
	"delay"	TEXT,
	"created_at"	TEXT NOT NULL DEFAULT (datetime('now')),
	PRIMARY KEY("id")
);

CREATE TABLE IF NOT EXISTS "alarms" (
	"id"	INTEGER NOT NULL,
	"satellite_id"	TEXT NOT NULL,
	"time"	TEXT NOT NULL,
	"weekdays"	TEXT,
	"skip_dates"	TEXT NOT NULL DEFAULT '[]',
	"one_shot_date"	TEXT,
	"enabled"	INTEGER NOT NULL DEFAULT 1,
	"label"	TEXT,
	"user_id"	INTEGER NOT NULL,
	"created_at"	TEXT NOT NULL DEFAULT (datetime('now')),
	PRIMARY KEY("id" AUTOINCREMENT),
	FOREIGN KEY("satellite_id") REFERENCES "satellites"("device_id") ON DELETE CASCADE,
	FOREIGN KEY("user_id") REFERENCES "users"("id") ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS "settings_category" (
	"id"	INTEGER NOT NULL,
	"name"	TEXT NOT NULL,
	"parent"	INTEGER,
	PRIMARY KEY("id" AUTOINCREMENT),
	UNIQUE("name"),
	FOREIGN KEY("parent") REFERENCES "settings_category"("id") ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS "settings" (
	"id"	INTEGER NOT NULL,
	"category"	INTEGER NOT NULL,
	"name"	TEXT NOT NULL,
	"value"	TEXT NOT NULL,
	PRIMARY KEY("id" AUTOINCREMENT),
	UNIQUE("category","name"),
	FOREIGN KEY("category") REFERENCES "settings_category"("id") ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS "ble_tags" (
	"id"	INTEGER NOT NULL,
	"mac_address"	TEXT NOT NULL,
	"label"	TEXT NOT NULL,
	"user_id"	INTEGER,
	"created_at"	TEXT NOT NULL DEFAULT (datetime('now')),
	PRIMARY KEY("id" AUTOINCREMENT),
	UNIQUE("mac_address"),
	FOREIGN KEY("user_id") REFERENCES "users"("id") ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS "cars" (
	"id"	INTEGER NOT NULL,
	"name"	TEXT,
	"topic_prefix"	TEXT NOT NULL,
	"home_address"	TEXT,
	"created_at"	TEXT NOT NULL DEFAULT (datetime('now')),
	PRIMARY KEY("id" AUTOINCREMENT),
	UNIQUE("topic_prefix")
);

CREATE TABLE IF NOT EXISTS "user_to_car" (
	"user_id"	INTEGER NOT NULL,
	"car_id"	INTEGER NOT NULL,
	PRIMARY KEY("user_id","car_id"),
	FOREIGN KEY("user_id") REFERENCES "users"("id") ON DELETE CASCADE,
	FOREIGN KEY("car_id") REFERENCES "cars"("id") ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS "user_automations" (
	"user_id"	INTEGER NOT NULL,
	"automation"	TEXT NOT NULL,
	PRIMARY KEY("user_id","automation"),
	FOREIGN KEY("user_id") REFERENCES "users"("id") ON DELETE CASCADE
);
"""


def get_db():
    """Frische Connection pro Aufruf — Hannah Core läuft nicht request-scoped wie Flask,
    sondern aus gRPC-Handlern/MQTT-Callbacks/Telegram, daher kein g-Caching."""
    # SQLiteDialect.connect() setzt bereits check_same_thread=False + row_factory=Row,
    # aber keine Pragmas — die bleiben Hannah-spezifisch und werden hier weiterhin explizit gesetzt.
    db = Database.sqlite(DB_PATH)
    db.connection.execute("PRAGMA journal_mode=WAL")
    db.connection.execute("PRAGMA foreign_keys=ON")
    return db


def _existing_tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _col_names(db, table):
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def init_db():
    db = get_db()
    # executescript() ist SQLite-spezifisches Bootstrapping, kein Teil der
    # dialectorm-Abstraktion — daher bewusst über die rohe Connection statt db.execute().
    db.connection.executescript(SCHEMA)

    # Additive column migrations for tables that already existed before a column was
    # introduced — executescript()'s CREATE TABLE IF NOT EXISTS is a no-op on a table
    # that's already there, so new columns need an explicit ALTER TABLE here.
    if "actions" not in _col_names(db, "triggers"):
        db.execute('ALTER TABLE "triggers" ADD COLUMN "actions" TEXT')
        db.commit()

    if "owner_user_id" not in _col_names(db, "satellites"):
        db.execute('ALTER TABLE "satellites" ADD COLUMN "owner_user_id" INTEGER REFERENCES "users"("id")')
        db.commit()

    if "firmware_version" not in _col_names(db, "satellites"):
        db.execute('ALTER TABLE "satellites" ADD COLUMN "firmware_version" TEXT')
        db.execute('ALTER TABLE "satellites" ADD COLUMN "update_available" INTEGER NOT NULL DEFAULT 0')
        db.execute('ALTER TABLE "satellites" ADD COLUMN "new_version" TEXT')
        db.commit()

    if "name" not in _col_names(db, "cars"):
        db.execute('ALTER TABLE "cars" ADD COLUMN "name" TEXT')
        db.commit()

    if "smalltalk_followup_listen" not in _col_names(db, "satellites"):
        db.execute('ALTER TABLE "satellites" ADD COLUMN "smalltalk_followup_listen" INTEGER NOT NULL DEFAULT 0')
        db.commit()

    # --- First-run: create admin account if no users exist ---
    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        pw = secrets.token_urlsafe(16)
        db.execute(
            "INSERT INTO users (username, display_name, email, password_hash, trust_level, mood_level, system_messages, type, is_active) VALUES (?,?,?,?,?,?,?,?,?)",
            # TODO: get username from configfile
            ("hannah", "Hannah", "hannah@localhost", generate_password_hash(secrets.token_urlsafe(16)), 10, 10, 1, "roomie", 1)
        )
        db.execute(
            "INSERT INTO users (username, display_name, email, password_hash, trust_level, mood_level, system_messages, type, is_active) VALUES (?,?,?,?,?,?,?,?,?)",
            ("admin", "Admin", "admin@localhost", generate_password_hash(pw), 10, 10, 1, "roomie", 1)
        )
        db.commit()
        print(f"\n{'='*55}")
        print(f"  First-run: admin account created")
        print(f"  Username : admin")
        print(f"  Password : {pw}")
        print(f"  Please change the password after first login!")
        print(f"{'='*55}\n")
