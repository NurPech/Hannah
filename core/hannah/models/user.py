from hannah.utils import EventEmitterMixin
from pyorm import BaseModel

class User(BaseModel, EventEmitterMixin):
    __table__ = "users"
    __primary_key__ = "id"
    __slots__ = (
        "id", "username", "display_name","email", "password_hash", "trust_level", "mood_level", "system_messages","is_active", "type", "_db", "_cached_linked_accounts", "_cached_enabled_automations", "_presence_state"
    )

    # Tristate, deckt sich 1:1 mit dem Residents-Adapter-Tristate (0/1/2, siehe
    # hannah/residents/Resident.py) — ersetzt die früheren zwei unabhängigen Booleans
    # presence/asleep (hannah#309, Nachfolger von #298), die eine ungültige Kombination
    # wie presence=False + asleep=True zuließen.
    PRESENCE_STATES = ("away", "home", "asleep")

    def after_init(self):
        """Wird vom BaseModel am Ende von __init__ aufgerufen."""
        self._cached_linked_accounts = None
        self._cached_enabled_automations = None
        self._presence_state = "away"

    @property
    def presence_state(self):
        """Gibt den aktuellen Tristate-Anwesenheitsstatus zurück: "away" | "home" | "asleep"."""
        return self._presence_state

    @presence_state.setter
    def presence_state(self, value):
        if value not in self.PRESENCE_STATES:
            raise ValueError(f"invalid presence_state: {value!r}")
        if value == self._presence_state:
            return
        old = self._presence_state
        was_home, is_home = old != "away", value != "away"
        was_asleep, is_asleep = old == "asleep", value == "asleep"
        self._presence_state = value
        if is_home and not was_home:
            self._emit("arrival")
        elif was_home and not is_home:
            self._emit("departure")
        if is_asleep and not was_asleep:
            self._emit("fell_asleep")
        elif was_asleep and not is_asleep:
            self._emit("woke_up")

    @property
    def is_home(self):
        """"Zuhause" umfasst sowohl "home" als auch "asleep" — analog zu
        Resident.is_home() (hannah#286)."""
        return self._presence_state != "away"

    @property
    def is_asleep(self):
        return self._presence_state == "asleep"

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
    def enabled_automations(self):
        """Gibt die Keys aller für diesen User aktivierten Automations zurück (z.B. 'telegram_autoresponder')."""
        if not self._db or not self.id:
            return []

        if self._cached_enabled_automations is None:
            from hannah.models.user_automation import UserAutomation
            rows = UserAutomation.select(self._db).where("user_id = ?", self.id).all()
            self._cached_enabled_automations = [r.automation for r in rows]

        return self._cached_enabled_automations

    def has_automation(self, automation):
        """Prüft, ob eine bestimmte Automation für diesen User aktiviert ist."""
        return automation in self.enabled_automations

    def clear_enabled_automations_cache(self):
        """Leert den internen Cache, damit beim nächsten Zugriff frisch geladen wird."""
        self._cached_enabled_automations = None

    def enable_automation(self, automation):
        """Aktiviert eine Automation für diesen User (idempotent)."""
        from hannah.models.user_automation import UserAutomation
        if not self.has_automation(automation):
            UserAutomation.create(self._db, user_id=self.id, automation=automation)
            self.clear_enabled_automations_cache()

    def disable_automation(self, automation):
        """Deaktiviert eine Automation für diesen User (idempotent)."""
        from hannah.models.user_automation import UserAutomation
        row = UserAutomation.get(self._db, user_id=self.id, automation=automation)
        if row:
            row.delete()
            self.clear_enabled_automations_cache()

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