from pyorm import BaseModel

class LinkedAccount(BaseModel):
    __table__ = "linked_accounts"
    __primary_key__ = "id"
    __json_fields__ = ("provider_payload",)
    __slots__ = (
        "id", "user_id", "provider", "external_id", "provider_payload", "_db", "_cached_user"
    )

    def after_init(self):
        """Wird vom BaseModel am Ende von __init__ aufgerufen."""
        self._cached_user = None

    @property
    def user(self):
        if not self._db or not self.user_id:
            return None
        
        if self._cached_user is None:
            from hannah.models.user import User
            self._cached_user = User.get(self._db, id=self.user_id)
            
        return self._cached_user
