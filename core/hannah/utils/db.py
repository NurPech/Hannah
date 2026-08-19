import datetime
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
	"last_restart_at"	TEXT,
	"last_reported_restart_count"	INTEGER,
	PRIMARY KEY("device_id"),
	FOREIGN KEY("room_id") REFERENCES "rooms"("room_id") ON DELETE SET NULL,
    FOREIGN KEY("owner_user_id") REFERENCES "users"("id") ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS "group_satellites" (
	"group_id"	TEXT NOT NULL,
	"device_id"	TEXT NOT NULL,
	PRIMARY KEY("group_id","device_id"),
	FOREIGN KEY("group_id") REFERENCES "groups"("group_id") ON DELETE CASCADE,
	FOREIGN KEY("device_id") REFERENCES "satellites"("device_id") ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS "satellite_restarts" (
	"id"	INTEGER NOT NULL,
	"device_id"	TEXT NOT NULL,
	"reported_at"	TEXT NOT NULL DEFAULT (datetime('now')),
	"restart_reason"	TEXT,
	"restart_count"	INTEGER,
	PRIMARY KEY("id" AUTOINCREMENT),
	FOREIGN KEY("device_id") REFERENCES "satellites"("device_id") ON DELETE CASCADE
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

CREATE TABLE IF NOT EXISTS "messages" (
	"id"	INTEGER NOT NULL,
	"user_id"	INTEGER NOT NULL,
	"content"	TEXT NOT NULL,
	"source"	TEXT,
	"sender_user_id"	INTEGER,
	"reply_to_id"	INTEGER,
	"created_at"	TEXT NOT NULL DEFAULT (datetime('now')),
	PRIMARY KEY("id" AUTOINCREMENT),
	FOREIGN KEY("user_id") REFERENCES "users"("id") ON DELETE CASCADE,
	FOREIGN KEY("sender_user_id") REFERENCES "users"("id") ON DELETE SET NULL,
	FOREIGN KEY("reply_to_id") REFERENCES "messages"("id") ON DELETE SET NULL
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

    if "last_restart_at" not in _col_names(db, "satellites"):
        db.execute('ALTER TABLE "satellites" ADD COLUMN "last_restart_at" TEXT')
        # Bestandssatelliten auf "jetzt" statt NULL/überfällig setzen (#162) — sonst
        # sieht die gesamte Flotte direkt nach diesem Deploy gleichzeitig überfällig aus.
        db.execute(
            'UPDATE "satellites" SET "last_restart_at" = ? WHERE "last_restart_at" IS NULL',
            (datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),),
        )
        db.commit()

    if "last_reported_restart_count" not in _col_names(db, "satellites"):
        db.execute('ALTER TABLE "satellites" ADD COLUMN "last_reported_restart_count" INTEGER')
        db.commit()

    if "smalltalk_followup_listen" not in _col_names(db, "satellites"):
        db.execute('ALTER TABLE "satellites" ADD COLUMN "smalltalk_followup_listen" INTEGER NOT NULL DEFAULT 0')
        db.commit()

    if "sender_user_id" not in _col_names(db, "messages"):
        db.execute('ALTER TABLE "messages" ADD COLUMN "sender_user_id" INTEGER REFERENCES "users"("id")')
        db.execute('ALTER TABLE "messages" ADD COLUMN "reply_to_id" INTEGER REFERENCES "messages"("id")')
        db.commit()

    # #238: Die additive Migration oben (ADD COLUMN "sender_user_id"/"reply_to_id")
    # konnte kein "ON DELETE SET NULL" setzen — SQLite unterstützt das nachträgliche
    # Ändern einer REFERENCES-Klausel per ALTER TABLE nicht. Damit hat jede DB, die
    # diesen Pfad durchlaufen hat (statt frisch per CREATE TABLE angelegt zu werden),
    # FKs ohne ON DELETE SET NULL und bricht beim Löschen referenzierter Messages/User
    # mit "FOREIGN KEY constraint failed" ab. Fix: Tabelle einmalig gegen das aktuelle
    # SCHEMA (mit korrektem ON DELETE SET NULL) neu aufbauen — klassische SQLite-FK-
    # Migration (rename → recreate → copy → drop), da SQLite kein ALTER COLUMN kennt.
    if "messages" in _existing_tables(db):
        needs_fk_fix = any(
            fk[3] in ("sender_user_id", "reply_to_id") and fk[6] != "SET NULL"
            for fk in db.execute('PRAGMA foreign_key_list("messages")').fetchall()
        )
        if needs_fk_fix:
            db.connection.execute("PRAGMA foreign_keys=OFF")
            db.execute('ALTER TABLE "messages" RENAME TO "messages_old_238"')
            db.connection.executescript(SCHEMA)
            db.execute(
                'INSERT INTO "messages" (id, user_id, content, source, sender_user_id, reply_to_id, created_at) '
                'SELECT id, user_id, content, source, sender_user_id, reply_to_id, created_at FROM "messages_old_238"'
            )
            db.execute('DROP TABLE "messages_old_238"')
            db.commit()
            db.connection.execute("PRAGMA foreign_keys=ON")
            _log.info("DB-Migration #238: messages-Tabelle mit korrektem ON DELETE SET NULL neu angelegt")

    # #56: Gruppen referenzieren jetzt Satelliten direkt statt Räume — group_rooms wird
    # einmalig nach group_satellites übernommen (jeder Satellit, der laut DB aktuell im
    # migrierten Raum sitzt) und danach gelöscht. Läuft nur einmal, da group_rooms danach
    # nicht mehr existiert (SCHEMA legt die Tabelle für Neuinstallationen nicht mehr an).
    if "group_rooms" in _existing_tables(db):
        rows = db.execute("SELECT group_id, room_id FROM group_rooms").fetchall()
        for group_id, room_id in rows:
            for (device_id,) in db.execute('SELECT device_id FROM satellites WHERE room_id = ?', (room_id,)).fetchall():
                db.execute(
                    'INSERT OR IGNORE INTO group_satellites (group_id, device_id) VALUES (?, ?)',
                    (group_id, device_id),
                )
        db.execute("DROP TABLE group_rooms")
        db.commit()
        _log.info(f"DB-Migration #56: {len(rows)} group_rooms-Zeile(n) nach group_satellites übernommen, group_rooms gelöscht")

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
