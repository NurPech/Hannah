#!/usr/bin/env python3
"""
Testscript zum manuellen Veröffentlichen von MQTT-Nachrichten.

Verwendung:
  python mqtt_publish.py <topic> <value> [--type bool|str|int|float]

Beispiele:
  python mqtt_publish.py hue/0/Lampe/on true --type bool
  python mqtt_publish.py hue/0/Lampe/on false --type bool
  python mqtt_publish.py hue/0/Lampe/bri 128 --type int
  python mqtt_publish.py voice/device1/text "Hallo Welt"
"""

import argparse
import sys
import yaml
import paho.mqtt.client as mqtt


def parse_value(raw: str, typ: str):
    if typ == "bool":
        if raw.lower() in ("true", "1", "yes", "ja", "an", "ein"):
            return "true"
        elif raw.lower() in ("false", "0", "no", "nein", "aus"):
            return "false"
        else:
            print(f"Unbekannter bool-Wert: {raw!r}. Erlaubt: true/false/1/0/yes/no/ja/nein/an/aus/ein")
            sys.exit(1)
    elif typ == "int":
        return str(int(raw))
    elif typ == "float":
        return str(float(raw))
    else:
        return raw


def load_mqtt_cfg(config_path: str) -> dict:
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        return cfg.get("mqtt", {})
    except FileNotFoundError:
        return {}


def main():
    parser = argparse.ArgumentParser(description="MQTT Testpublisher")
    parser.add_argument("topic", help="MQTT-Topic (z.B. hue/0/Lampe/on)")
    parser.add_argument("value", help="Zu sendender Wert")
    parser.add_argument("--type", choices=["bool", "str", "int", "float"], default="str",
                        help="Datentyp des Wertes (default: str)")
    parser.add_argument("--host", default=None, help="MQTT-Broker Host")
    parser.add_argument("--port", type=int, default=None, help="MQTT-Broker Port")
    parser.add_argument("--username", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--config", default="config.yaml", help="Pfad zur config.yaml")
    parser.add_argument("--qos", type=int, choices=[0, 1, 2], default=1)
    args = parser.parse_args()

    cfg = load_mqtt_cfg(args.config)

    host = args.host or cfg.get("host", "localhost")
    port = args.port or cfg.get("port", 1883)
    username = args.username or cfg.get("username", "")
    password = args.password or cfg.get("password", "")

    payload = parse_value(args.value, args.type)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if username:
        client.username_pw_set(username, password or None)

    connected = False
    published = False

    def on_connect(c, userdata, flags, reason_code, properties):
        nonlocal connected
        if reason_code == 0:
            connected = True
        else:
            print(f"Verbindungsfehler: {reason_code}")
            sys.exit(1)

    def on_publish(c, userdata, mid, reason_code, properties):
        nonlocal published
        published = True

    client.on_connect = on_connect
    client.on_publish = on_publish

    print(f"Verbinde mit {host}:{port} ...")
    client.connect(host, port, keepalive=10)
    client.loop_start()

    import time
    timeout = 5.0
    start = time.time()
    while not connected and time.time() - start < timeout:
        time.sleep(0.05)

    if not connected:
        print("Timeout: Keine Verbindung zum Broker.")
        sys.exit(1)

    print(f"Sende → {args.topic}: {payload!r} (QoS {args.qos})")
    client.publish(args.topic, payload, qos=args.qos)

    start = time.time()
    while not published and time.time() - start < timeout:
        time.sleep(0.05)

    client.loop_stop()
    client.disconnect()
    print("Fertig.")


if __name__ == "__main__":
    main()
