#!/usr/bin/env bash
# install.sh — Hannah Voice-ID installer / updater
#
# Clones the hannah repo (voiceid/ subdirectory), sets up a Python venv,
# mounts a RAM-disk for fast embedding lookups, and installs a systemd service.
#
# Usage:
#   ./install.sh              # install or update
#   ./install.sh --uninstall  # remove service (keeps voice profiles)
#
# Env vars:
#   REPO_URL     Full Git clone URL (default: public GitHub repo)
#   REPO_TOKEN   Optional token for private forks (injected into URL)
#
set -euo pipefail

# ── CONFIG ────────────────────────────────────────────────────────────────────
REPO_URL="${REPO_URL:-https://github.com/OWNER/hannah.git}"   # public default
REPO_TOKEN="${REPO_TOKEN:-}"
INSTALL_DIR="/opt/hannah-voiceid"
RAM_DISK="/mnt/hannah_mem"
RAM_DISK_SIZE="128M"
SERVICE_NAME="hannah-voiceid"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
SERVICE_USER="hannah-voiceid"
# ──────────────────────────────────────────────────────────────────────────────

info()  { echo "[INFO]  $*"; }
ok()    { echo "[OK]    $*"; }
err()   { echo "[ERROR] $*" >&2; exit 1; }

need() { command -v "$1" &>/dev/null || err "Required tool not found: $1"; }
need git
need python3
need systemctl

if [[ -n "$REPO_TOKEN" ]]; then
    REPO_URL="${REPO_URL/https:\/\//https://oauth2:${REPO_TOKEN}@}"
fi

# ── Uninstall ─────────────────────────────────────────────────────────────────
uninstall() {
    info "Stopping and disabling ${SERVICE_NAME} ..."
    systemctl stop    "${SERVICE_NAME}" 2>/dev/null || true
    systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
    rm -f "${SERVICE_FILE}"
    systemctl daemon-reload
    info "Removing ${INSTALL_DIR} ..."
    rm -rf "${INSTALL_DIR}"
    ok "Uninstalled. Voice profiles in ${RAM_DISK} (RAM) and ~/hannah/voice_profiles were kept."
}

[[ "${1:-}" == "--uninstall" ]] && { uninstall; exit 0; }

# ── Repo: clone or update ──────────────────────────────────────────────────────
if [[ -d "${INSTALL_DIR}/.git" ]]; then
    info "Updating repository in ${INSTALL_DIR} ..."
    git config --global --add safe.directory "${INSTALL_DIR}"
    git -C "${INSTALL_DIR}" pull --ff-only
else
    info "Cloning repository into ${INSTALL_DIR} ..."
    git clone --depth=1 "${REPO_URL}" "${INSTALL_DIR}"
fi

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
PROFILES_DIR="${INSTALL_DIR}/hannah/voice_profiles"
mkdir -p "$PROFILES_DIR"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}/hannah"
ok "Voice profiles directory: ${PROFILES_DIR}"

# ── Config directory ──────────────────────────────────────────────────────────
CONFIG_DIR="/etc/hannah-voiceid"
if [[ ! -d "$CONFIG_DIR" ]]; then
    mkdir -p "$CONFIG_DIR"
    chown "${SERVICE_USER}:${SERVICE_USER}" "$CONFIG_DIR"
    info "Created ${CONFIG_DIR} — place your config.yaml there."
    info "Example: ${INSTALL_DIR}/voiceid/config.yaml"
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
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${SCRIPT_DIR}/hannah-voiceid.service" ]]; then
    install -m 644 "${SCRIPT_DIR}/hannah-voiceid.service" "$SERVICE_FILE"
else
    install -m 644 "${INSTALL_DIR}/voiceid/deploy/hannah-voiceid.service" "$SERVICE_FILE"
fi
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

ok "${SERVICE_NAME} is running."
systemctl status "${SERVICE_NAME}" --no-pager -l || true
