#!/bin/bash
# Stage 1: Hannah-Satellit Base-Image bauen
# Einmalig ausführen — dauert ~20 Min.
# Ergebnis: BASE_OUTPUT (generisches Image mit allen Paketen + Modellen, kein Service)
#
# Danach für jeden Satelliten: configure_satellite-pi.sh aufrufen

# --- KONFIGURATION ---
IMAGE_PATH="/mnt/e/hanna_images/image.img"   # Quell-Raspbian-Image (unmodifiziert)
BASE_OUTPUT="/mnt/e/hanna_images/satellite-base.img"  # fertiges Base-Image
MOUNT_POINT="/mnt/raspberry"
BUILD_CACHE="satelite_cache/cache.img"  # optional: Verzeichnis für zwischengespeicherte Dateien (z.B. Modelle, Pakete)
BUILD_CACHE_MOUNT="/tmp/pip_cache"
SRC_DATA="satellite-pi"
WAKEWORD_MODEL="satellite-pi/models/hannah.onnx"   # lokaler Pfad zum custom Modell (leer = nur Standard-Modelle)
SSH_PUBLIC_KEY=""        # Eigenen SSH Public Key eintragen, z.B. "ssh-ed25519 AAAA... user@host"
WIFI_SSID=""             # WLAN-SSID eintragen
WIFI_COUNTRY="DE"
WIFI_PASS=""

echo "🚀 Starte Base-Image Build..."

# WLAN-Passwort abfragen (nicht als Argument — bleibt aus der Shell-History)
if [[ -z "$WIFI_PASS" ]]; then
    read -rsp "🔑 WLAN-Passwort für '$WIFI_SSID': " WIFI_PASS
    echo ""
    if [[ -z "$WIFI_PASS" ]]; then
        echo "Fehler: WLAN-Passwort darf nicht leer sein."
        exit 1
    fi
fi

# 0. Voraussetzungen & Aufräumen
sudo apt-get update && sudo apt-get install -y util-linux parted arch-install-scripts qemu-user-static

echo "🧹 Räume alte Mounts und Loops auf..."
sudo umount -R "$MOUNT_POINT/root/.cache" 2>/dev/null || true
sudo umount "$BUILD_CACHE_MOUNT"
sudo umount -R "$MOUNT_POINT" 2>/dev/null || true
sudo losetup -D 2>/dev/null || true

# 1. Quell-Image ins Base-Output kopieren und vergrößern
echo "📋 Kopiere $IMAGE_PATH → $BASE_OUTPUT ..."
cp "$IMAGE_PATH" "$BASE_OUTPUT"
echo "📏 Vergrößere Image-Datei um 2GB..."
truncate -s +2G "$BASE_OUTPUT"
sync

# 2. Partitionstabelle anpassen
echo "🔧 Erweitere Partition 2 auf das Maximum..."
sleep 2
sudo parted -s "$BASE_OUTPUT" resizepart 2 100%
sync
sleep 2

# 3. Loop-Device einbinden
echo "🔗 Binde Image als Loop-Device ein..."
LOOP_DEV=$(sudo losetup --show -fP "$BASE_OUTPUT")

if [ -z "$LOOP_DEV" ]; then
    echo "❌ FEHLER: Loop-Device konnte nicht erstellt werden!"
    exit 1
fi

sudo udevadm settle
sleep 2

BOOT_PART="${LOOP_DEV}p1"
ROOT_PART="${LOOP_DEV}p2"

# 4. Dateisystem vergrößern
echo "🛠️ Maximiere Dateisystem auf $ROOT_PART..."
sudo e2fsck -fy "$ROOT_PART"
sudo resize2fs "$ROOT_PART"

# 5. Mounten
echo "📂 Mounte Partitionen..."
sudo mkdir -p "$MOUNT_POINT"
sudo mount "$ROOT_PART" "$MOUNT_POINT"

if [ -d "$MOUNT_POINT/boot/firmware" ]; then
    BOOT_DIR="$MOUNT_POINT/boot/firmware"
else
    BOOT_DIR="$MOUNT_POINT/boot"
    sudo mkdir -p "$BOOT_DIR"
fi
sudo mount "$BOOT_PART" "$BOOT_DIR"

sudo mkdir -p "$MOUNT_POINT/root/.cache"

