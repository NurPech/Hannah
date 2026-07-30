"""
Hannah Satellite Manager

Verwaltet, über hannah.models, Persistenz für Satelliten: Provisioning/Pairing,
Raum-Zuweisung, Personen-Zuordnung (Owner) und Seed-Cleanup.
"""
import datetime
import hashlib
import logging
import sqlite3
import threading
import time
from typing import Callable, Optional

from hannah.models.room import Room
from hannah.models.user import User
from hannah.models.satellite import Satellite

log = logging.getLogger(__name__)


class SatellitePermissionError(Exception):
    """Requestor fehlt die nötige Trust-Level-/Eigentümer-Berechtigung für diese Satelliten-Aktion."""


def _now_sql() -> str:
    """UTC-Zeitstempel im selben Format wie SQLite's datetime('now')."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class SatelliteManager:
    _CLEANUP_INTERVAL_S = 3600  # Prüfintervall für veraltete unpaired Seeds

    def __init__(
        self,
        db: Callable,
        cfg: dict,
        user_manager=None,
        is_device_free: Optional[Callable[[str], bool]] = None,
        trigger_restart: Optional[Callable[[str], None]] = None,
    ):
        self._db = db
        self._user_manager = user_manager
        self._seed_ttl_days = int(cfg.get("seed_ttl_days", 7))
        self._restart_interval_days = int(cfg.get("restart_interval_days", 7))
        self._is_device_free = is_device_free
        self._trigger_restart = trigger_restart
        self._lock = threading.Lock()
        threading.Thread(
            target=self._cleanup_loop, daemon=True, name="hannah-satellitemanager-cleanup"
        ).start()

    def _requestor_trust_level(self, requestor_id: int) -> int:
        user = self._user_manager.get_user_by_id(requestor_id) if self._user_manager else None
        return user.trust_level if user else 0

    def _check_admin(self, requestor_id: Optional[int]) -> None:
        """requestor_id=None: interner/systemseitiger Aufruf, keine Prüfung."""
        if requestor_id is None:
            return
        if self._requestor_trust_level(requestor_id) < 10:
            raise SatellitePermissionError(f"requestor {requestor_id} lacks trust level 10")

    def _check_own_or_admin(self, requestor_id: Optional[int], device_id: str) -> None:
        """Trustlevel 10 darf jeden Satelliten anfassen, ab 5 nur den eigenen (owner_user_id).
        Unzugewiesene Satelliten (owner_user_id NULL) bleiben Trustlevel 10 vorbehalten.
        requestor_id=None: interner/systemseitiger Aufruf, keine Prüfung."""
        if requestor_id is None:
            return
        trust = self._requestor_trust_level(requestor_id)
        if trust >= 10:
            return
        if trust < 5:
            raise SatellitePermissionError(f"requestor {requestor_id} lacks trust level 5")
        sat = self.get_satellite(device_id)
        if not sat or sat.owner_user_id != requestor_id:
            raise SatellitePermissionError(f"requestor {requestor_id} does not own satellite '{device_id}'")

    def provision_satellite(self, seed: str, display_name: str, room_id: Optional[str]) -> bool:
        """Pre-registers a satellite before flash. seed is a one-time pairing token,
        used as the device_id placeholder until pair_satellite() renames it."""
        try:
            db = self._db()
            existing = Satellite.get(db, device_id=seed)
            if existing:
                existing.update(seed=seed, display_name=display_name, room_id=room_id)
            else:
                Satellite.create(db, device_id=seed, seed=seed, display_name=display_name, room_id=room_id)
            log.info("SatelliteManager: provisioned satellite seed=%s name=%s", seed[:8], display_name)
            return True
        except Exception as e:
            log.error("SatelliteManager: provision_satellite failed: %s", e)
            return False

    def pair_satellite(self, device_id: str, seed: str) -> bool:
        """Links a device_id (eFuse MAC) to a pre-provisioned seed entry.

        Looks up the seed, renames the record's device_id to the hardware device_id,
        and clears the seed. Returns True if pairing succeeded, False if seed not found.

        device_id is the PRIMARY KEY here, which BaseModel.update() never touches
        (it always WHEREs on the current PK value) — the rename has to go through
        raw SQL rather than the model layer.
        """
        db = self._db()
        with self._lock:
            row = db.execute(
                "SELECT device_id FROM satellites WHERE seed = ?", (seed,)
            ).fetchone()
            if row is None:
                return False
            old_device_id = row[0]
            now = _now_sql()
            if old_device_id != device_id:
                try:
                    db.execute(
                        "UPDATE satellites SET device_id=?, seed=NULL, paired_at=? WHERE seed=?",
                        (device_id, now, seed),
                    )
                except sqlite3.IntegrityError:
                    db.execute("DELETE FROM satellites WHERE seed = ?", (seed,))
                    db.execute(
                        "UPDATE satellites SET seed=NULL, paired_at=? WHERE device_id=?",
                        (now, device_id),
                    )
            else:
                db.execute(
                    "UPDATE satellites SET seed=NULL, paired_at=? WHERE seed=?",
                    (now, seed),
                )
            db.commit()
        log.info("SatelliteManager: paired device_id=%s", device_id)
        return True

    def resolve_satellite_name(self, device_id: str) -> Optional[str]:
        """Return the provisioned display_name for a satellite, or None if not set."""
        sat = Satellite.get(self._db(), device_id=device_id)
        return sat.display_name if sat and sat.display_name else None

    def upsert_satellite(self, device_id: str) -> None:
        db = self._db()
        now = _now_sql()
        with self._lock:
            sat = Satellite.get(db, device_id=device_id)
            if sat:
                sat.update(last_seen=now)
            else:
                Satellite.create(db, device_id=device_id, last_seen=now)

    def set_satellite_room(self, device_id: str, room_id: Optional[str], requestor_id: Optional[int] = None) -> bool:
        self._check_own_or_admin(requestor_id, device_id)
        sat = Satellite.get(self._db(), device_id=device_id)
        if not sat:
            return False
        sat.update(room_id=room_id)
        return True

    def set_satellite_display_name(self, device_id: str, display_name: str, requestor_id: Optional[int] = None) -> bool:
        self._check_own_or_admin(requestor_id, device_id)
        sat = Satellite.get(self._db(), device_id=device_id)
        if not sat:
            return False
        sat.update(display_name=display_name)
        return True

    def set_satellite_owner(self, device_id: str, user_id: Optional[int], requestor_id: Optional[int] = None) -> bool:
        self._check_admin(requestor_id)
        sat = Satellite.get(self._db(), device_id=device_id)
        if not sat:
            return False
        sat.set_owner(user_id)
        return True

    def set_satellite_smalltalk_followup(self, device_id: str, enabled: bool, requestor_id: Optional[int] = None) -> bool:
        self._check_own_or_admin(requestor_id, device_id)
        sat = Satellite.get(self._db(), device_id=device_id)
        if not sat:
            return False
        sat.update(smalltalk_followup_listen=enabled)
        return True

    def get_satellite_smalltalk_followup(self, device_id: str) -> bool:
        sat = Satellite.get(self._db(), device_id=device_id)
        return bool(sat.smalltalk_followup_listen) if sat else False

    def set_satellite_firmware(self, device_id: str, version: str) -> None:
        """Persistiert die aktuell laufende Firmware-Version (vom `.../firmware`-MQTT-Report).
        Legt den Satelliten an, falls er noch nicht in der DB existiert. Löscht ein
        anstehendes update_available/new_version, sobald die gemeldete Version mit
        der zuvor als Ziel gemerkten new_version übereinstimmt (Update wurde installiert)."""
        db = self._db()
        with self._lock:
            sat = Satellite.get(db, device_id=device_id)
            if not sat:
                sat = Satellite.create(db, device_id=device_id, last_seen=_now_sql())
            if sat.new_version and sat.new_version == version:
                sat.update(firmware_version=version, update_available=False, new_version=None)
            else:
                sat.update(firmware_version=version)

    def set_satellite_update_available(self, device_id: str, new_version: str) -> None:
        """Vermerkt, dass für diesen Satelliten ein OTA-Update auf new_version ansteht
        (vom `.../ota/pending`-MQTT-Report)."""
        db = self._db()
        with self._lock:
            sat = Satellite.get(db, device_id=device_id)
            if not sat:
                sat = Satellite.create(db, device_id=device_id, last_seen=_now_sql())
            sat.update(update_available=True, new_version=new_version)

    def get_satellite_owner(self, device_id: str) -> Optional[int]:
        """Gibt die zugewiesene owner_user_id zurück oder None."""
        sat = Satellite.get(self._db(), device_id=device_id)
        return sat.owner_user_id if sat else None

    def get_satellite_room_map(self) -> dict[str, str]:
        """Gibt {device_id: room_id} für alle Satelliten mit DB-Raum-Zuweisung zurück."""
        rows = Satellite.select(self._db()).where("room_id IS NOT NULL").all()
        return {s.device_id: s.room_id for s in rows}

    def get_satellite_room(self, device_id: str) -> Optional[str]:
        """Gibt die zugewiesene room_id zurück oder None."""
        sat = Satellite.get(self._db(), device_id=device_id)
        return sat.room_id if sat else None

    def get_room_satellite_ids(self, room_id: str) -> list[str]:
        """Gibt alle device_ids zurück, die in der DB diesem Raum zugewiesen sind. #31"""
        rows = Satellite.select(self._db()).where(room_id=room_id).all()
        return [s.device_id for s in rows]

    def get_user_satellites(self, user_id: int) -> list[dict]:
        """Gibt alle Satelliten zurück, die der angegebenen Person als Owner zugeordnet sind."""
        rows = Satellite.select(self._db()).where(owner_user_id=user_id).all()
        return [{"device_id": s.device_id, "display_name": s.display_name, "room_id": s.room_id} for s in rows]

    def get_satellites(self) -> list[dict]:
        db = self._db()
        sats = Satellite.select(db).order_by("device_id").all()
        room_names = {r.room_id: r.display_name for r in Room.select(db).all()}
        owner_names = {u.id: u.display_name for u in User.select(db).all()}
        return [
            {
                "device_id": s.device_id,
                "display_name": s.display_name,
                "room_id": s.room_id,
                "last_seen": s.last_seen,
                "room_display_name": room_names.get(s.room_id),
                "owner_user_id": s.owner_user_id,
                "owner_display_name": owner_names.get(s.owner_user_id),
                "firmware_version": s.firmware_version,
                "update_available": bool(s.update_available),
                "new_version": s.new_version,
                "smalltalk_followup_listen": bool(s.smalltalk_followup_listen),
            }
            for s in sats
        ]

    def get_satellite(self, device_id: str) -> Optional[Satellite]:
        return Satellite.get(self._db(), device_id=device_id)

    def delete_satellite(self, device_id: str, requestor_id: Optional[int] = None) -> bool:
        self._check_admin(requestor_id)
        sat: Satellite = Satellite.get(self._db(), device_id=device_id)
        if not sat:
            return False
        sat.delete()
        log.info(f"SatelliteManager: Satellit '{device_id}' gelöscht")
        return True

    def cleanup_stale_seeds(self) -> int:
        """Löscht provisionierte, aber nie gepairte Satelliten (seed gesetzt) älter als seed_ttl_days."""
        stale = Satellite.select(self._db()).where(
            "seed IS NOT NULL AND created_at < datetime('now', ?)",
            f"-{self._seed_ttl_days} days",
        ).all()
        with self._lock:
            for s in stale:
                s.delete()
        if stale:
            log.info(
                "SatelliteManager: %d veraltete unpaired Seed(s) gelöscht (>%dd)",
                len(stale), self._seed_ttl_days,
            )
        return len(stale)

    def record_restart_report(self, device_id: str, restart_reason: str, restart_count: int) -> None:
        """Persistiert eine vom Satelliten gemeldete Neustart-Kennzahl (#165) —
        restart_count ist der geräteseitige NVS-Zähler (überlebt Neustarts),
        restart_reason unterscheidet "remote" (#161/#162), "ota" und "watchdog"
        (reaktiv) sowie harte Reset-Gründe (Brownout/Panic/...). Dedupliziert gegen
        Satellite.last_reported_restart_count: der Firmware-`firmware`-Topic wird
        mit retain=1 publiziert, jeder MQTT-(Re-)Connect von Core liefert daher den
        zuletzt retained Payload erneut — ohne diese Prüfung würde jeder Core-
        Neustart eine Phantom-Zeile in die Historie schreiben."""
        from hannah.models.satellite_restart import SatelliteRestart
        db = self._db()
        sat = Satellite.get(db, device_id=device_id)
        if not sat:
            # Gleiches Muster wie set_satellite_firmware(): der Satellit könnte
            # theoretisch noch nicht in der DB existieren (satellite_restarts hat
            # eine FK auf satellites, die Zeile muss also erst angelegt werden).
            sat = Satellite.create(db, device_id=device_id, last_seen=_now_sql())
        if sat.last_reported_restart_count == restart_count:
            return
        SatelliteRestart.create(
            db, device_id=device_id, restart_reason=restart_reason, restart_count=restart_count,
        )
        sat.update(last_reported_restart_count=restart_count)

    def get_restart_reports(self, device_id: str) -> list[dict]:
        """Gibt die Neustart-Historie eines Satelliten zurück, neueste zuerst (#165)."""
        from hannah.models.satellite_restart import SatelliteRestart
        rows = SatelliteRestart.select(self._db()).where(device_id=device_id).order_by("id DESC").all()
        return [
            {"reported_at": r.reported_at, "restart_reason": r.restart_reason, "restart_count": r.restart_count}
            for r in rows
        ]

    def mark_restarted(self, device_id: str) -> None:
        """Setzt last_restart_at auf jetzt — sowohl vom periodischen Scheduler (#162)
        als auch vom manuellen Remote-Neustart (#161) genutzt: ein Neustart ist ein
        Neustart, egal ob automatisch oder auf Zuruf ausgelöst."""
        sat = Satellite.get(self._db(), device_id=device_id)
        if sat:
            sat.update(last_restart_at=_now_sql())

    def check_and_restart_due_satellites(self, now: Optional[datetime.datetime] = None) -> int:
        """Präventiver Rejuvenation-Restart (#162): pro Satellit fällig, wenn seit
        last_restart_at mindestens restart_interval_days vergangen sind UND die
        aktuelle Stunde dem Hash-Offset des Geräts entspricht (Streuung über den Tag,
        damit nicht alle Satelliten gleichzeitig neu starten). Ausgelöst wird nur,
        wenn is_device_free(device_id) True liefert (kein aktives Gespräch/Stream/
        TTS/DND) — sonst wird der Versuch übersprungen und beim nächsten stündlichen
        Durchlauf erneut geprüft (last_restart_at bleibt unverändert, bis tatsächlich
        neugestartet wurde).

        now: optional injizierbarer Zeitpunkt (UTC) für Tests — Default ist die echte
        aktuelle Zeit."""
        if not self._trigger_restart:
            return 0
        now = now or datetime.datetime.now(datetime.timezone.utc)
        current_hour = now.hour
        restarted = 0
        due = Satellite.select(self._db()).where("last_restart_at IS NOT NULL").all()
        for sat in due:
            last = datetime.datetime.strptime(
                sat.last_restart_at, "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=datetime.timezone.utc)
            if (now - last).days < self._restart_interval_days:
                continue
            offset_hour = int(hashlib.md5(sat.device_id.encode()).hexdigest(), 16) % 24
            if current_hour != offset_hour:
                continue
            if self._is_device_free and not self._is_device_free(sat.device_id):
                log.info(
                    "SatelliteManager: Rejuvenation-Restart für '%s' fällig, aber gerade nicht frei — verschoben.",
                    sat.device_id,
                )
                continue
            log.info(
                "SatelliteManager: Rejuvenation-Restart für '%s' ausgelöst (zuletzt %s).",
                sat.device_id, sat.last_restart_at,
            )
            self._trigger_restart(sat.device_id)
            self.mark_restarted(sat.device_id)
            restarted += 1
        return restarted

    def _cleanup_loop(self) -> None:
        while True:
            time.sleep(self._CLEANUP_INTERVAL_S)
            try:
                self.cleanup_stale_seeds()
            except Exception as e:
                log.error("SatelliteManager: cleanup_stale_seeds fehlgeschlagen: %s", e)
            try:
                self.check_and_restart_due_satellites()
            except Exception as e:
                log.error("SatelliteManager: check_and_restart_due_satellites fehlgeschlagen: %s", e)
