"""
Hannah Presence-Source Registry

Verwaltet Presence-Fusion-Rohsignalquellen (source_type/reference/home_confidence/
away_confidence pro User) als eigenes DB-Modell statt als JSON-Blob im generischen
Settings-System (hannah#294, analog zu BleTagManager/#115). CRUD fürs Admin-UI;
PresenceManager liest daraus beim Start bzw. bei Änderungen die aktive Quellenliste.
"""
import sqlite3
from typing import Callable, Optional

from hannah.models.presence_source import PresenceSource as PresenceSourceModel


class PresenceSourceManager:
    def __init__(self, db: Callable):
        self._db = db

    def get_source_records(self) -> list[dict]:
        return [s.to_dict() for s in PresenceSourceModel.select(self._db()).all()]

    def create_source(self, user_id: int, source_type: str, reference: str,
                       home_confidence: float, away_confidence: float, enabled: bool = True) -> Optional[dict]:
        try:
            s = PresenceSourceModel.create(
                self._db(), user_id=user_id, source_type=source_type, reference=reference,
                home_confidence=home_confidence, away_confidence=away_confidence, enabled=int(enabled),
            )
        except sqlite3.IntegrityError:
            return None
        return s.to_dict()

    def update_source(self, id: int, user_id: int, source_type: str, reference: str,
                       home_confidence: float, away_confidence: float, enabled: bool) -> bool:
        s = PresenceSourceModel.get(self._db(), id=id)
        if not s:
            return False
        s.update(
            user_id=user_id, source_type=source_type, reference=reference,
            home_confidence=home_confidence, away_confidence=away_confidence, enabled=int(enabled),
        )
        return True

    def delete_source(self, id: int) -> bool:
        s = PresenceSourceModel.get(self._db(), id=id)
        if not s:
            return False
        s.delete()
        return True
