#!/usr/bin/env python3
"""One-time migration for #139: absorbs the "routines" table into "triggers" as
when.phrase-conditioned Trigger rows — Routines/RoutineManager are retired in
favor of a single Trigger system. Safe to re-run — uses INSERT OR IGNORE
(matched by the generated triggers.id).

Does NOT touch or delete the "routines" table — kept as an audit trail/manual
rollback aid until the migration is verified.

Translates the old {"topic": "hannah/set/devices/...", "value": ...} MQTT-publish
actions into proper {"set_state": {"id": ..., "value": ...}} Trigger actions —
that MQTT-topic scheme is a 1:1 rename of the "javascript.0.virtualDevice."
ioBroker prefix and is no longer used ioBroker-side. Any {"topic": ...} entry
NOT matching that scheme is skipped (logged) rather than silently mistranslated.

Assumes hannah.db already has the "routines"/"triggers" tables (i.e. init_db()
has run at least once).

Usage:
    python migrate_routines_to_triggers.py [--hannah-db hannah.db]
"""
import argparse
import json
import re
import sqlite3

_OLD_MQTT_PREFIX = "hannah/set/devices/"
_NEW_STATE_PREFIX = "javascript.0.virtualDevice."


def _slugify(name: str) -> str:
    slug = name.strip().lower().replace(" ", "_")
    slug = re.sub(r"[^a-z0-9_]", "", slug)
    return slug or "routine"


def _unique_id(base: str, taken: set) -> str:
    if base not in taken:
        return base
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


def _translate_action(a: dict) -> dict | None:
    """Routine-Action -> Trigger-Action. None (übersprungen, geloggt) wenn ein
    {topic,value} nicht dem erwarteten hannah/set/devices/...-Schema entspricht."""
    if "say" in a:
        return {"say": a["say"], "room": a.get("room", "all")}
    topic = a.get("topic", "")
    if not topic.startswith(_OLD_MQTT_PREFIX):
        print(f"  WARNUNG: Topic {topic!r} folgt nicht dem erwarteten Schema — übersprungen, manuell nachpflegen.")
        return None
    state_id = _NEW_STATE_PREFIX + topic[len(_OLD_MQTT_PREFIX):].replace("/", ".")
    return {"set_state": {"id": state_id, "value": a.get("value")}}


def migrate(hannah_db_path: str) -> int:
    db = sqlite3.connect(hannah_db_path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")

    existing_ids = {row[0] for row in db.execute("SELECT id FROM triggers")}
    routines = db.execute("SELECT * FROM routines").fetchall()

    count = 0
    for r in routines:
        trigger_id = _unique_id(_slugify(r["name"]), existing_ids)
        existing_ids.add(trigger_id)

        when = [{"phrase": p} for p in json.loads(r["triggers"])]
        actions_in = json.loads(r["actions"])
        actions_out = [a for a in (_translate_action(a) for a in actions_in) if a is not None]

        db.execute(
            'INSERT OR IGNORE INTO triggers (id, "when", actions, say, room, cooldown) '
            "VALUES (?, ?, ?, ?, ?, ?)",
            (trigger_id, json.dumps(when), json.dumps(actions_out), r["reply"] or "", "all", 0),
        )
        count += 1
        print(f"  '{r['name']}' -> Trigger '{trigger_id}' ({len(actions_out)} Action(en))")

    db.commit()
    db.close()
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hannah-db", default="hannah.db")
    args = parser.parse_args()
    n = migrate(args.hannah_db)
    print(f"routines -> triggers: {n} Zeile(n) übernommen. 'routines'-Tabelle unverändert gelassen.")
