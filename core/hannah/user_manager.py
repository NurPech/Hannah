import re
from typing import Callable, Optional
from hannah.models.user import User
from werkzeug.security import check_password_hash, generate_password_hash

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DUMMY_HASH = "scrypt:32768:8:1$3vcmJuZVX7ZD6OJj$71778a0231aec9c7a99d239f115f3a6a25bef7118dbba606c8f62883d252f6d71bd974519f90785827c1e45704ed7b867f31a374f26efe895241de19d448091a"

class UserManager:
    """Verwaltet die Benutzerkonten und deren Authentifizierung."""

    def __init__(self, db : Callable):
        self._db = db
        self._users: dict[int, User] = {}
        # Spät gebunden (residents existiert noch nicht, wenn UserManager entsteht) —
        # fn(roomie_id, is_home, resident_type) pusht Hannah-seitige Presence-Änderungen
        # Richtung ioBroker, für Users mit verlinktem Roomie.
        self._residents_pusher: Optional[Callable[[str, bool, str], None]] = None
        # fn(roomie_id, mood, resident_type) pusht Hannah-seitige Mood-Änderungen Richtung ioBroker.
        self._mood_pusher: Optional[Callable[[str, int, str], None]] = None
        # User-IDs, die bereits einen Listener für presence/mood haben — getrennt, weil
        # set_residents_pusher() und set_mood_pusher() zu unterschiedlichen Zeiten spät
        # gebunden werden können und sich sonst gegenseitig am Nachverdrahten hindern.
        self._wired_residents: set[int] = set()
        self._wired_mood: set[int] = set()
        self._loadAll()

    def set_residents_pusher(self, fn: Callable[[str, bool, str], None]):
        """Wird spät gebunden (residents existiert noch nicht beim Erzeugen von UserManager) —
        verdrahtet rückwirkend alle bereits gecachten User, deren Wiring deshalb beim
        ersten Caching noch leerlief (z.B. der komplette _loadAll()-Bestand beim Start)."""
        self._residents_pusher = fn
        for user in list(self._users.values()):
            self._wire_residents_bridge(user)

    def set_mood_pusher(self, fn: Callable[[str, int, str], None]):
        """Wird spät gebunden, siehe set_residents_pusher()."""
        self._mood_pusher = fn
        for user in list(self._users.values()):
            self._wire_residents_bridge(user)

    def _cache(self, user: User) -> User:
        """Zentrale Cache-Eintrittsstelle — verdrahtet die Residents-Brücke einmal pro User."""
        if user.id not in self._users:
            self._users[user.id] = user
            self._wire_residents_bridge(user)
        return self._users[user.id]

    def refresh_residents_bridge(self, user: User):
        """Erneut versuchen zu verdrahten — nötig, wenn der Residents-Link erst NACH dem
        ersten Caching dieses Users angelegt wurde (z.B. Hannahs eigener Self-Link beim
        allerersten Start: zu dem Zeitpunkt existiert der Link noch nicht)."""
        self._wire_residents_bridge(user)

    def _resident_link(self, user: User) -> Optional[tuple[str, str]]:
        """Liefert (roomie_id, resident_type) für einen User mit verlinktem Residents-Account,
        oder None falls nicht verlinkt bzw. ohne roomie_id im provider_payload."""
        la = user.get_linked_account("residents")
        if not la:
            return None
        payload = la.provider_payload
        # Defensive: a malformed/legacy provider_payload (e.g. double-JSON-encoded by the
        # LinkAccount RPC bug fixed alongside this) decodes to a plain string instead of a
        # dict — treat as "no usable roomie_id" instead of crashing the whole boot on it.
        if not isinstance(payload, dict):
            payload = {}
        roomie_id = payload.get("roomie_id")
        if not roomie_id:
            return None
        return roomie_id, payload.get("resident_type", "roomie")

    def _wire_residents_bridge(self, user: User):
        """Bei verlinktem Roomie: user.presence/.mood-Änderungen Richtung ioBroker pushen.
        Presence und Mood werden unabhängig verdrahtet (separate Pusher, separates Tracking),
        falls einer der beiden noch nicht gebunden ist, wenn der andere ankommt."""
        link = self._resident_link(user)
        if not link:
            return
        roomie_id, resident_type = link

        if self._residents_pusher and user.id not in self._wired_residents:
            user.on("arrival", lambda _u: self._residents_pusher(roomie_id, True, resident_type))
            user.on("departure", lambda _u: self._residents_pusher(roomie_id, False, resident_type))
            self._wired_residents.add(user.id)

        if self._mood_pusher and user.id not in self._wired_mood:
            user.on("mood_change", lambda _u, mood: self._mood_pusher(roomie_id, mood, resident_type))
            self._wired_mood.add(user.id)

    def dump_present_users(self):
        """Pusht 'anwesend' für jeden User, den Hannah aktuell als zuhause kennt — gedacht für
        den AgentConnect-Fall (#83): BLE-Sichtungen können eintreffen, bevor der Adapter
        überhaupt verbunden ist, das zugehörige arrival-Event verhallt dann ungehört, weil
        niemand zuhört. Sendet bewusst nur "anwesend", nie "weg" — ioBroker kann eine eigene,
        unabhängige Presence-Quelle haben (z.B. WLAN-Controller-Tracking), die nicht von einem
        veralteten oder unvollständigen Hannah-Stand überschrieben werden soll."""
        if not self._residents_pusher:
            return
        for user in self.users():
            if not user.presence:
                continue
            link = self._resident_link(user)
            if not link:
                continue
            roomie_id, resident_type = link
            self._residents_pusher(roomie_id, True, resident_type)

    def get_roomie_ids(self) -> set[str]:
        """Roomie-IDs aller User mit verlinktem Residents-Account vom Typ 'roomie' —
        maßgebliche Quelle für "echte Bewohner" (vs. Gäste/Pets), ersetzt die frühere
        statische user_roomie-Config (siehe #87)."""
        ids = set()
        for user in self.users():
            link = self._resident_link(user)
            if link and link[1] == "roomie":
                ids.add(link[0])
        return ids

    def _loadAll(self):
        users = User.select(self._db()).order_by("id").all()
        for user in users:
            self._cache(user)

    def users(self, include_inactive: bool = False) -> dict[int, User]:
        """Gibt alle aktuell im RAM verwalteten Benutzer zurück."""
        all_users = sorted(self._users.values(), key=lambda u: u.id)
    
        if not include_inactive:
            return [u for u in all_users if u.is_active]
            
        return all_users

    def create_user(self, username, password_hash, email=None, display_name=None, type="roomie") -> User:
        """Erstellt einen neuen Benutzer."""
        if not email or not _EMAIL_RE.match(email):
            raise ValueError(f"Ungültige E-Mail-Adresse: {email!r}")
        user = User.create(
            self._db(), username=username, password_hash=password_hash, email=email,
            display_name=display_name or username, type=type,
        )
        return self._cache(user)
    
    def login_user(self, username, password) -> User:
        """Loggt einen User ein und gibt das Objekt oder None zurück"""
        user = self.get_user_by_username(username=username)
        
        # Wenn der User existiert, nimm seinen Hash.
        # Wenn nicht, nimm den Dummy-Hash, damit die Prüfung gleich lange dauert.
        hash_to_check = user.password_hash if user else DUMMY_HASH
        
        # Passwort prüfen
        is_valid = check_password_hash(hash_to_check, password)
        
        # Nur einloggen, wenn der User wirklich existiert UND das Passwort stimmt
        if user and is_valid:
            return user
            
        return None
        

    def get_user_by_id(self, user_id) -> User:
        """Gibt den Benutzer mit der angegebenen ID zurück, oder None.

        Normalisiert auf int, damit ein versehentlich als String übergebenes
        user_id nicht den (int-keyed) Cache mit einem KeyError verfehlt. Nicht-numerische
        IDs (z.B. Voice-IDs "unknown"-Sentinel bei unsicherer Sprechererkennung) geben
        ebenfalls None zurück, statt mit ValueError zu crashen.
        """
        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            return None
        if user_id not in self._users:
            user = User.get(self._db(), id=user_id)
            if not user:
                return None
            self._cache(user)

        return self._users[user_id]

    def get_user_by_username(self, username) -> User:
        """Gibt den Benutzer mit dem angegebenen Benutzernamen zurück."""
        for user in self._users.values():
            if user.username == username:
                return user

        user = User.get(self._db(), username=username)
        if user:
            return self._cache(user)
        return None

    def delete_user(self, user_id) -> bool:
        """Löscht den Benutzer dauerhaft (inkl. Linked Accounts, per ON DELETE CASCADE)."""
        user = self.get_user_by_id(user_id)
        if not user:
            return False
        user.delete()
        self._users.pop(user.id, None)
        self._wired_residents.discard(user.id)
        self._wired_mood.discard(user.id)
        return True

    def get_users_with_automation_enabled(self, automation: str) -> list[User]:
        """Alle aktiven User, für die eine bestimmte Automation gerade aktiviert ist —
        Grundlage für den Snapshot, den ein Automation-Service beim Registrieren
        über AutomationConnect bekommt."""
        return [u for u in self.users() if u.has_automation(automation)]

    def get_user_by_linked_account(self, provider, external_id) -> User:
        """Gibt den Benutzer mit der angegebenen external ID zurück."""
        user = User.select(self._db()).join(
            "linked_accounts", on="linked_accounts.user_id = users.id"
        ).where(
            "linked_accounts.provider = ? AND linked_accounts.external_id = ?",
            provider, external_id
        ).first()
        if user:
            return self._cache(user)
        return None