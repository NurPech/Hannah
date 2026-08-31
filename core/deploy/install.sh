#!/usr/bin/env bash
# install.sh — Hannah Core installer / updater
#
# Downloads Hannah Core from the Update Server and installs it as a systemd service.
#
# Usage:
#   ./install.sh              # install or update
#   ./install.sh --uninstall  # remove service (keeps config)
#
# Env vars:
#   UPDATE_SERVER_URL    Base URL of the Hannah Update Server
#   UPDATE_SERVER_TOKEN  Bearer token for the Update Server (optional, only required for non-public channels)
#   CORE_CHANNEL         Channel to install from (default: core-stable)
#
set -euo pipefail

# ── CONFIG ────────────────────────────────────────────────────────────────────
UPDATE_SERVER_URL="${UPDATE_SERVER_URL:-https://hannah-update.sgessinger.de}"
UPDATE_SERVER_TOKEN="${UPDATE_SERVER_TOKEN:-}"
CORE_CHANNEL="${CORE_CHANNEL:-core-stable}"
INSTALL_DIR="/opt/hannah/core"
CONFIG_DIR="/etc/hannah"
SERVICE_NAME="hannah"
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

# -S: show errors despite -s. --retry: retry transient errors (curl honors a
# server's Retry-After header for 429/503 automatically). --retry-connrefused
# also retries a refused connection. --retry-max-time caps total retry time so
# a persistently unreachable server still fails eventually instead of hanging.
CURL_OPTS=(-sS -f --retry 5 --retry-connrefused --retry-max-time 300)

info "Fetching latest core release from ${UPDATE_SERVER_URL} (channel: ${CORE_CHANNEL}) ..."
LATEST_JSON=$(curl "${CURL_OPTS[@]}" \
    "${AUTH_HEADER[@]}" \
    "${UPDATE_SERVER_URL}/latest?channel=${CORE_CHANNEL}") \
    || err "Failed to fetch latest release info from ${UPDATE_SERVER_URL} (channel: ${CORE_CHANNEL})"
LATEST_VERSION=$(echo "$LATEST_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['version'])")
info "Latest version: ${LATEST_VERSION}"

TMPFILE=$(mktemp /tmp/hannah-core-XXXXXX.tar.gz)
trap 'rm -f "$TMPFILE"' EXIT

curl "${CURL_OPTS[@]}" \
    "${AUTH_HEADER[@]}" \
    -o "$TMPFILE" \
    "${UPDATE_SERVER_URL}/releases/${LATEST_VERSION}?channel=${CORE_CHANNEL}" \
    || err "Failed to download ${LATEST_VERSION} from ${UPDATE_SERVER_URL}"
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
install -m 644 "${INSTALL_DIR}/deploy/hannah.service" "$SERVICE_FILE"
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
