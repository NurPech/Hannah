from pyorm import BaseModel

class UserAutomation(BaseModel):
    __table__ = "user_automations"
    __primary_key__ = ["user_id", "automation"]
    __slots__ = ("user_id", "automation", "_db")
