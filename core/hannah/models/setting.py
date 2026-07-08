from pyorm import BaseModel

class Setting(BaseModel):
    __table__ = "settings"
    __primary_key__ = "id"
    __slots__ = ("id", "category", "name", "value")
    __json_fields__ = ("value",)
