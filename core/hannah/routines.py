import logging
import os
from dataclasses import dataclass
from typing import Optional

import yaml

log = logging.getLogger(__name__)

def _normalize(s: str) -> str:
    s = s.lower()
    s = s.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    return s


@dataclass
class RoutineAction:
    topic: str
    value: str


@dataclass
class Routine:
    name: str
    triggers: list[str]
    actions: list[RoutineAction]
    reply: str = ""


class RoutineManager:
    def __init__(self, path: str):
        self._path = path
        self._routines: list[Routine] = []
        self._mtime: float = -1.0
        self._load()

    def match(self, text: str) -> Optional[Routine]:
        """Prüft ob text einen Routine-Trigger enthält. Hot-reloads bei Dateiänderung."""
        self._load()
        norm = _normalize(text)
        for routine in self._routines:
            for trigger in routine.triggers:
                if trigger in norm:
                    log.info(f"Routine '{routine.name}' getriggert durch '{trigger}'")
                    return routine
        return None

    def _load(self) -> None:
        if not os.path.exists(self._path):
            if self._mtime != -1.0:
                log.warning(f"Routines: Datei nicht gefunden: {self._path}")
                self._mtime = -1.0
                self._routines = []
            return

        mtime = os.path.getmtime(self._path)
        if mtime == self._mtime:
            return

        try:
            with open(self._path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}

            routines: list[Routine] = []
            for r in data.get("routines", []):
                actions = [
                    RoutineAction(topic=a["topic"], value=str(a.get("value", "true")))
                    for a in r.get("actions", [])
                ]
                routines.append(Routine(
                    name=r["name"],
                    triggers=[_normalize(t) for t in r.get("triggers", [])],
                    actions=actions,
                    reply=r.get("reply", ""),
                ))

            self._routines = routines
            self._mtime = mtime
            log.info(f"Routines: {len(routines)} Routine(n) geladen aus '{self._path}'")
        except Exception as e:
            log.error(f"Routines: Fehler beim Laden von '{self._path}': {e}")