if [ ! -f "$BUILD_CACHE" ]; then
    echo "🆕 Erstelle 5GB Cache-Container auf der NVMe (via dd)..."
    # dd schreibt physisch 5GB Nullen. Sicherer für NTFS/WSL.
    sudo dd if=/dev/zero of="$BUILD_CACHE" bs=1M count=5120
    
    echo "🏗️ Formatiere als ext4..."
    mkfs.ext4 -F "$BUILD_CACHE"
fi

mkdir -p "$BUILD_CACHE_MOUNT"
if ! mountpoint -q "$BUILD_CACHE_MOUNT"; then
    sudo mount -o loop "$BUILD_CACHE" "$BUILD_CACHE_MOUNT"
fi

sudo mkdir -p "$MOUNT_POINT/root/.cache"
sudo mount --bind "$BUILD_CACHE_MOUNT" "$MOUNT_POINT/root/.cache"

sudo chown -R root:root "$MOUNT_POINT/root/.cache"

# 6. SSH & Daten vorbereiten
echo "✅ Aktiviere SSH..."
sudo touch "$BOOT_DIR/ssh"

# --- WLAN konfigurieren (NetworkManager, Debian 13 Trixie) ---
echo "📶 Konfiguriere WLAN ($WIFI_SSID, $WIFI_COUNTRY)..."
NM_DIR="$MOUNT_POINT/etc/NetworkManager/system-connections"
sudo mkdir -p "$NM_DIR"
sudo tee "$NM_DIR/${WIFI_SSID}.nmconnection" > /dev/null <<NMEOF
[connection]
id=${WIFI_SSID}
type=wifi
autoconnect=true

[wifi]
ssid=${WIFI_SSID}
mode=infrastructure

[wifi-security]
key-mgmt=wpa-psk
psk=${WIFI_PASS}

[ipv4]
method=auto

[ipv6]
method=auto
NMEOF
sudo chmod 600 "$NM_DIR/${WIFI_SSID}.nmconnection"
echo "    → NetworkManager-Profil geschrieben."

# NM-State: WiFi-Radio beim Start aktivieren (verhindert, dass 'nmcli radio wifi on' manuell nötig ist)
sudo mkdir -p "$MOUNT_POINT/var/lib/NetworkManager"
sudo tee "$MOUNT_POINT/var/lib/NetworkManager/NetworkManager.state" > /dev/null <<NMSTATE
[main]
NetworkingEnabled=true
WirelessEnabled=true
WWANEnabled=false
NMSTATE
echo "    → NetworkManager.state: WirelessEnabled=true gesetzt."

# NM conf: wlan0 explizit als managed markieren
sudo mkdir -p "$MOUNT_POINT/etc/NetworkManager/conf.d"
sudo tee "$MOUNT_POINT/etc/NetworkManager/conf.d/99-wifi-managed.conf" > /dev/null <<NMCONF
[keyfile]
unmanaged-devices=none
NMCONF
echo "    → NM conf: unmanaged-devices=none gesetzt."

# rfkill-Persistenz löschen — verhindert soft-block beim nächsten Boot
sudo rm -f "$MOUNT_POINT/var/lib/systemd/rfkill/"*
echo "    → Persistierter rfkill-Block gelöscht."

# Regulatory domain für WiFi (gegen soft-block durch fehlende Länderinfo)
echo "REGDOMAIN=${WIFI_COUNTRY}" | sudo tee "$MOUNT_POINT/etc/default/crda" > /dev/null
echo "    → /etc/default/crda: REGDOMAIN=${WIFI_COUNTRY} gesetzt."

echo "🌍 Setze WiFi-Country in der cmdline.txt (gegen rfkill-Block)..."

# Pfad zur cmdline.txt basierend auf deinem BOOT_DIR
CMDLINE_FILE="$BOOT_DIR/cmdline.txt"

if [ -f "$CMDLINE_FILE" ]; then
    # Prüfen, ob der Eintrag schon existiert, falls nicht: ans Ende der Zeile hängen
    if ! grep -q "cfg80211.ieee80211_regdom=" "$CMDLINE_FILE"; then
        # Wichtig: cmdline.txt darf nur EINE Zeile haben. 
        # Wir hängen es mit einem Leerzeichen davor ans Ende der ersten Zeile.
        sudo sed -i "1s/$/ cfg80211.ieee80211_regdom=$WIFI_COUNTRY/" "$CMDLINE_FILE"
        echo "✅ Eintrag hinzugefügt: cfg80211.ieee80211_regdom=$WIFI_COUNTRY"
    else
        echo "ℹ️ Eintrag existiert bereits in cmdline.txt"
    fi
