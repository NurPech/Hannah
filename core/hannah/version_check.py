"""
Versionsvergleich der laufenden Komponenten mit den öffentlichen Versionen (#504).

Core kennt die laufenden Instanzen samt Version aus der Component-Registry
(hannah.component_registry.ComponentTracker). Die letzte öffentliche Version jeder Komponente
liefert der Version-Aggregator (``GET <url>``, flaches JSON ``{"<schlüssel>": "<version>"}``).
Dieses Modul holt diese Antwort und vergleicht beides nach SemVer.

Abgerufen wird auf Anfrage statt per Polling: der erste GetComponentVersions-Call holt die
Antwort, danach kommt sie bis zum Ablauf der TTL aus dem Speicher. Schlägt ein Abruf fehl, bleibt
der letzte gute Stand erhalten (veraltet, aber da), und ein neuer Versuch kommt erst nach einer
Wartezeit, die sich bei jedem Fehlschlag verdoppelt (höchstens bis zur TTL). Der Aufrufer wird nie
durch einen Fehler des Aggregators gestört, und der Abruf lässt sich in den Settings abschalten
(Kategorie ``version_check``: ``enabled``, ``url``, ``cache_ttl_s``).
"""
import logging
import re
import threading
import time
from typing import Callable, NamedTuple, Optional

import requests

log = logging.getLogger(__name__)

DEFAULT_URL = "https://hannah-versions.leonie.network/versions"
DEFAULT_CACHE_TTL_S = 3600.0

# Wie lange ein Abruf höchstens dauern darf, und wie viel Antwort wir akzeptieren (der Aggregator
# liefert ein paar hundert Bytes, alles darüber ist nicht der Aggregator).
FETCH_TIMEOUT_S = 5.0
MAX_RESPONSE_BYTES = 64 * 1024

# Wartezeit nach dem ersten Fehlschlag, verdoppelt sich bis zur TTL.
_RETRY_BASE_S = 60.0
# Eine zu kurze TTL würde den Aggregator bei jedem Aufruf fragen.
_MIN_CACHE_TTL_S = 60.0

# Aggregator-Schlüssel, deren Name von dem x-component-Namen der Registry abweicht. Alle anderen
# Schlüssel gelten als gleichnamig.
AGGREGATOR_TO_COMPONENT = {
    "iobroker-adapter": "iobroker",
}

# Schlüssel, die nicht verglichen werden: Satelliten bekommen updateAvailable/newVersion vom Update
# Server, und satellite-esp ist im Aggregator ein einzelner Wert, der Rev4 und Rev5 nicht trennt.
EXCLUDED_AGGREGATOR_KEYS = frozenset({"satellite-esp"})

UP_TO_DATE = "up_to_date"
UPDATE_AVAILABLE = "update_available"
NEWER_THAN_PUBLIC = "newer_than_public"

