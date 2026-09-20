import logging
import threading
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)


class BleTag:
    __slots__ = ("mac", "label", "user_id")

    def __init__(self, mac: str, label: str, user_id: Optional[int] = None):
        self.mac = mac.lower()
        self.label = label
        self.user_id = user_id


class _Report:
    __slots__ = ("rssi", "ts")

    def __init__(self, rssi: int, ts: float):
        self.rssi = rssi
        self.ts = ts


class BleLocationEngine:
    """
    Aggregiert BLE-RSSI-Reports von mehreren ESP32-Satelliten und bestimmt
    per „stärkster RSSI gewinnt" den aktuellen Aufenthaltsort pro BLE-Tag.

    Tags kommen aus der BleTag-DB-Tabelle (mac_address, label, optionale user_id;
    siehe hannah.ble_tags.BleTagManager, #115). Für jeden Tag werden RSSI-Messungen
    pro Satellit gehalten. Sobald alle Einträge eines Tags älter als stale_timeout
    sind, gilt er als nicht sichtbar. Bei jeder Lageänderung wird der
    on_location_change-Callback aufgerufen.
    """

    def __init__(self, cfg: dict, get_satellite_room: Callable[[str], Optional[str]]):
        """
        cfg                 : ble-Abschnitt aus config.yaml, "tags" via BleTagManager.get_tag_records() befüllt
        get_satellite_room  : fn(device) → room-Name oder None
        """
        self._stale = float(cfg.get("stale_timeout", 30))
        self._get_room = get_satellite_room
        self._lock = threading.Lock()

        self._tags: dict[str, BleTag] = {}
        for t in cfg.get("tags", []):
            mac = (t.get("mac") or t.get("mac_address") or "").lower()
            label = t.get("label", mac)
            if mac:
                self._tags[mac] = BleTag(mac, label, t.get("user_id"))

        # {mac → {satellite → _Report}}
        self._reports: dict[str, dict[str, _Report]] = {}
        # letzter bekannter best-Satellit pro MAC
        self._last_sat: dict[str, Optional[str]] = {}

        self._on_change: Optional[Callable[["BleTag", Optional[str], Optional[str], int], None]] = None

        if self._tags:
            self._start_stale_timer()

    # ── Public API ────────────────────────────────────────────────────────────

    def get_all_macs(self) -> list[str]:
        return list(self._tags.keys())

    def get_current_locations(self) -> list[tuple["BleTag", Optional[str], Optional[str], int]]:
        """Gibt den zuletzt bekannten Standort aller Tags zurück: (tag, room, satellite, rssi)."""
        now = time.monotonic()
        result = []
        with self._lock:
            for mac, tag in self._tags.items():
                best_sat = self._last_sat.get(mac)
                fresh = {
                    s: r for s, r in self._reports.get(mac, {}).items()
                    if now - r.ts < self._stale
                }
                if best_sat and best_sat not in fresh:
                    best_sat = None
                rssi = fresh[best_sat].rssi if best_sat else 0
                room = self._get_room(best_sat) if best_sat else None
                result.append((tag, room, best_sat, rssi))
        return result

    def set_location_change_handler(
        self, fn: Callable[["BleTag", Optional[str], Optional[str], int], None]
    ):
        """fn(tag, room_or_None, satellite_or_None, rssi)"""
        self._on_change = fn

    def on_report(self, satellite: str, mac: str, rssi: int):
        """Wird für jedes eingehende BLE-RSSI-Report aufgerufen."""
        mac = mac.lower()
        if mac not in self._tags:
            return
        with self._lock:
            self._reports.setdefault(mac, {})[satellite] = _Report(rssi, time.monotonic())
            self._evaluate(mac)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _start_stale_timer(self):
        interval = max(5.0, self._stale / 3)
        t = threading.Timer(interval, self._tick)
        t.daemon = True
        t.start()

    def _tick(self):
        with self._lock:
            for mac in list(self._tags):
                self._evaluate(mac)
        self._start_stale_timer()

    def _evaluate(self, mac: str):
        now = time.monotonic()
        fresh = {
            s: r
            for s, r in self._reports.get(mac, {}).items()
            if now - r.ts < self._stale
        }
        best_sat = max(fresh, key=lambda s: fresh[s].rssi) if fresh else None
        best_rssi = fresh[best_sat].rssi if best_sat else 0
        prev = self._last_sat.get(mac)
        if best_sat == prev:
            return
        self._last_sat[mac] = best_sat
        room = self._get_room(best_sat) if best_sat else None
        tag = self._tags[mac]
        log.debug(f"BLE: {tag.label} ({mac}) → {room!r} via {best_sat!r} (RSSI {best_rssi})")
        if self._on_change:
            try:
                self._on_change(tag, room, best_sat, best_rssi)
            except Exception as e:
                log.error(f"BLE location-change-callback fehlgeschlagen: {e}")
