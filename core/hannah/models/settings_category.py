from pyorm import BaseModel

class SettingsCategory(BaseModel):
    __table__ = "settings_category"
    __primary_key__ = "id"
    __slots__ = ("id", "name", "parent")
