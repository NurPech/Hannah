#!/bin/bash
# Stage 2: Satellit aus Base-Image konfigurieren
# Schnell (~1-2 Min) — einfach für jeden Satelliten aufrufen.
#
# Voraussetzung: create_satellite-base-pi.sh wurde einmal ausgeführt.
#
# Verwendung:
#   bash configure_satellite-pi.sh \
#     --device wohnzimmer \
#     --room Wohnzimmer \
#     --mqtt-pass geheim \
#     [--broker 192.168.8.1] \
#     [--mqtt-user mqtt] \
#     [--wakeword-score 0.7] \
#     [--output /mnt/d/satellite-wohnzimmer.img]

set -e

# --- Defaults ---
BASE_IMAGE="/mnt/e/hanna_images/satellite-base.img"
MOUNT_POINT="/mnt/raspberry-cfg"
BROKER="192.168.8.1"
MQTT_USER="mqtt"
MQTT_PASS=""
WAKEWORD_MODEL="/home/pi/models/hannah.onnx"
WAKEWORD_SCORE="0.7"
DEVICE=""
ROOM=""
OUTPUT=""

# --- Argumente parsen ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --device)        DEVICE="$2";        shift 2 ;;
        --room)          ROOM="$2";          shift 2 ;;
        --broker)        BROKER="$2";        shift 2 ;;
        --mqtt-user)     MQTT_USER="$2";     shift 2 ;;
        --mqtt-pass)     MQTT_PASS="$2";     shift 2 ;;
        --wakeword-model) WAKEWORD_MODEL="$2"; shift 2 ;;
        --wakeword-score) WAKEWORD_SCORE="$2"; shift 2 ;;
        --base)          BASE_IMAGE="$2";    shift 2 ;;
        --output)        OUTPUT="$2";        shift 2 ;;
        *) echo "Unbekannte Option: $1"; exit 1 ;;
    esac
done

# --- Pflichtfelder prüfen ---
if [[ -z "$DEVICE" || -z "$ROOM" || -z "$MQTT_PASS" ]]; then
    echo "Fehler: --device, --room und --mqtt-pass sind Pflichtfelder."
    echo ""
    echo "Verwendung:"
    echo "  bash configure_satellite-pi.sh --device wohnzimmer --room Wohnzimmer --mqtt-pass geheim"
    exit 1
fi

if [[ -z "$OUTPUT" ]]; then
    OUTPUT="/mnt/e/hanna_images/satellite-pi-${DEVICE}.img"
fi

echo "🛰️  Konfiguriere Satellit: $DEVICE ($ROOM)"
echo "    Base-Image : $BASE_IMAGE"
echo "    Output     : $OUTPUT"
echo ""

# --- Base-Image kopieren ---
echo "📋 Kopiere Base-Image..."
cp "$BASE_IMAGE" "$OUTPUT"

# --- Mounten ---
echo "🔗 Mounte Image..."
sudo mkdir -p "$MOUNT_POINT"
LOOP_DEV=$(sudo losetup --show -fP "$OUTPUT")
sudo udevadm settle

ROOT_PART="${LOOP_DEV}p2"
BOOT_PART="${LOOP_DEV}p1"

sudo e2fsck -fy "$ROOT_PART" 2>/dev/null || true
sudo mount "$ROOT_PART" "$MOUNT_POINT"

if [ -d "$MOUNT_POINT/boot/firmware" ]; then
    sudo mount "$BOOT_PART" "$MOUNT_POINT/boot/firmware"
else
    sudo mount "$BOOT_PART" "$MOUNT_POINT/boot"
fi

# --- systemd-Service schreiben ---
echo "⚙️  Schreibe systemd-Service..."
sudo mkdir -p "$MOUNT_POINT/etc/systemd/system"

sudo tee "$MOUNT_POINT/etc/systemd/system/hannah-satellite-pi.service" > /dev/null <<SVCEOF
[Unit]
Description=Hannah Satellit (${DEVICE})
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/hannah-satellite-pi
ExecStart=/home/pi/hannah-satellite-pi/venv/bin/python satellite.py \\
  --device ${DEVICE} \\
  --room ${ROOM} \\
  --broker ${BROKER} \\
  --mqtt-user ${MQTT_USER} \\
  --mqtt-pass ${MQTT_PASS} \\
  --wakeword-model ${WAKEWORD_MODEL} \\
  --wakeword-score ${WAKEWORD_SCORE} \\
  --tts-rate 48000
Restart=on-failure
RestartSec=15
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hannah-satellite-pi
TimeoutStopSec=10
KillMode=mixed

[Install]
WantedBy=multi-user.target
SVCEOF

# Service aktivieren (Symlink in multi-user.target.wants)
sudo mkdir -p "$MOUNT_POINT/etc/systemd/system/multi-user.target.wants"
sudo ln -sf /etc/systemd/system/hannah-satellite-pi.service \
    "$MOUNT_POINT/etc/systemd/system/multi-user.target.wants/hannah-satellite-pi.service"

# --- Hostname setzen ---
echo "🏷️  Setze Hostname: $DEVICE ..."
echo "$DEVICE" | sudo tee "$MOUNT_POINT/etc/hostname" > /dev/null
sudo sed -i "s/127.0.1.1.*/127.0.1.1\t${DEVICE}/" "$MOUNT_POINT/etc/hosts" 2>/dev/null || \
    echo "127.0.1.1	${DEVICE}" | sudo tee -a "$MOUNT_POINT/etc/hosts" > /dev/null

# --- Aufräumen ---
echo "🧼 Räume auf..."
sync
sudo umount -R "$MOUNT_POINT"
sudo losetup -d "$LOOP_DEV"

echo ""
echo "🎉 Fertig: $OUTPUT"
echo "    Gerät   : $DEVICE"
echo "    Raum    : $ROOM"
echo "    Broker  : $BROKER"
