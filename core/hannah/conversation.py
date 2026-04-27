"""
Gesprächskontext für Hannah — pro Quelle (Gerät, Roomie, Kanal).

Zwei Funktionen:
1. Intent-Kontext: Raum/Gerät/Kategorie aus dem letzten Befehl für
   Folgeanfragen ergänzen ("Und die Küche auch?").
2. LLM-History: Nachrichtenhistorie für Smalltalk-Folgefragen.

Kontext verfällt nach `ttl` Sekunden Inaktivität.
"""
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

from .nlu import Intent

log = logging.getLogger(__name__)


@dataclass
class _SourceCtx:
    room: Optional[str] = None
    room_id: Optional[str] = None
    device: Optional[str] = None
    device_id: Optional[str] = None
    category_filter: Optional[str] = None
    intent_name: Optional[str] = None   # letzter Geräte-Intent (TurnOn/Off/SetLevel/SetColor)
    value: Optional[object] = None
    unit: Optional[str] = None
    history: deque = field(default_factory=deque)
    ts: float = field(default_factory=time.time)
    pending_clarification: Optional[dict] = None  # {"intent": Intent, "candidates": list}
    smalltalk_active: bool = False


class ConversationContext:
    def __init__(
        self,
        ttl: float = 120.0,
        max_history_turns: int = 3,
        on_conversation_end: Optional[Callable[[str, list], None]] = None,
    ):
        self._ttl = ttl
        self._max_messages = max_history_turns * 2
        self._on_conversation_end = on_conversation_end
        self._lock = threading.Lock()
        self._ctxs: dict[str, _SourceCtx] = {}

        if on_conversation_end:
            t = threading.Thread(target=self._expiry_loop, daemon=True)
            t.start()

    def _expiry_loop(self):
        while True:
            time.sleep(30)
            expired: list[tuple[str, list]] = []
            with self._lock:
                for source, ctx in list(self._ctxs.items()):
                    if not self._valid(ctx) and ctx.history:
                        expired.append((source, list(ctx.history)))
                        ctx.history.clear()
            for source, history in expired:
                try:
                    self._on_conversation_end(source, history)
                except Exception as e:
                    log.warning(f"[memory] on_conversation_end fehlgeschlagen für {source!r}: {e}")

    def _valid(self, ctx: _SourceCtx) -> bool:
        return time.time() - ctx.ts <= self._ttl

    def _ensure(self, source: str) -> _SourceCtx:
        ctx = self._ctxs.get(source)
        if ctx is None:
            ctx = _SourceCtx(history=deque(maxlen=self._max_messages))
            self._ctxs[source] = ctx
        return ctx

    # ------------------------------------------------------------------

    def fill_intent(self, source: str, intent: Intent) -> None:
        """Ergänzt fehlende Raum/Gerät/Kategorie aus dem letzten Befehl."""
        with self._lock:
            ctx = self._ctxs.get(source)
            if not ctx or not self._valid(ctx):
                return
            user_specified_room = intent.room_id is not None
            if intent.room_id is None and ctx.room_id:
                intent.room = ctx.room
                intent.room_id = ctx.room_id
                log.debug(f"[{source}] Kontext: Raum '{ctx.room}' ergänzt")
            if intent.device is None and ctx.device_id:
                # Kein Gerät erben wenn der User explizit einen Raum genannt hat —
                # er meint dann den ganzen Raum, nicht ein spezifisches Gerät aus dem Kontext.
                if not user_specified_room:
                    intent.device = ctx.device
                    intent.device_id = ctx.device_id
                    log.debug(f"[{source}] Kontext: Gerät '{ctx.device}' ergänzt")
            if intent.category_filter is None and ctx.category_filter:
                intent.category_filter = ctx.category_filter
                log.debug(f"[{source}] Kontext: Kategorie '{ctx.category_filter}' ergänzt")

    def inherit_action(self, source: str, intent: Intent) -> bool:
        """
        Erbt die letzte Aktion wenn intent 'Unknown' ist aber ein Ziel vorliegt.
        Beispiel: "Und die Küche auch?" nach "Wohnzimmer Licht aus" → TurnOff Küche.
        Gibt True zurück wenn geerbt wurde.
        """
        with self._lock:
            ctx = self._ctxs.get(source)
            if not ctx or not self._valid(ctx) or not ctx.intent_name:
                return False
            if intent.name != "Unknown":
                return False
            has_target = bool(intent.room_id or intent.device_id or intent.category_filter)
            if not has_target:
                return False
            intent.name = ctx.intent_name
            if intent.value is None and ctx.value is not None:
                intent.value = ctx.value
                intent.unit = ctx.unit
            log.debug(f"[{source}] Kontext: Aktion '{ctx.intent_name}' geerbt")
            return True

    def update_from_intent(self, source: str, intent: Intent) -> None:
        """Speichert Kontext nach einem Geräte-Intent."""
        with self._lock:
            ctx = self._ensure(source)
            if intent.room_id:
                ctx.room = intent.room
                ctx.room_id = intent.room_id
            if intent.device_id:
                ctx.device = intent.device
                ctx.device_id = intent.device_id
            if intent.category_filter:
                ctx.category_filter = intent.category_filter
            if intent.name in ("TurnOn", "TurnOff", "SetLevel", "SetColor"):
                ctx.intent_name = intent.name
                ctx.value = intent.value
                ctx.unit = intent.unit
            ctx.ts = time.time()

    def set_clarification(self, source: str, intent: "Intent", candidates: list) -> None:
        with self._lock:
            ctx = self._ensure(source)
            ctx.pending_clarification = {"intent": intent, "candidates": candidates}
            ctx.ts = time.time()

    def has_clarification(self, source: str) -> bool:
        with self._lock:
            ctx = self._ctxs.get(source)
            return bool(ctx and self._valid(ctx) and ctx.pending_clarification)

    def get_clarification(self, source: str) -> Optional[dict]:
        with self._lock:
            ctx = self._ctxs.get(source)
            return ctx.pending_clarification if ctx and self._valid(ctx) else None

    def clear_clarification(self, source: str) -> None:
        with self._lock:
            ctx = self._ctxs.get(source)
            if ctx:
                ctx.pending_clarification = None

    def set_smalltalk_active(self, source: str, active: bool) -> None:
        with self._lock:
            ctx = self._ensure(source)
            ctx.smalltalk_active = active
            ctx.ts = time.time()

    def is_smalltalk_active(self, source: str) -> bool:
        with self._lock:
            ctx = self._ctxs.get(source)
            return bool(ctx and self._valid(ctx) and ctx.smalltalk_active)

    def add_llm_exchange(self, source: str, user_msg: str, assistant_msg: str) -> None:
        """Speichert user+assistant Nachrichten für LLM-Folgefragen."""
        with self._lock:
            ctx = self._ensure(source)
            if not isinstance(ctx.history, deque) or ctx.history.maxlen != self._max_messages:
                ctx.history = deque(ctx.history, maxlen=self._max_messages)
            ctx.history.append({"role": "user", "content": user_msg})
            ctx.history.append({"role": "assistant", "content": assistant_msg})
            ctx.ts = time.time()

    def get_llm_history(self, source: str) -> list[dict]:
        """Gibt die LLM-Nachrichtenhistorie zurück (leer wenn abgelaufen)."""
        with self._lock:
            ctx = self._ctxs.get(source)
            if not ctx or not self._valid(ctx):
                return []
            return list(ctx.history)
