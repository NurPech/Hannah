"""
Link-Tokens für die Konto-Verknüpfung per Deep-Link (#334).

Einmal-Codes, die Hannah für einen User und einen Dienst ausstellt (CreateLinkToken)
und die der Adapter des Dienstes über ChannelConnect einlöst. Bewusst nur im Speicher:

- gültig TOKEN_TTL_SECONDS ab Ausstellung
- höchstens ein gültiger Code pro (user_id, service) — ein neuer ersetzt den alten
- nach einem Neustart von Hannah sind alle Codes ungültig

Format: secrets.token_urlsafe(24) → 32 Zeichen aus A-Za-z0-9_- (Telegram erlaubt
für den start-Parameter max. 64 Zeichen aus genau diesem Alphabet).
"""
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

TOKEN_TTL_SECONDS = 600

LOOKUP_OK = "ok"
LOOKUP_UNKNOWN = "unknown"
LOOKUP_EXPIRED = "expired"


@dataclass(frozen=True)
class LinkToken:
    token: str
    user_id: int
    service: str
    expires_at: float  # Unix timestamp (seconds)


class LinkTokenStore:
    def __init__(self, ttl_seconds: int = TOKEN_TTL_SECONDS, clock: Callable[[], float] = time.time):
        self._ttl = ttl_seconds
        self._clock = clock
        self._by_token: dict[str, LinkToken] = {}
        self._by_owner: dict[tuple[int, str], str] = {}  # (user_id, service) → token
        self._lock = threading.Lock()

    def issue(self, user_id: int, service: str) -> LinkToken:
        """Stellt einen neuen Code aus; ein noch gültiger Code für (user_id, service) verfällt."""
        with self._lock:
            self._prune_expired()
            old = self._by_owner.pop((user_id, service), None)
            if old:
                self._by_token.pop(old, None)
            entry = LinkToken(
                token=secrets.token_urlsafe(24),
                user_id=user_id,
                service=service,
                expires_at=self._clock() + self._ttl,
            )
            self._by_token[entry.token] = entry
            self._by_owner[(user_id, service)] = entry.token
            return entry

    def lookup(self, token: str, service: str) -> tuple[str, Optional[LinkToken]]:
        """Prüft einen Code, ohne ihn zu entwerten. Ein Code für einen anderen Dienst
        gilt als unbekannt. Abgelaufene Codes werden dabei entfernt."""
        with self._lock:
            entry = self._by_token.get(token)
            if entry is None or entry.service != service:
                return LOOKUP_UNKNOWN, None
            if entry.expires_at <= self._clock():
                self._remove(entry)
                return LOOKUP_EXPIRED, None
            return LOOKUP_OK, entry

    def consume(self, token: str) -> None:
        """Entwertet einen Code (nach erfolgreicher Verknüpfung)."""
        with self._lock:
            entry = self._by_token.get(token)
            if entry is not None:
                self._remove(entry)

    def _remove(self, entry: LinkToken) -> None:
        self._by_token.pop(entry.token, None)
        if self._by_owner.get((entry.user_id, entry.service)) == entry.token:
            del self._by_owner[(entry.user_id, entry.service)]

    def _prune_expired(self) -> None:
        now = self._clock()
        for entry in [e for e in self._by_token.values() if e.expires_at <= now]:
            self._remove(entry)
