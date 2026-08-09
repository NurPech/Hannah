from pyorm import BaseModel


class ActivityLog(BaseModel):
    __table__ = "activity_log"
    __primary_key__ = "id"
    __json_fields__ = ("intent_meta",)
    __slots__ = (
        "id", "ts", "channel_type", "channel_id", "user_id", "raw_text",
        "intent_name", "intent_meta", "answer_text", "audio_path", "_db",
    )
