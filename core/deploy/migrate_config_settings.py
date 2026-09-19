#!/usr/bin/env python3
"""One-time migration for Issue #27 Phase 5 (extended by #115): copy the config.yaml
sections that move out of static YAML config into hannah.db. nlu.*/llm.system_prompt
go into the generic Settings module (settings_category/settings tables). ble.tags/cars
go directly into their own tables (ble_tags/cars/user_to_car, #115 — these were never a
good fit for the generic JSON-blob Settings schema). iobroker.state_names used to be
migrated here too, but is a hardcoded, non-editable fallback since #257 — see
SettingsManager.cleanup_legacy_settings() for removing an already-migrated leftover.
Safe to re-run - uses INSERT OR IGNORE throughout.

Assumes hannah.db already has all tables from hannah.utils.db.SCHEMA (i.e. init_db()
has run at least once - they're created by Hannah Core's normal startup), including
"users"/"linked_accounts" (needed to resolve ble.tags' username / cars' owner_roomies
to a Hannah users.id).

Everything else in config.yaml (udp, web_ui, grpc, audio, mqtt/asset_server
connection data, stt/tts backend & credentials, ble.stale_timeout,
iobroker.feedback_timeout) stays static YAML config and is not touched.

Usage:
    python migrate_config_settings.py [--config config.yaml] [--hannah-db hannah.db]
"""
import argparse
import json
import sqlite3

import yaml


def _ensure_category(db: sqlite3.Connection, path: str) -> int:
    row = db.execute("SELECT id FROM settings_category WHERE name = ?", (path,)).fetchone()
    if row:
        return row[0]
    parent_id = _ensure_category(db, path.rsplit(".", 1)[0]) if "." in path else None
    cur = db.execute("INSERT INTO settings_category (name, parent) VALUES (?, ?)", (path, parent_id))
    db.commit()
    return cur.lastrowid


def _create_setting(db: sqlite3.Connection, category_id: int, name: str, value) -> bool:
    cur = db.execute(
        "INSERT OR IGNORE INTO settings (category, name, value) VALUES (?, ?, ?)",
        (category_id, name, json.dumps(value)),
    )
    db.commit()
    return cur.rowcount > 0


def _user_id_by_username(db: sqlite3.Connection, username: str) -> int | None:
    row = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    return row[0] if row else None


def _user_id_by_roomie_id(db: sqlite3.Connection, roomie_id: str) -> int | None:
    row = db.execute(
        "SELECT user_id FROM linked_accounts WHERE provider = 'residents' AND external_id = ?",
        (f"{roomie_id}_roomie",),
    ).fetchone()
    return row[0] if row else None


def migrate_ble_tags(cfg: dict, db: sqlite3.Connection) -> int:
    tags = cfg.get("ble", {}).get("tags", [])
    if not tags:
        return 0
    count = 0
    for tag in tags:
        mac = tag.get("mac", "").lower()
        label = tag.get("label") or mac
        if not mac:
            continue
        username = tag.get("username")
        user_id = _user_id_by_username(db, username) if username else None
        if username and user_id is None:
            print(f"  ble.tags: '{label}' verweist auf unbekannten User '{username}' — Tippfehler in config.yaml?")
        cur = db.execute(
            "INSERT OR IGNORE INTO ble_tags (mac_address, label, user_id) VALUES (?, ?, ?)",
            (mac, label, user_id),
        )
        count += cur.rowcount
    db.commit()
    return count


def migrate_cars(cfg: dict, db: sqlite3.Connection) -> int:
    cars = cfg.get("cars") or ([cfg["car"]] if cfg.get("car") else [])
    if not cars:
        return 0
    count = 0
    for car in cars:
        topic_prefix = car.get("topic_prefix", "")
        if not topic_prefix:
            continue
        cur = db.execute(
            "INSERT OR IGNORE INTO cars (topic_prefix, home_address) VALUES (?, ?)",
            (topic_prefix, car.get("home_address", "")),
        )
        if cur.rowcount == 0:
            continue
        count += 1
        car_id = cur.lastrowid
        owner_roomies = car.get("owner_roomies", car.get("owner_roomie", ""))
        if not isinstance(owner_roomies, list):
            owner_roomies = [owner_roomies] if owner_roomies else []
        for roomie_id in owner_roomies:
            user_id = _user_id_by_roomie_id(db, roomie_id)
            if user_id is None:
                print(f"  cars: '{topic_prefix}' verweist auf unverlinkte Roomie-ID '{roomie_id}' — Owner wird übersprungen")
                continue
            db.execute("INSERT OR IGNORE INTO user_to_car (user_id, car_id) VALUES (?, ?)", (user_id, car_id))
    db.commit()
    return count


def migrate_nlu(cfg: dict, db: sqlite3.Connection) -> int:
    nlu = cfg.get("nlu", {})
    if not nlu:
        return 0
    cat = _ensure_category(db, "nlu")
    count = 0
    for key, value in nlu.items():
        if _create_setting(db, cat, key, value):
            count += 1
    return count


def migrate_llm(cfg: dict, db: sqlite3.Connection) -> int:
    prompt = cfg.get("llm", {}).get("system_prompt")
    if not prompt:
        return 0
    cat = _ensure_category(db, "llm")
    return 1 if _create_setting(db, cat, "system_prompt", prompt) else 0


def migrate(config_path: str, hannah_db_path: str) -> None:
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    db = sqlite3.connect(hannah_db_path)
    db.execute("PRAGMA foreign_keys=ON")

    print(f"ble.tags: {migrate_ble_tags(cfg, db)} Zeile(n) übernommen")
    print(f"cars: {migrate_cars(cfg, db)} Zeile(n) übernommen")
    print(f"nlu: {migrate_nlu(cfg, db)} Zeile(n) übernommen")
    print(f"llm.system_prompt: {migrate_llm(cfg, db)} Zeile(n) übernommen")

    db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--hannah-db", default="hannah.db")
    args = parser.parse_args()
    migrate(args.config, args.hannah_db)
