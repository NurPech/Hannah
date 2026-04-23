"""
Hannah User Registry

Persistente Nutzerdatenbank die ioBroker Residents als Quelle der Wahrheit verwendet,
aber eigene Felder (UUID, Trust Level, Linked Accounts) hinzufügt.

Sync-Strategie:
  - Beim Start einmalig synchronisieren
  - Danach alle sync_interval Sekunden im Hintergrund
  - Neue Roomies werden angelegt, gelöschte werden deaktiviert (nicht gelöscht)

Dieses Modul hat kein MQTT/gRPC — es ist reine Datenhaltung mit einem
fetch-Callable als einziger externer Abhängigkeit. So bleibt es
leicht wrappbar (REST, gRPC, etc.) ohne die Logik zu duplizieren.
"""
import logging
import sqlite3
import threading
import time
import uuid as _uuid
from typing import Callable, Optional

log = logging.getLogger(__name__)


class UserRegistry:
    def __init__(
        self,
        cfg: dict,
        fetch_roomies: Callable[[], dict[str, str]],
        hannah_roomie: str = "hannah",
    ):
        """
        cfg           : user_registry-Abschnitt aus config.yaml
        fetch_roomies : fn() → {roomie_id: display_name}
                        Wird beim Sync aufgerufen (z.B. iobroker.list_roomies).
        hannah_roomie : Roomie-ID von Hannah selbst — bekommt immer trust_level=10.
        """
        self._db_path       = cfg.get("db_path", "hannah_users.db")
        self._sync_interval = int(cfg.get("sync_interval", 60))
        self._fetch_roomies = fetch_roomies
        self._hannah_roomie = hannah_roomie
        self._lock          = threading.Lock()
        self._init_db()

    # ------------------------------------------------------------------
    # Schema

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    uuid            TEXT    PRIMARY KEY,
                    roomie_id       TEXT    UNIQUE NOT NULL,
                    display_name    TEXT    NOT NULL,
                    trust_level     INTEGER NOT NULL DEFAULT 5,
                    system_messages INTEGER NOT NULL DEFAULT 0,
                    active          INTEGER NOT NULL DEFAULT 1,
                    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
                    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
                );
            """)
            # Migration: system_messages column for existing DBs
            existing = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
            if "system_messages" not in existing:
                conn.execute("ALTER TABLE users ADD COLUMN system_messages INTEGER NOT NULL DEFAULT 0")
            conn.executescript("""

                CREATE TABLE IF NOT EXISTS linked_accounts (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_uuid   TEXT    NOT NULL REFERENCES users(uuid),
                    service     TEXT    NOT NULL,
                    account_id  TEXT    NOT NULL,
                    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
                    UNIQUE(service, account_id)
                );
            """)

    # ------------------------------------------------------------------
    # Sync mit ioBroker

    def sync(self) -> tuple[int, int]:
        """
        Gleicht Registry mit ioBroker ab.
        Gibt (added, deactivated) zurück.
        """
        try:
            roomies = self._fetch_roomies()
        except Exception as e:
            log.warning(f"UserRegistry sync fehlgeschlagen: {e}")
            return 0, 0

        added = deactivated = 0

        with self._lock, self._connect() as conn:
            existing: dict[str, str] = {
                row[0]: row[1]
                for row in conn.execute(
                    "SELECT roomie_id, uuid FROM users WHERE active = 1"
                )
            }

            # Neue Roomies anlegen / Hannah's trust_level sicherstellen
            for roomie_id, display_name in roomies.items():
                is_hannah = (roomie_id == self._hannah_roomie)
                trust_level = 10 if is_hannah else 5
                if roomie_id not in existing:
                    new_uuid = str(_uuid.uuid4())
                    conn.execute(
                        "INSERT INTO users (uuid, roomie_id, display_name, trust_level)"
                        " VALUES (?, ?, ?, ?)",
                        (new_uuid, roomie_id, display_name, trust_level),
                    )
                    log.info(
                        f"UserRegistry: +{display_name!r} ({roomie_id})"
                        f" trust={trust_level} → {new_uuid}"
                    )
                    added += 1
                elif is_hannah:
                    # Hannah bekommt immer trust_level=10, auch wenn sie schon existiert
                    conn.execute(
                        "UPDATE users SET trust_level = 10, updated_at = datetime('now')"
                        " WHERE roomie_id = ? AND trust_level != 10",
                        (roomie_id,),
                    )

            # In ioBroker gelöschte Roomies deaktivieren
            for roomie_id in existing:
                if roomie_id not in roomies:
                    conn.execute(
                        "UPDATE users SET active = 0, updated_at = datetime('now')"
                        " WHERE roomie_id = ?",
                        (roomie_id,),
                    )
                    log.info(f"UserRegistry: -{roomie_id!r} (in ioBroker gelöscht) → deaktiviert")
                    deactivated += 1

        if added or deactivated:
            log.info(f"UserRegistry: Sync abgeschlossen (+{added} / -{deactivated})")
        return added, deactivated

    def start_sync_loop(self):
        """Sync einmalig jetzt, dann alle sync_interval Sekunden im Hintergrund."""
        self.sync()

        def _loop():
            while True:
                time.sleep(self._sync_interval)
                self.sync()

        t = threading.Thread(target=_loop, daemon=True, name="user-registry-sync")
        t.start()
        log.info(f"UserRegistry: Hintergrund-Sync alle {self._sync_interval}s aktiv.")

    # ------------------------------------------------------------------
    # Abfragen

    def get_all(self, include_inactive: bool = False) -> list[dict]:
        """Gibt alle Nutzer mit ihren verknüpften Accounts zurück."""
        where = "" if include_inactive else "WHERE u.active = 1"
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(f"""
                SELECT
                    u.uuid, u.roomie_id, u.display_name,
                    u.trust_level, u.system_messages, u.active,
                    u.created_at, u.updated_at,
                    GROUP_CONCAT(la.service || ':' || la.account_id) AS linked_accounts
                FROM users u
                LEFT JOIN linked_accounts la ON la.user_uuid = u.uuid
                {where}
                GROUP BY u.uuid
                ORDER BY u.display_name
            """).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_by_roomie(self, roomie_id: str) -> Optional[dict]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM users WHERE roomie_id = ? AND active = 1", (roomie_id,)
            ).fetchone()
        return _row_to_dict(row) if row else None

    def get_by_uuid(self, user_uuid: str) -> Optional[dict]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM users WHERE uuid = ?", (user_uuid,)
            ).fetchone()
        return _row_to_dict(row) if row else None

    def get_by_linked_account(self, service: str, account_id: str) -> Optional[dict]:
        """Sucht einen Nutzer anhand eines verknüpften Accounts (z.B. Telegram-ID)."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("""
                SELECT u.* FROM users u
                JOIN linked_accounts la ON la.user_uuid = u.uuid
                WHERE la.service = ? AND la.account_id = ? AND u.active = 1
            """, (service, str(account_id))).fetchone()
        return _row_to_dict(row) if row else None

    # ------------------------------------------------------------------
    # Mutationen

    def link_account(self, roomie_id: str, service: str, account_id: str) -> bool:
        """Verknüpft einen externen Account (z.B. Telegram) mit einem Roomie."""
        user = self.get_by_roomie(roomie_id)
        if not user:
            log.warning(f"UserRegistry: link_account — Roomie {roomie_id!r} nicht gefunden")
            return False
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO linked_accounts (user_uuid, service, account_id)"
                " VALUES (?, ?, ?)",
                (user["uuid"], service, str(account_id)),
            )
        log.info(f"UserRegistry: {roomie_id} → {service}:{account_id} verknüpft")
        return True

    def unlink_account(self, service: str, account_id: str) -> bool:
        with self._lock, self._connect() as conn:
            c = conn.execute(
                "DELETE FROM linked_accounts WHERE service = ? AND account_id = ?",
                (service, str(account_id)),
            )
        return c.rowcount > 0

    def set_trust_level(self, roomie_id: str, level: int) -> bool:
        level = max(0, min(10, level))
        with self._lock, self._connect() as conn:
            c = conn.execute(
                "UPDATE users SET trust_level = ?, updated_at = datetime('now')"
                " WHERE roomie_id = ? AND active = 1",
                (level, roomie_id),
            )
        return c.rowcount > 0

    def set_system_messages(self, roomie_id: str, enabled: bool) -> bool:
        """Aktiviert/deaktiviert System-Notifications für einen Nutzer."""
        with self._lock, self._connect() as conn:
            c = conn.execute(
                "UPDATE users SET system_messages = ?, updated_at = datetime('now')"
                " WHERE roomie_id = ? AND active = 1",
                (1 if enabled else 0, roomie_id),
            )
        return c.rowcount > 0

    def get_system_message_recipients(self) -> list[dict]:
        """Gibt alle aktiven Nutzer zurück, die System-Notifications erhalten sollen."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT u.*, GROUP_CONCAT(la.service || ':' || la.account_id) AS linked_accounts
                FROM users u
                LEFT JOIN linked_accounts la ON la.user_uuid = u.uuid
                WHERE u.active = 1 AND u.system_messages = 1
                GROUP BY u.uuid
            """).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    # linked_accounts als Liste parsen statt kommasepariertem String
    raw = d.get("linked_accounts")
    if raw:
        accounts: dict[str, str] = {}
        for entry in raw.split(","):
            if ":" in entry:
                svc, aid = entry.split(":", 1)
                accounts[svc] = aid
        d["linked_accounts"] = accounts
    else:
        d["linked_accounts"] = {}
    return d
