"""
Langzeitgedächtnis für Hannah — SQLite-basiert.

Nach Ablauf der Konversations-TTL fasst das LLM das Gespräch in einem Satz zusammen.
Beim nächsten Gespräch werden die letzten N Erinnerungen in den System-Prompt injiziert.
"""
import logging
import sqlite3
import threading
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)


class LongTermMemory:
    def __init__(self, db_path: str = "memory.db", recent_limit: int = 10):
        self._db_path     = db_path
        self._recent_limit = recent_limit
        self._lock        = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    roomie_id  TEXT    NOT NULL,
                    summary    TEXT    NOT NULL,
                    created_at TEXT    NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_roomie ON memories (roomie_id, created_at DESC)")

    def add(self, roomie_id: str, summary: str):
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO memories (roomie_id, summary, created_at) VALUES (?, ?, ?)",
                    (roomie_id, summary.strip(), datetime.now().isoformat(timespec="minutes")),
                )
        log.info(f"[memory] Erinnerung gespeichert für {roomie_id!r}: {summary[:80]!r}")

    def get_recent(self, roomie_id: str, limit: Optional[int] = None) -> list[str]:
        n = limit or self._recent_limit
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT summary, created_at FROM memories WHERE roomie_id = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (roomie_id, n),
                ).fetchall()
        return [f"[{r['created_at']}] {r['summary']}" for r in reversed(rows)]

    def format_for_prompt(self, roomie_id: str) -> str:
        entries = self.get_recent(roomie_id)
        if not entries:
            return ""
        lines = "\n".join(f"- {e}" for e in entries)
        return f"\n\nErinnerungen aus früheren Gesprächen mit dieser Person:\n{lines}"
