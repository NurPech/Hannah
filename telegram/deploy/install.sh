#!/usr/bin/env bash
# install.sh — Hannah Telegram Bot installer / updater
#
# Downloads the Telegram bot from the Update Server and installs it as a systemd service.
#
# Usage:
#   ./install.sh              # install or update
#   ./install.sh --uninstall  # remove service (keeps config)
#
# Env vars:
#   UPDATE_SERVER_URL    Base URL of the Hannah Update Server
#   UPDATE_SERVER_TOKEN  Bearer token for the Update Server (optional, only required for non-public channels)
#   TELEGRAM_CHANNEL     Channel to install from (default: telegram-stable)
#
set -euo pipefail

# ── CONFIG ────────────────────────────────────────────────────────────────────
UPDATE_SERVER_URL="${UPDATE_SERVER_URL:-https://hannah-update.sgessinger.de}"
UPDATE_SERVER_TOKEN="${UPDATE_SERVER_TOKEN:-}"
TELEGRAM_CHANNEL="${TELEGRAM_CHANNEL:-telegram-stable}"
INSTALL_DIR="/opt/hannah/telegram"
CONFIG_DIR="/etc/hannah-telegram"
SERVICE_NAME="hannah-telegram"
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
    rm -rf "${INSTALL_DIR}"
    ok "Uninstalled. Config in ${CONFIG_DIR} was kept."
}

[[ "${1:-}" == "--uninstall" ]] && { uninstall; exit 0; }

# ── Download latest release from Update Server ────────────────────────────────
AUTH_HEADER=()
[[ -n "$UPDATE_SERVER_TOKEN" ]] && AUTH_HEADER=(-H "Authorization: Bearer ${UPDATE_SERVER_TOKEN}")

info "Fetching latest telegram release from ${UPDATE_SERVER_URL} (channel: ${TELEGRAM_CHANNEL}) ..."
LATEST_JSON=$(curl -sf \
    "${AUTH_HEADER[@]}" \
    "${UPDATE_SERVER_URL}/latest?channel=${TELEGRAM_CHANNEL}")
LATEST_VERSION=$(echo "$LATEST_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['version'])")
info "Latest version: ${LATEST_VERSION}"

TMPFILE=$(mktemp /tmp/hannah-telegram-XXXXXX.tar.gz)
trap 'rm -f "$TMPFILE"' EXIT

curl -sf \
    "${AUTH_HEADER[@]}" \
    -o "$TMPFILE" \
    "${UPDATE_SERVER_URL}/releases/${LATEST_VERSION}?channel=${TELEGRAM_CHANNEL}"
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

info "Installing Python dependencies ..."
"${VENV}/bin/pip" install --upgrade pip --quiet
"${VENV}/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt"
ok "Python dependencies installed."

# ── Service user ──────────────────────────────────────────────────────────────
if ! id "$SERVICE_USER" &>/dev/null; then
    info "Creating system user '${SERVICE_USER}' ..."
    useradd -r -s /sbin/nologin "$SERVICE_USER"
fi
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"

# ── Config directory ──────────────────────────────────────────────────────────
if [[ ! -d "$CONFIG_DIR" ]]; then
    mkdir -p "$CONFIG_DIR"
    chown "${SERVICE_USER}:${SERVICE_USER}" "$CONFIG_DIR"
    info "Created ${CONFIG_DIR} — place your config.yaml there."
fi

# ── systemd unit ──────────────────────────────────────────────────────────────
install -m 644 "${INSTALL_DIR}/deploy/hannah-telegram.service" "$SERVICE_FILE"
ok "Service unit installed."

systemctl daemon-reload

# ── Start / Restart ───────────────────────────────────────────────────────────
if [[ ! -f "${CONFIG_DIR}/config.yaml" ]]; then
    ok "Installed ${LATEST_VERSION}. Place config.yaml in ${CONFIG_DIR} and run:"
    ok "  systemctl enable --now ${SERVICE_NAME}"
    exit 0
fi

if systemctl is-enabled --quiet "${SERVICE_NAME}" 2>/dev/null; then
    info "Restarting ${SERVICE_NAME} ..."
    systemctl restart "${SERVICE_NAME}"
else
    info "Enabling and starting ${SERVICE_NAME} ..."
    systemctl enable --now "${SERVICE_NAME}"
fi

ok "${SERVICE_NAME} is running (${LATEST_VERSION})."
systemctl status "${SERVICE_NAME}" --no-pager -l || true
