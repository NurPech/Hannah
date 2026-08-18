import logging
from typing import Callable, Optional

from hannah.models.message import Message

log = logging.getLogger(__name__)


class MessageManager:
    """Persistente, personengebundene Mailbox — dritter Notification-Typ neben
    Notify (Broadcast) und Announce (gezielte Sofort-TTS), die beide sofort
    abspielen. Eine Message wird konsumiert (gelöscht), sobald sie tatsächlich
    vorgelesen wurde, nicht schon beim Erzeugen. Refs #234."""

    def __init__(
        self,
        db: Callable,
        user_manager,
        on_created: Optional[Callable[[int], None]] = None,
        on_all_consumed: Optional[Callable[[int], None]] = None,
    ):
        """
        db: liefert eine pyorm.Database, z.B. hannah.utils.db.get_db.
        user_manager: für User.satellites (Signalisierung) und Trust-Level-Checks,
            gleiches Muster wie ActivityLogManager.
        on_created(user_id): Callback beim Anlegen — löst Signalton + gelbes LED auf
            den eigenen Satelliten des Users aus.
        on_all_consumed(user_id): Callback wenn count_pending(user_id) auf 0 fällt —
            löst das Zurücksetzen des LED-Zustands aus.
        """
        self._db = db
        self._user_manager = user_manager
        self._on_created = on_created or (lambda _u: None)
        self._on_all_consumed = on_all_consumed or (lambda _u: None)

    def _trust_level(self, user_id) -> int:
        user = self._user_manager.get_user_by_id(user_id) if user_id else None
        return user.trust_level if user else 0

    # ------------------------------------------------------------------
    # CRUD

    def create_message(self, user_id: int, content: str, source: str = "") -> dict:
        m = Message.create(self._db(), user_id=user_id, content=content, source=source)
        log.info(f"[messages] Message #{m.id} für user_id={user_id} angelegt (source={source!r}).")
        self._on_created(user_id)
        return m.to_dict()

    def get_messages(self, requestor_id: int, filter_user_id: int = 0) -> list[dict]:
        """Trust-Level >=10 kann filter_user_id fremd abfragen; sonst immer nur die
        eigenen Messages von requestor_id, unabhängig von filter_user_id (Muster
        aus ActivityLogManager.list_activity)."""
        target_user_id = (
            filter_user_id if (filter_user_id and self._trust_level(requestor_id) >= 10)
            else requestor_id
        )
        return [
            m.to_dict()
            for m in Message.select(self._db()).where(user_id=target_user_id).order_by("id").all()
        ]

    def count_pending(self, user_id) -> int:
        if not user_id:
            return 0
        return len(Message.select(self._db()).where(user_id=user_id).all())

    def consume_all(self, user_id) -> list[dict]:
        """Holt alle offenen Messages eines Users und löscht sie — Konsum bedeutet
        hier "wurde vorgelesen", nicht "wurde erzeugt"."""
        rows = Message.select(self._db()).where(user_id=user_id).order_by("id").all()
        result = [m.to_dict() for m in rows]
        for m in rows:
            m.delete()
        if result:
            log.info(f"[messages] {len(result)} Message(s) für user_id={user_id} konsumiert.")
            self._on_all_consumed(user_id)
        return result

    def delete_message(self, requestor_id: int, id: int) -> bool:
        """Dismiss ohne Vorlesen (gRPC DeleteMessage). Gleicher Trust-Level-Check wie
        get_messages: eigene Message oder Trust-Level >= 10."""
        m = Message.get(self._db(), id=id)
        if not m:
            return False
        if int(m.user_id) != int(requestor_id) and self._trust_level(requestor_id) < 10:
            return False
        user_id = m.user_id
        m.delete()
        if self.count_pending(user_id) == 0:
            self._on_all_consumed(user_id)
        return True
