from pyorm import BaseModel

class Message(BaseModel):
    __table__ = "messages"
    __primary_key__ = "id"
    __slots__ = ("id", "user_id", "content", "source", "sender_user_id", "reply_to_id", "created_at")
