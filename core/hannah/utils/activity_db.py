import logging

from pyorm import Column, Database, Table, create_table

log = logging.getLogger(__name__)

# Separate DB von hannah.db — bewusst nicht von utils/db.py::get_db() abgeleitet.
# Grund: erwartete Schreiblast fürs Activity-Log soll nicht auf der SD-Karte des Pi
# landen, auf der hannah.db liegt (#220).
ACTIVITY_TABLE = Table(
    name="activity_log",
    columns=[
        Column("id", "integer", primary_key=True, autoincrement=True),
        Column("ts", "datetime", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("channel_type", "string", length=32, nullable=False),
        Column("channel_id", "string", length=128, nullable=True),
        Column("user_id", "integer", nullable=True),
        Column("raw_text", "text", nullable=True),
        Column("intent_name", "string", length=64, nullable=True),
        Column("intent_meta", "text", nullable=True),
        Column("answer_text", "text", nullable=True),
        Column("audio_path", "string", length=512, nullable=True),
    ],
    indexes=[("user_id",), ("channel_type", "channel_id")],
)

_cfg: dict = {}


def init_activity_db(cfg: dict) -> None:
    """cfg = config.yaml['activity_log']. Kein 'host' gesetzt → Feature deaktiviert,
    log_activity() (activity_log.py) wird zum No-op — Monitoring darf die
    Sprachpipeline nie blockieren."""
    global _cfg
    _cfg = cfg or {}
    if not _cfg.get("host"):
        log.info("[activity_db] Kein Host konfiguriert — Activity-Log deaktiviert.")
        return
    db = get_activity_db()
    try:
        create_table(db, ACTIVITY_TABLE)
    finally:
        db.close()


def get_activity_db():
    """Frische Connection pro Aufruf, analog utils/db.py::get_db(). None wenn nicht konfiguriert."""
    if not _cfg.get("host"):
        return None
    return Database.mysql(
        host=_cfg["host"],
        port=_cfg.get("port", 3306),
        user=_cfg["user"],
        password=_cfg["password"],
        database=_cfg["database"],
    )
