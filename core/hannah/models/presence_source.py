from pyorm import BaseModel

class PresenceSource(BaseModel):
    __table__ = "presence_sources"
    __primary_key__ = "id"
    __slots__ = ("id", "user_id", "source_type", "reference", "home_confidence", "away_confidence", "enabled", "created_at")
