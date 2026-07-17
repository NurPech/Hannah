from pyorm import BaseModel

class Satellite(BaseModel):
    __table__ = "satellites"
    __primary_key__ = "device_id"
    __slots__ = (
        "device_id", "seed", "display_name", "room_id", "owner_user_id", "last_seen", "paired_at", "created_at",
        "firmware_version", "update_available", "new_version",
        "_cached_owner",
    )

    def after_init(self):
        self._cached_owner = None

    @property
    def owner(self):
        """Gibt den zugeordneten User zurück, oder None falls kein Owner gesetzt."""
        if not self.owner_user_id:
            return None
        if self._cached_owner is None:
            from hannah.models.user import User
            self._cached_owner = User.get(self._db, id=self.owner_user_id)
        return self._cached_owner

    def set_owner(self, user_id):
        """Setzt (oder löscht mit None) die Personen-Zuordnung dieses Satelliten."""
        self._cached_owner = None
        self.update(owner_user_id=user_id)