from pyorm import BaseModel

class Alarm(BaseModel):
    __table__ = "alarms"
    __primary_key__ = "id"
    __slots__ = ("id", "satellite_id", "time", "weekdays", "skip_dates",
                 "one_shot_date", "enabled", "label", "user_id", "created_at")
    __json_fields__ = ("weekdays", "skip_dates")
