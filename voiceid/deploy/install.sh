#!/usr/bin/env bash
# install.sh — Hannah Voice-ID installer / updater
#
# Downloads Voice-ID from the Update Server, sets up a Python venv,
# mounts a RAM-disk for fast embedding lookups, and installs a systemd service.
#
# Usage:
#   ./install.sh              # install or update
#   ./install.sh --uninstall  # remove service (keeps voice profiles)
#
# Env vars:
#   UPDATE_SERVER_URL    Base URL of the Hannah Update Server
#   UPDATE_SERVER_TOKEN  Bearer token for the Update Server (optional, only required for non-public channels)
#   VOICEID_CHANNEL      Channel to install from (default: voiceid-stable)
#
set -euo pipefail

# ── CONFIG ────────────────────────────────────────────────────────────────────
UPDATE_SERVER_URL="${UPDATE_SERVER_URL:-https://hannah-update.sgessinger.de}"
UPDATE_SERVER_TOKEN="${UPDATE_SERVER_TOKEN:-}"
VOICEID_CHANNEL="${VOICEID_CHANNEL:-voiceid-stable}"
INSTALL_DIR="/opt/hannah-voiceid"
RAM_DISK="/mnt/hannah_mem"
RAM_DISK_SIZE="128M"
SERVICE_NAME="hannah-voiceid"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
SERVICE_USER="hannah"
# ──────────────────────────────────────────────────────────────────────────────

info()  { echo "[INFO]  $*"; }
ok()    { echo "[OK]    $*"; }
err()   { echo "[ERROR] $*" >&2; exit 1; }

need() { command -v "$1" &>/dev/null || err "Required tool not found: $1"; }
need python3
need curl
need systemctl

[[ $EUID -eq 0 ]] || err "This script must be run as root (try: sudo bash)."

# ── Uninstall ─────────────────────────────────────────────────────────────────
uninstall() {
    info "Stopping and disabling ${SERVICE_NAME} ..."
    systemctl stop    "${SERVICE_NAME}" 2>/dev/null || true
    systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
    rm -f "${SERVICE_FILE}"
    systemctl daemon-reload
    info "Removing ${INSTALL_DIR} ..."
    rm -rf "${INSTALL_DIR}"
    ok "Uninstalled. Voice profiles in ${RAM_DISK} (RAM) were kept."
}

[[ "${1:-}" == "--uninstall" ]] && { uninstall; exit 0; }

# ── Download latest release from Update Server ────────────────────────────────
AUTH_HEADER=()
[[ -n "$UPDATE_SERVER_TOKEN" ]] && AUTH_HEADER=(-H "Authorization: Bearer ${UPDATE_SERVER_TOKEN}")

info "Fetching latest voiceid release from ${UPDATE_SERVER_URL} (channel: ${VOICEID_CHANNEL}) ..."
LATEST_JSON=$(curl -sf \
    "${AUTH_HEADER[@]}" \
    "${UPDATE_SERVER_URL}/latest?channel=${VOICEID_CHANNEL}")
LATEST_VERSION=$(echo "$LATEST_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['version'])")
info "Latest version: ${LATEST_VERSION}"

TMPFILE=$(mktemp /tmp/hannah-voiceid-XXXXXX.tar.gz)
trap 'rm -f "$TMPFILE"' EXIT

curl -sf \
    "${AUTH_HEADER[@]}" \
    -o "$TMPFILE" \
    "${UPDATE_SERVER_URL}/releases/${LATEST_VERSION}?channel=${VOICEID_CHANNEL}"
ok "Downloaded ${LATEST_VERSION}."

# ── Extract to install dir ────────────────────────────────────────────────────
mkdir -p "${INSTALL_DIR}"
tar -xzf "$TMPFILE" -C "${INSTALL_DIR}"
ok "Extracted to ${INSTALL_DIR}."

# ── Python venv ───────────────────────────────────────────────────────────────
VENV="${INSTALL_DIR}/venv"
if [[ ! -d "$VENV" ]]; then
    info "Creating Python venv ..."
    python3 -m venv "$VENV"
fi

info "Installing Python dependencies (this may take a while — torch + speechbrain) ..."
"${VENV}/bin/pip" install --upgrade pip --quiet
"${VENV}/bin/pip" install --quiet \
    fastapi uvicorn python-multipart pyyaml \
    torch torchaudio --index-url https://download.pytorch.org/whl/cpu
"${VENV}/bin/pip" install --quiet speechbrain
ok "Python dependencies installed."

# ── Service user ──────────────────────────────────────────────────────────────
if ! id "$SERVICE_USER" &>/dev/null; then
    info "Creating system user '${SERVICE_USER}' ..."
    useradd -r -d "${INSTALL_DIR}" -s /sbin/nologin "$SERVICE_USER"
fi
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"

# ── Voice profiles directory (persistent, on SD card) ─────────────────────────
PROFILES_DIR="${INSTALL_DIR}/voice_profiles"
mkdir -p "$PROFILES_DIR"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "$PROFILES_DIR"
ok "Voice profiles directory: ${PROFILES_DIR}"

# ── Config directory ──────────────────────────────────────────────────────────
CONFIG_DIR="/etc/hannah-voiceid"
if [[ ! -d "$CONFIG_DIR" ]]; then
    mkdir -p "$CONFIG_DIR"
    chown "${SERVICE_USER}:${SERVICE_USER}" "$CONFIG_DIR"
    info "Created ${CONFIG_DIR} — place your config.yaml there."
fi

# ── RAM-disk ──────────────────────────────────────────────────────────────────
mkdir -p "$RAM_DISK"
chown "${SERVICE_USER}:${SERVICE_USER}" "$RAM_DISK"

FSTAB_ENTRY="tmpfs ${RAM_DISK} tmpfs defaults,size=${RAM_DISK_SIZE},uid=${SERVICE_USER},gid=${SERVICE_USER} 0 0"
if grep -qF "$RAM_DISK" /etc/fstab; then
    info "RAM-disk fstab entry already present — skipping."
else
    echo "$FSTAB_ENTRY" >> /etc/fstab
    ok "RAM-disk added to /etc/fstab (${RAM_DISK}, ${RAM_DISK_SIZE})."
fi

if ! mountpoint -q "$RAM_DISK"; then
    mount "$RAM_DISK"
    ok "RAM-disk mounted."
fi

# ── systemd unit ──────────────────────────────────────────────────────────────
install -m 644 "${INSTALL_DIR}/deploy/hannah-voiceid.service" "$SERVICE_FILE"
ok "Service unit installed."

systemctl daemon-reload

# ── Start / Restart ───────────────────────────────────────────────────────────
if systemctl is-enabled --quiet "${SERVICE_NAME}" 2>/dev/null; then
    info "Restarting ${SERVICE_NAME} ..."
    systemctl restart "${SERVICE_NAME}"
else
    info "Enabling and starting ${SERVICE_NAME} ..."
    systemctl enable --now "${SERVICE_NAME}"
fi

ok "${SERVICE_NAME} is running (${LATEST_VERSION})."
systemctl status "${SERVICE_NAME}" --no-pager -l || true