_SEMVER = re.compile(
    r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


class PublicVersions(NamedTuple):
    versions: dict          # {x-component-Name: Version}, ohne Satelliten
    available: bool         # False: Abruf aus, oder bisher nie erreichbar
    checked_at: int         # Unix-Sekunden des letzten erfolgreichen Abrufs, 0 = nie


_UNAVAILABLE = PublicVersions({}, False, 0)


def _parse(version: str):
    """(major, minor, patch, prerelease-Teile) oder None, wenn kein gültiges SemVer."""
    match = _SEMVER.match(version.strip()) if isinstance(version, str) else None
    if not match:
        return None
    major, minor, patch, pre = match.groups()
    parts = []
    for part in pre.split(".") if pre else []:
        parts.append((0, int(part), "") if part.isdigit() else (1, 0, part))
    return int(major), int(minor), int(patch), parts


def _compare_prerelease(a: list, b: list) -> int:
    # Eine Version ohne Prerelease ist größer als dieselbe mit (1.0.0-rc.1 < 1.0.0).
    if not a and not b:
        return 0
    if not a:
        return 1
    if not b:
        return -1
    for x, y in zip(a, b):
        if x != y:
            return -1 if x < y else 1
    return (len(a) > len(b)) - (len(a) < len(b))


def compare(running: str, public: str) -> Optional[str]:
    """Vergleicht die laufende mit der öffentlichen Version nach SemVer. None, wenn eine der beiden
    kein gültiges SemVer ist (zum Beispiel leer: eine Komponente ohne Version)."""
    a, b = _parse(running), _parse(public)
    if a is None or b is None:
        return None
    if a[:3] != b[:3]:
        return UPDATE_AVAILABLE if a[:3] < b[:3] else NEWER_THAN_PUBLIC
    order = _compare_prerelease(a[3], b[3])
    if order == 0:
        return UP_TO_DATE
    return UPDATE_AVAILABLE if order < 0 else NEWER_THAN_PUBLIC


def component_versions(instances: list, public: dict) -> list:
    """Hängt an jede laufende Instanz (Einträge von ComponentTracker.snapshot()) die öffentliche
    Version und das Vergleichsergebnis. ``public`` ist PublicVersions.versions.

    ``instance_id`` ist der Name, unter dem die Instanz angezeigt wird (#506): der Host, auf dem sie
    läuft, solange sie ihn mitschickt, sonst ihre zufällige Instanz-ID. Der Schlüssel in der
    Registry bleibt die ID, ein Host ist nicht eindeutig (zwei Instanzen auf einem System)."""
    out = []
    for instance in instances:
        latest = public.get(instance["component"], "")
        out.append({
            "component": instance["component"],
            "instance_id": instance.get("host") or instance["instance_id"],
            "version": instance["version"],
            "latest_version": latest,
            "comparison": compare(instance["version"], latest) if latest else None,
        })
    return out


class VersionChecker:
    """Holt die öffentlichen Versionen auf Anfrage und hält sie im Speicher."""

    def __init__(self, get_settings: Callable[[], dict],
                 http_get: Callable[..., "requests.Response"] = requests.get,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time):
        self._get_settings = get_settings
        self._http_get = http_get
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._fetching = threading.Lock()
        self._cache: Optional[PublicVersions] = None
        self._fetched_at = 0.0
        self._next_attempt = 0.0
        self._failures = 0

    def public_versions(self) -> PublicVersions:
        """Die letzte bekannte öffentliche Version jeder Komponente. Wirft nie."""
        try:
            settings = self._get_settings() or {}
        except Exception:
            log.exception("[version_check] Settings nicht lesbar")
            settings = {}
        if not _as_bool(settings.get("enabled"), True):
            return _UNAVAILABLE
        url = settings.get("url") or DEFAULT_URL
        ttl = max(_as_float(settings.get("cache_ttl_s"), DEFAULT_CACHE_TTL_S), _MIN_CACHE_TTL_S)

        now = self._clock()
        with self._lock:
            cached, fresh = self._cache, self._cache is not None and now - self._fetched_at < ttl
            due = now >= self._next_attempt
        if fresh or not due:
            return cached or _UNAVAILABLE
        # Höchstens ein Abruf gleichzeitig: wer ihn nicht bekommt, nimmt den Stand, den es gibt.
        if not self._fetching.acquire(blocking=False):
            return cached or _UNAVAILABLE
        try:
            return self._fetch(str(url), ttl) or cached or _UNAVAILABLE
        finally:
            self._fetching.release()

    def _fetch(self, url: str, ttl: float) -> Optional[PublicVersions]:
        try:
            response = self._http_get(url, timeout=FETCH_TIMEOUT_S)
            response.raise_for_status()
            if len(response.content) > MAX_RESPONSE_BYTES:
                raise ValueError("Antwort zu groß")
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Antwort ist kein JSON-Objekt")
        except Exception as e:
            self._failed(ttl, e)
            return None

        versions = {}
        for key, version in payload.items():
            if key in EXCLUDED_AGGREGATOR_KEYS or not isinstance(version, str):
                continue
            versions[AGGREGATOR_TO_COMPONENT.get(key, key)] = version
        result = PublicVersions(versions, True, int(self._wall_clock()))
        with self._lock:
            self._cache = result
            self._fetched_at = self._clock()
            self._failures = 0
            self._next_attempt = 0.0
        return result

    def _failed(self, ttl: float, error: Exception) -> None:
        with self._lock:
            self._failures += 1
            wait = min(_RETRY_BASE_S * 2 ** (self._failures - 1), ttl)
            self._next_attempt = self._clock() + wait
            stale = "letzter Stand bleibt" if self._cache else "noch kein Stand"
        log.warning(f"[version_check] Abruf der öffentlichen Versionen fehlgeschlagen ({error}), "
                    f"{stale}, nächster Versuch in {wait:.0f} s")


def _as_bool(value, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _as_float(value, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)
