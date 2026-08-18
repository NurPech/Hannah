from pyorm import BaseModel

class Message(BaseModel):
    __table__ = "messages"
    __primary_key__ = "id"
    __slots__ = ("id", "user_id", "content", "source", "created_at")
