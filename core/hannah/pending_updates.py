"""
Meldet Admins per Mailbox, wenn ein Komponenten-Update länger aussteht (#507).

Die Daten liefert der Versionsabgleich (#504): ``hannah.version_check.component_versions`` kennt zu
jeder laufenden Instanz, ob eine neuere öffentliche Version existiert. Dieses Modul ergänzt, was
dem Abgleich fehlt — *seit wann* das Update aussteht — und macht daraus eine passive Nachricht
(Mailbox, #234: gedimmtes gelbes Idle-LED, wird nicht von selbst vorgelesen).

Ablauf von ``check()`` (periodisch aufgerufen, siehe main.py):
- Pro Komponente, nicht pro Instanz: steht für irgendeine Instanz ein Update aus, gilt die
  Komponente als veraltet. Der Schlüssel ist (Komponente, neueste öffentliche Version).
- Erstes Auftreten eines Schlüssels wird in ``pending_update_seen`` festgehalten. Erscheint eine
  noch neuere öffentliche Version, beginnt für sie ein neuer Zeitraum.
- Ist der Schlüssel länger als die Schwelle (``notify_after_h``, Standard 24) offen, bekommt jeder
  Admin (Trust-Level 10, ohne Hannahs eigenen User) genau eine Nachricht. Wer schon benachrichtigt
  wurde, steht in ``pending_update_notified`` — ein Core-Neustart wiederholt nichts, und ein erst
  später zum Admin gewordener User bekommt die Nachricht nachträglich.
- Der Zeitraum wird nur durch eine eindeutige Entwarnung zurückgesetzt (Komponente läuft und ist
  auf dem öffentlichen Stand). Ist sie gerade nicht verbunden oder der Aggregator nicht erreichbar,
  bleibt der Stand, sonst würde jeder Neustart den Zeitraum von vorn beginnen lassen.

Abschalten: ``version_check.enabled`` (kein Abruf) oder ``version_check.notify_admins`` (keine
Nachrichten). Bei ausgeschalteten Nachrichten wird der Aggregator nicht angefragt.
"""
import logging
import threading
import time
from typing import Callable

from hannah import version_check

log = logging.getLogger(__name__)

DEFAULT_NOTIFY_AFTER_H = 24.0

# Benutzername von Hannahs eigenem User (main.py legt ihn unter diesem Namen als Roomie an).
HANNAH_USERNAME = "hannah"

_SEEN = "pending_update_seen"
_NOTIFIED = "pending_update_notified"


