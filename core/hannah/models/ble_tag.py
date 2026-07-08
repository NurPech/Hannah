from pyorm import BaseModel

class BleTag(BaseModel):
    __table__ = "ble_tags"
    __primary_key__ = "id"
    __slots__ = ("id", "mac_address", "label", "user_id", "created_at")

    @property
    def user(self):
        """Gibt den zugeordneten User zurück, sofern gesetzt (sonst None)."""
        from hannah.models.user import User
        if not self._db or not self.user_id:
            return None
        return User.get(self._db, id=self.user_id)