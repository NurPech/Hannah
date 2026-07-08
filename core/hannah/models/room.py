from pyorm import BaseModel

class Room(BaseModel):
    __table__ = "rooms"
    __primary_key__ = "room_id"
    __slots__ = (
        "room_id", "display_name", "created_at"
    )