class PendingUpdateNotifier:
    def __init__(self, db: Callable, user_manager, message_manager,
                 get_instances: Callable[[], list],
                 get_public_versions: Callable[[], "version_check.PublicVersions"],
                 get_settings: Callable[[], dict],
                 clock: Callable[[], float] = time.time,
                 own_username: str = HANNAH_USERNAME):
        """
        db: liefert eine pyorm.Database, z.B. hannah.utils.db.get_db.
        user_manager: für get_users_with_trust_level() (Admin-Empfänger).
        message_manager: für create_message() (Mailbox, #234).
        get_instances: laufende Komponenten-Instanzen, z.B. ComponentTracker.snapshot.
        get_public_versions: VersionChecker.public_versions.
        get_settings: Settings-Kategorie ``version_check``.
        """
        self._db = db
        self._user_manager = user_manager
        self._message_manager = message_manager
        self._get_instances = get_instances
        self._get_public_versions = get_public_versions
        self._get_settings = get_settings
        self._clock = clock
        self._own_username = own_username
        self._lock = threading.Lock()

    def check(self) -> int:
        """Ein Durchlauf. Gibt die Zahl der angelegten Nachrichten zurück. Wirft nie nach außen
        wegen fehlender Daten (Aggregator weg, Setting unlesbar), nur bei echten Programmfehlern."""
        settings = self._settings()
        if not _as_bool(settings.get("enabled"), True) or not _as_bool(settings.get("notify_admins"), True):
            return 0
        public = self._get_public_versions()
        if not public.available:
            return 0

        pending, cleared = self._classify(version_check.component_versions(self._get_instances(), public.versions))
        threshold_s = max(_as_float(settings.get("notify_after_h"), DEFAULT_NOTIFY_AFTER_H), 0.0) * 3600.0

        with self._lock:
            db = self._db()
            for component in cleared:
                self._forget(db, component, keep_version=None)
            now = self._clock()
            sent = 0
            for component, (latest, running) in pending.items():
                self._forget(db, component, keep_version=latest)
                db.execute(
                    f'INSERT OR IGNORE INTO "{_SEEN}" ("component", "version", "first_seen") VALUES (?, ?, ?)',
                    (component, latest, int(now)),
                )
                first_seen = db.execute(
                    f'SELECT "first_seen" FROM "{_SEEN}" WHERE "component" = ? AND "version" = ?',
                    (component, latest),
                ).fetchone()[0]
                # Schreibsperre freigeben, bevor die Mailbox über eine eigene Connection schreibt
                db.commit()
                if now - first_seen >= threshold_s:
                    sent += self._notify(db, component, latest, running, threshold_s)
            db.commit()
        return sent

    def _settings(self) -> dict:
        try:
            return self._get_settings() or {}
        except Exception:
            log.exception("[pending_updates] Settings nicht lesbar")
            return {}

    @staticmethod
    def _classify(instances: list) -> tuple[dict, set]:
        """(veraltet, entwarnt). veraltet: {Komponente: (neueste Version, [laufende Versionen])},
        entwarnt: Komponenten, die laufen und auf dem öffentlichen Stand (oder darüber) sind."""
        pending: dict[str, tuple[str, set]] = {}
        current: set[str] = set()
        for inst in instances:
            comparison = inst["comparison"]
            if comparison == version_check.UPDATE_AVAILABLE:
                _, running = pending.setdefault(inst["component"], (inst["latest_version"], set()))
                running.add(inst["version"])
            elif comparison in (version_check.UP_TO_DATE, version_check.NEWER_THAN_PUBLIC):
                current.add(inst["component"])
        return (
            {c: (latest, sorted(running)) for c, (latest, running) in pending.items()},
            current - pending.keys(),
        )

    @staticmethod
    def _forget(db, component: str, keep_version) -> None:
        """Vergisst den Stand einer Komponente — ganz (keep_version=None) oder bis auf eine Version."""
        for table in (_SEEN, _NOTIFIED):
            if keep_version is None:
                db.execute(f'DELETE FROM "{table}" WHERE "component" = ?', (component,))
            else:
                db.execute(f'DELETE FROM "{table}" WHERE "component" = ? AND "version" != ?', (component, keep_version))

    def _notify(self, db, component: str, latest: str, running: list, threshold_s: float) -> int:
        already = {
            r[0] for r in db.execute(
                f'SELECT "user_id" FROM "{_NOTIFIED}" WHERE "component" = ? AND "version" = ?',
                (component, latest),
            ).fetchall()
        }
        admins = [
            u for u in self._user_manager.get_users_with_trust_level(10)
            if u.username != self._own_username and u.id not in already
        ]
        if not admins:
            return 0
        hours = threshold_s / 3600.0
        content = (
            f"Für die Komponente „{component}“ steht seit mehr als {hours:g} Stunden ein Update aus. "
            f"Sie läuft in Version {' und '.join(running)}, aktuell ist Version {latest}."
        )
        for admin in admins:
            self._message_manager.create_message(user_id=admin.id, content=content, source="system")
            db.execute(
                f'INSERT OR IGNORE INTO "{_NOTIFIED}" ("component", "version", "user_id") VALUES (?, ?, ?)',
                (component, latest, admin.id),
            )
            db.commit()
        log.info(f"[pending_updates] Update für {component} {latest} steht aus — {len(admins)} Admin(s) benachrichtigt")
        return len(admins)


def _as_bool(value, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _as_float(value, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)
