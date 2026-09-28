"""
Benachrichtigt Admins über Komponenten, die Core noch über den eingefrorenen
N−1-Pfad ansprechen (hannah-proto#11, #359, #358).

Die eigentliche Erkennung sitzt im OutdatedComponentInterceptor
(hannah.grpc_interceptors) — der sieht bei jedem RPC, welcher Pfad gerufen
wurde, unabhängig von Registrierungsnachrichten einzelner Komponenten. Dieses
Modul kümmert sich nur um Entprellen (eine Mailbox-Nachricht pro Methode und
x-proto-version, nicht bei jedem Aufruf) und Entwarnung (läuft dieselbe
Methode später wieder über den aktuellen Pfad, gilt der Hinweis als erledigt).
"""
import logging
import threading
from typing import Callable

log = logging.getLogger(__name__)

_TABLE = "outdated_component_notices"


class OutdatedComponentNotifier:
    def __init__(self, db: Callable, user_manager, message_manager):
        """
        db: liefert eine pyorm.Database, z.B. hannah.utils.db.get_db.
        user_manager: für get_users_with_trust_level() (Admin-Empfänger).
        message_manager: für create_message() (Mailbox, #234).
        """
        self._db = db
        self._user_manager = user_manager
        self._message_manager = message_manager
        self._lock = threading.Lock()
        self._active: set[tuple[str, str]] = self._load_active()

    def _load_active(self) -> set[tuple[str, str]]:
        rows = self._db().execute(f'SELECT "method", "proto_version" FROM "{_TABLE}"').fetchall()
        return {(r[0], r[1]) for r in rows}

    def notify_legacy_call(self, method: str, proto_version: str) -> None:
        """method: bare RPC-Name (z.B. 'ChannelConnect'). proto_version: aus dem
        x-proto-version-Header, leer wenn der Client zu alt ist, um ihn zu senden."""
        key = (method, proto_version)
        with self._lock:
            if key in self._active:
                return
            self._active.add(key)

        db = self._db()
        db.execute(
            f'INSERT OR IGNORE INTO "{_TABLE}" ("method", "proto_version") VALUES (?, ?)',
            (method, proto_version),
        )
        db.commit()

        admins = self._user_manager.get_users_with_trust_level(10)
        content = (
            f"Eine Komponente spricht Hannah noch über das eingefrorene alte Protokoll an "
            f"(RPC: {method}, hannah-proto-Version: {proto_version or 'unbekannt'}). Sie "
            f"funktioniert ab dem nächsten großen Protokoll-Update nicht mehr — bitte aktualisieren."
        )
        for admin in admins:
            self._message_manager.create_message(user_id=admin.id, content=content, source="system")
        log.info(
            f"[outdated] Legacy-Aufruf erkannt: {method} (proto_version={proto_version!r}) — "
            f"{len(admins)} Admin(s) benachrichtigt"
        )

    def notify_current_call(self, method: str) -> None:
        """Läuft ein Aufruf derselben Methode wieder über den aktuellen Pfad, gilt ein
        zuvor gesetzter Hinweis als erledigt — leise, ohne zusätzliche Mailbox-Nachricht."""
        with self._lock:
            stale = {key for key in self._active if key[0] == method}
            if not stale:
                return
            self._active -= stale

        db = self._db()
        db.execute(f'DELETE FROM "{_TABLE}" WHERE "method" = ?', (method,))
        db.commit()
        log.info(f"[outdated] {method} läuft wieder über das aktuelle Protokoll — Hinweis zurückgesetzt")
