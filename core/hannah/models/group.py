from pyorm import BaseModel

class Group(BaseModel):
    __table__ = "groups"
    __primary_key__ = "group_id"
    __slots__ = (
        "group_id", "display_name", "created_at"
    )