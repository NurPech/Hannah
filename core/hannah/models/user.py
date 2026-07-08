from hannah.utils import EventEmitterMixin
from pyorm import BaseModel

class User(BaseModel, EventEmitterMixin):
    __table__ = "users"
    __primary_key__ = "id"
    __slots__ = (
        "id", "username", "display_name","email", "password_hash", "trust_level", "mood_level", "system_messages","is_active", "type", "_db", "_cached_linked_accounts", "_presence"
    )

    def after_init(self):
        """Wird vom BaseModel am Ende von __init__ aufgerufen."""
        self._cached_linked_accounts = None
        self._presence = False

    @property
    def presence(self):
        """Gibt den aktuellen Präsenzstatus dieses Users zurück."""
        return self._presence
    
    @presence.setter
    def presence(self, value):
        if self._presence != value:
            self._presence = value
            if self._presence:
                self._emit("arrival")
            else:
                self._emit("departure")

    @property
    def mood(self):
        """Gibt die aktuelle Stimmung dieses Users zurück."""
        return self.mood_level
    
    @mood.setter
    def mood(self, value):
        if self.mood_level != value:
            self.mood_level = value
            self._emit("mood_change", value)
            self.save()

    @property
    def linked_accounts(self):
        """Gibt eine Liste aller LinkedAccounts dieses Users zurück."""
        if not self._db or not self.id:
            return []
            
        if self._cached_linked_accounts is None:
            from hannah.models.linked_account import LinkedAccount
            self._cached_linked_accounts = LinkedAccount.select(self._db).where("user_id = ?", self.id).all()
            
        return self._cached_linked_accounts
    
    def get_linked_account(self, provider):
        """Sucht einen spezifischen LinkedAccount dieses Users."""
        from hannah.models.linked_account import LinkedAccount
        return LinkedAccount.select(self._db).where("provider = ? AND user_id = ?", provider, self.id).first()
    
    def clear_linked_accounts_cache(self):
        """Leert den internen Cache, damit beim nächsten Zugriff frisch geladen wird."""
        self._cached_linked_accounts = None

    def link_account(self, provider, external_id, provider_payload=None):
        """Verknüpft diesen User mit einem externen Account."""
        from hannah.models.linked_account import LinkedAccount
        la = LinkedAccount.create(
            self._db,
            user_id=self.id,
            provider=provider,
            external_id=external_id,
            provider_payload=provider_payload
        )
        self.clear_linked_accounts_cache()
        return la

    def unlink_account(self, provider):
        """Entfernt die Verknüpfung dieses Users mit einem externen Account."""
        la = self.get_linked_account(provider)
        if la:
            la.delete()
            self.clear_linked_accounts_cache()

    @property
    def satellites(self):
        """Gibt alle Satelliten zurück, die diesem User als Owner zugeordnet sind."""
        from hannah.models.satellite import Satellite
        if not self._db or not self.id:
            return []
        return Satellite.select(self._db).where(owner_user_id=self.id).all()

    @property
    def ble_tags(self):
        """Gibt alle BLE-Tags zurück, die diesem User als Owner zugeordnet sind."""
        from hannah.models.ble_tag import BleTag
        if not self._db or not self.id:
            return []
        return BleTag.select(self._db).where(user_id=self.id).all()

    @property
    def cars(self):
        """Gibt alle Autos zurück, bei denen dieser User als Owner eingetragen ist (user_to_car-Pivot)."""
        from hannah.models.car import Car
        if not self._db or not self.id:
            return []
        return Car.select(self._db).join(
            "user_to_car utc", on="utc.car_id = cars.id"
        ).where("utc.user_id = ?", self.id).all()