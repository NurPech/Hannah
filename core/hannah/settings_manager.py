"""
Hannah Settings Manager

Verwaltet, über hannah.models, Persistenz für konfigurierbare Werte, die aus
config.yaml in die DB gewandert sind (nlu.*, llm.system_prompt, iobroker.state_names).
ble.tags/cars haben seit #115 eigene Modelle + CRUD (siehe hannah.ble_tags/hannah.car_registry)
statt hier als JSON-Blob zu laufen. Zwei Tabellen:
  - settings_category: hierarchisch (self-referencing parent), name = voller
    Punkt-Pfad (z.B. "llm")
  - settings: Wert als JSON-Text, gehört zu genau einer Kategorie
"""
import sqlite3
from typing import Callable, Optional

from hannah.models.settings_category import SettingsCategory
from hannah.models.setting import Setting

# Generische, installationsunabhängige Defaults für Kategorien, die früher als
# Beispielwerte in config.example.yaml lagen (#114). llm.system_prompt wird mit ""
# geseedet statt mit einem Text-Default — core/hannah/llm.py's `if system_prompt:`-Guard
# behandelt das als No-Op (kein Persona-Prompt, aber auch kein Fehler), #115.
DEFAULT_NLU_SETTINGS: dict = {
    "turn_on_words": [
        "an", "einschalten", "anschalten", "anmachen", "einmachen", "starte", "aktiviere",
    ],
    "turn_off_words": [
        "aus", "ausschalten", "ausmachen", "ausdrehen", "stoppe", "deaktiviere",
    ],
    "category_words": {
        "licht": "light", "lichter": "light", "lampe": "light", "lampen": "light",
        "stecker": "socket", "strom": "socket",
        "heizung": "thermostat", "heizungen": "thermostat",
        "temperatur": "temperature_sensor", "temperaturen": "temperature_sensor", "warm": "temperature_sensor",
        "fenster": "window",
        "tuer": "door", "tueren": "door",
        "rollladen": "blind",
        "luftqualitaet": "air_quality_sensor", "iaq": "air_quality_sensor", "co2": "air_quality_sensor",
        "voc": "air_quality_sensor", "luftguete": "air_quality_sensor", "luft": "air_quality_sensor",
        "raumluft": "air_quality_sensor",
        "luftfeuchtigkeit": "humidity_sensor", "luftfeuchte": "humidity_sensor",
        "feuchtigkeit": "humidity_sensor", "feuchte": "humidity_sensor",
        "helligkeit": "illuminance_sensor", "lux": "illuminance_sensor",
        "watt": "socket", "leistung": "socket",
    },
    "query_words": ["ist", "sind", "wie", "was", "welche", "wieviel", "status"],
    "temperature_units": ["grad", "°c", "°", "celsius"],
    "percentage_units": ["prozent", "%"],
}

DEFAULT_IOBROKER_STATE_NAMES: dict = {
    "on": "on", "level": "level", "color": "color", "colorTemp": "colorTemp",
    "current": "current", "expected": "expected", "illuminance": "illuminance",
    "open": "open", "iaq": "iaq", "co2_equiv": "co2_equiv", "voc_equiv": "voc_equiv",
    "power": "power",
}


class SettingsManager:
    def __init__(self, db: Callable):
        self._db = db

    def get_categories(self) -> list[dict]:
        return [c.to_dict() for c in SettingsCategory.select(self._db()).all()]

    def get_settings(self) -> list[dict]:
        return [s.to_dict() for s in Setting.select(self._db()).all()]

    def get_category_id(self, path: str) -> Optional[int]:
        cat = SettingsCategory.get(self._db(), name=path)
        return cat.id if cat else None

    def ensure_category(self, path: str) -> int:
        """Get-or-create für eine Kategorie; legt fehlende Ahnen-Kategorien entlang
        des Punkt-Pfads an (z.B. "ble.tags" legt zuerst "ble" an, falls nötig)."""
        db = self._db()
        existing = SettingsCategory.get(db, name=path)
        if existing:
            return existing.id
        parent_id = None
        if "." in path:
            parent_id = self.ensure_category(path.rsplit(".", 1)[0])
        return SettingsCategory.create(db, name=path, parent=parent_id).id

    def create_setting(self, category_id: int, name: str, value) -> Optional[dict]:
        """Legt ein neues Setting an. Gibt None zurück wenn der Name in dieser
        Kategorie bereits existiert."""
        try:
            s = Setting.create(self._db(), category=category_id, name=name, value=value)
        except sqlite3.IntegrityError:
            return None
        return s.to_dict()

    def update_setting_value(self, setting_id: int, value) -> bool:
        s = Setting.get(self._db(), id=setting_id)
        if not s:
            return False
        s.update(value=value)
        return True

    def get_settings_dict(self, category_path: str) -> dict:
        """Rekonstruiert {setting_name: value} für eine Kategorie — für main.py's
        Adapter-Funktionen, die die Legacy-cfg-Shape für NLU/CarTracker/etc. bauen."""
        cat_id = self.get_category_id(category_path)
        if cat_id is None:
            return {}
        return {s["name"]: s["value"] for s in self.get_settings() if s["category"] == cat_id}

    def seed_defaults(self) -> None:
        """Befüllt "nlu", "iobroker" (state_names) und "llm" (system_prompt) mit generischen
        Defaults, falls die jeweilige Kategorie noch komplett leer ist — für Neuinstallationen
        mit leerer DB, die früher über Beispielwerte in config.example.yaml liefen (#114).
        Migrierte oder per Admin-UI editierte Werte werden nie überschrieben, da nur bei einer
        leeren Kategorie überhaupt geschrieben wird."""
        if not self.get_settings_dict("nlu"):
            cat = self.ensure_category("nlu")
            for name, value in DEFAULT_NLU_SETTINGS.items():
                self.create_setting(cat, name, value)
        if not self.get_settings_dict("iobroker"):
            cat = self.ensure_category("iobroker")
            self.create_setting(cat, "state_names", DEFAULT_IOBROKER_STATE_NAMES)
        if not self.get_settings_dict("llm"):
            cat = self.ensure_category("llm")
            self.create_setting(cat, "system_prompt", "")
