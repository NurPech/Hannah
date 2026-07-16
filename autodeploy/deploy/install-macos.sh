#!/usr/bin/env bash
# install-macos.sh — Hannah AutoDeploy installer for macOS
#
# Downloads autodeploy.py from the Update Server, sets up a Python venv,
# and installs a LaunchDaemon. Runs as root (no dedicated service user,
# analog to the voiceid macOS installer).
#
# Usage:
#   sudo bash install-macos.sh              # install or update
#   sudo bash install-macos.sh --uninstall  # remove service (keeps config/state)
#
# Env vars:
#   UPDATE_SERVER_URL    Base URL of the Hannah Update Server
#   UPDATE_SERVER_TOKEN  Bearer token for the Update Server (optional, only required for non-public channels)
#   AUTODEPLOY_CHANNEL   Channel to install from (default: autodeploy-stable)
#
set -euo pipefail

# ── CONFIG ────────────────────────────────────────────────────────────────────
UPDATE_SERVER_URL="${UPDATE_SERVER_URL:-https://hannah-update.sgessinger.de}"
UPDATE_SERVER_TOKEN="${UPDATE_SERVER_TOKEN:-}"
AUTODEPLOY_CHANNEL="${AUTODEPLOY_CHANNEL:-autodeploy-stable}"
INSTALL_DIR="/opt/hannah/autodeploy"
CONFIG_DIR="/opt/hannah/etc"
STATE_DIR="/opt/hannah/var"
SERVICE_NAME="com.hannah.autodeploy"
PLIST="/Library/LaunchDaemons/${SERVICE_NAME}.plist"
LOG="/opt/hannah/autodeploy.log"
# ──────────────────────────────────────────────────────────────────────────────

info()  { echo "[INFO]  $*"; }
ok()    { echo "[OK]    $*"; }
err()   { echo "[ERROR] $*" >&2; exit 1; }

need() { command -v "$1" &>/dev/null || err "Required tool not found: $1"; }
need python3
need curl

[[ "$(uname)" == "Darwin" ]] || err "This script is for macOS only."
[[ "$EUID" -eq 0 ]] || err "Please run as root: sudo bash $0"

# ── Uninstall ─────────────────────────────────────────────────────────────────
uninstall() {
    info "Stopping and removing ${SERVICE_NAME} ..."
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    rm -rf "$INSTALL_DIR"
    ok "Uninstalled. Config in ${CONFIG_DIR} and state in ${STATE_DIR} were kept."
}

[[ "${1:-}" == "--uninstall" ]] && { uninstall; exit 0; }

# ── Download latest release from Update Server ────────────────────────────────
AUTH_HEADER=()
[[ -n "$UPDATE_SERVER_TOKEN" ]] && AUTH_HEADER=(-H "Authorization: Bearer ${UPDATE_SERVER_TOKEN}")

info "Fetching latest autodeploy release from ${UPDATE_SERVER_URL} (channel: ${AUTODEPLOY_CHANNEL}) ..."
LATEST_JSON=$(curl -sf \
    "${AUTH_HEADER[@]}" \
    "${UPDATE_SERVER_URL}/latest?channel=${AUTODEPLOY_CHANNEL}")
LATEST_VERSION=$(echo "$LATEST_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['version'])")
info "Latest version: ${LATEST_VERSION}"

TMPFILE=$(mktemp /tmp/autodeploy-XXXXXX.tar.gz)
trap 'rm -f "$TMPFILE"' EXIT

curl -sf \
    "${AUTH_HEADER[@]}" \
    -o "$TMPFILE" \
    "${UPDATE_SERVER_URL}/releases/${LATEST_VERSION}?channel=${AUTODEPLOY_CHANNEL}"
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

# ── Config and state directories ──────────────────────────────────────────────
mkdir -p "$CONFIG_DIR" "$STATE_DIR"

if [[ ! -f "${CONFIG_DIR}/autodeploy.yaml" ]]; then
    info "Created ${CONFIG_DIR} — place your autodeploy.yaml there."
    info "Example: ${INSTALL_DIR}/autodeploy.yaml.example"
fi

# ── LaunchDaemon ──────────────────────────────────────────────────────────────
cat > "$PLIST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${SERVICE_NAME}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${VENV}/bin/python</string>
        <string>${INSTALL_DIR}/autodeploy.py</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>AUTODEPLOY_CONFIG</key>
        <string>${CONFIG_DIR}/autodeploy.yaml</string>
        <key>AUTODEPLOY_STATE</key>
        <string>${STATE_DIR}/autodeploy-state.json</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${LOG}</string>
    <key>StandardErrorPath</key>
    <string>${LOG}</string>
</dict>
</plist>
EOF
ok "LaunchDaemon installed: ${PLIST}"

if [[ ! -f "${CONFIG_DIR}/autodeploy.yaml" ]]; then
    ok "Installed ${LATEST_VERSION}. Place autodeploy.yaml in ${CONFIG_DIR}, then run:"
    ok "  sudo launchctl load ${PLIST}"
    exit 0
fi

# ── Start / Restart ───────────────────────────────────────────────────────────
if launchctl list | grep -q "$SERVICE_NAME"; then
    info "Restarting ${SERVICE_NAME} ..."
    launchctl kickstart -k "system/${SERVICE_NAME}"
else
    info "Loading ${SERVICE_NAME} ..."
    launchctl load "$PLIST"
fi

ok "${SERVICE_NAME} is running (${LATEST_VERSION})."
info "Logs: tail -f ${LOG}"
