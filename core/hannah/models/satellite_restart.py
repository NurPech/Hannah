from pyorm import BaseModel

class SatelliteRestart(BaseModel):
    """Historie gemeldeter Satelliten-Neustarts (#165) — im Gegensatz zu
    Satellite.last_restart_at (wird überschrieben) bleibt hier jede einzelne
    Meldung erhalten, damit sich Neustart-Häufigkeit über Zeit auswerten lässt."""
    __table__ = "satellite_restarts"
    __primary_key__ = "id"
    __slots__ = ("id", "device_id", "reported_at", "restart_reason", "restart_count")
