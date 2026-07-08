from pyorm import BaseModel

class Car(BaseModel):
    __table__ = "cars"
    __primary_key__ = "id"
    __slots__ = ("id", "name", "topic_prefix", "home_address", "created_at")

    @property
    def owners(self):
        """Gibt alle Owner (User) dieses Autos zurück, über die user_to_car-Pivot-Tabelle."""
        from hannah.models.user import User
        if not self._db or not self.id:
            return []
        return User.select(self._db).join(
            "user_to_car utc", on="utc.user_id = users.id"
        ).where("utc.car_id = ?", self.id).all()