else
    echo "⚠️ Fehler: $CMDLINE_FILE nicht gefunden! Ist die Boot-Partition korrekt gemountet?"
fi

# Journald: Logs nur im RAM halten (schont SD-Karte)
echo "📋 Konfiguriere journald (RAM-only)..."
sudo mkdir -p "$MOUNT_POINT/etc/systemd/journald.conf.d"
sudo tee "$MOUNT_POINT/etc/systemd/journald.conf.d/00-ramlog.conf" > /dev/null <<JRNEOF
[Journal]
Storage=volatile
RuntimeMaxUse=32M
JRNEOF
echo "    → journald: Storage=volatile, max 32M."

echo "📦 Kopiere Projektdaten..."
sudo mkdir -p "$MOUNT_POINT/home/pi/hannah-satellite-pi"
if [ -d "$SRC_DATA" ]; then
    sudo cp -r "$SRC_DATA/." "$MOUNT_POINT/home/pi/hannah-satellite-pi/"
else
    echo "⚠️ Warnung: Quellordner '$SRC_DATA' nicht gefunden!"
fi

# Custom Wakeword-Modell ins Image kopieren
if [ -n "$WAKEWORD_MODEL" ] && [ -f "$WAKEWORD_MODEL" ]; then
    echo "🎤 Kopiere Wakeword-Modell: $WAKEWORD_MODEL ..."
    sudo mkdir -p "$MOUNT_POINT/home/pi/models"
    sudo cp "$WAKEWORD_MODEL" "$MOUNT_POINT/home/pi/models/"
else
    echo "ℹ️  Kein custom Wakeword-Modell angegeben — nur Standard-Modelle."
fi

# 7. Chroot-Konfiguration
echo "🛠️ Starte Konfiguration im Chroot..."
# WICHTIG: <<'EOF' verhindert, dass Variablen lokal interpretiert werden
sudo arch-chroot "$MOUNT_POINT" /bin/bash <<'EOF'
    set -e
    
    # User-Setup
    if ! id -u pi > /dev/null 2>&1; then
        useradd -m -s /bin/bash pi
    fi

    echo "pi:raspberry" | chpasswd
    usermod -U pi
    chage -d -1 pi

    # SSH Key Setup
    mkdir -p /home/pi/.ssh
    echo "$SSH_PUBLIC_KEY" > /home/pi/.ssh/authorized_keys
    chown -R pi:pi /home/pi/.ssh
    chmod 700 /home/pi/.ssh
    chmod 600 /home/pi/.ssh/authorized_keys

    # Software-Installation
    apt-get update
    apt-get install -y python3-pip python3-venv python3-dev build-essential portaudio19-dev libopenblas0

    # Python Environment Setup
    cd /home/pi/hannah-satellite-pi
    echo "🐍 Erstelle Virtual Environment..."
    python3 -m venv venv --system-site-packages
    
    ./venv/bin/python -m pip install --no-cache-dir --upgrade pip
    
    if [ -f "install.sh" ]; then
        echo "📥 Installiere Abhängigkeiten via install.sh..."
        PYTHON=./venv/bin/python bash install.sh
    fi

    if [ -f "satellite.py" ]; then
        echo "📥 Lade Wake-Word-Modelle herunter..."
        ./venv/bin/python satellite.py --download-models
    fi

    # Rechte fixen
    chown -R pi:pi /home/pi/
    echo "✅ Base-Image Setup abgeschlossen — kein Service installiert (folgt in Stage 2)."
EOF

# 8. Aufräumen
echo "🧼 Räume auf..."
sync
sudo umount "$MOUNT_POINT/root/.cache"
sudo umount "$BUILD_CACHE_MOUNT"
sudo umount -R "$MOUNT_POINT"
sudo losetup -D

echo "🎉 Base-Image fertig: $BASE_OUTPUT"
echo ""
echo "Nächster Schritt — Satellit konfigurieren:"
echo "  bash configure_satellite-pi.sh --device wohnzimmer --room Wohnzimmer --mqtt-pass geheim"