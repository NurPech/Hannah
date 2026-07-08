from pyorm import BaseModel

class Routine(BaseModel):
    __table__ = "routines"
    __primary_key__ = "id"
    __slots__ = ("id", "name", "triggers", "actions", "reply", "created_at")
    __json_fields__ = ("triggers", "actions")