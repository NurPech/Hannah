#!/usr/bin/env bash
# install-macos.sh — Hannah Voice-ID installer for macOS
#
# Downloads voiceid from the Update Server, sets up a Python venv, and
# installs a LaunchDaemon. Runs as root (no dedicated service user,
# analog to Ollama and faster-whisper-server).
#
# Code/venv (replaceable, lives in INSTALL_DIR) and voice profiles/cache
# (persistent, lives in DATA_DIR) are kept in separate trees, so neither
# an update nor --uninstall can ever touch the other.
#
# Usage:
#   sudo bash install-macos.sh              # install or update
#   sudo bash install-macos.sh --uninstall  # remove service (keeps voice profiles)
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
INSTALL_DIR="/opt/hannah/voiceid"
DATA_DIR="/opt/hannah/voiceid-data"
PROFILES_DIR="${DATA_DIR}/voice_profiles"
MEM_DIR="${DATA_DIR}/mem"            # plain dir on macOS — no RAM-disk mount needed
CONFIG_DIR="/opt/hannah/etc"
CONFIG_FILE="${CONFIG_DIR}/voiceid.yaml"
SERVICE_NAME="com.hannah.voiceid"
PLIST="/Library/LaunchDaemons/${SERVICE_NAME}.plist"
LOG="/opt/hannah/voiceid.log"
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
    ok "Uninstalled. Voice profiles in ${PROFILES_DIR} were kept."
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

info "Installing Python dependencies (torch + speechbrain, can take a few minutes) ..."
"${VENV}/bin/pip" install --upgrade pip --quiet
"${VENV}/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt"
# Standard torch wheel for macOS includes MPS support for Apple Silicon
"${VENV}/bin/pip" install --quiet torch torchaudio
"${VENV}/bin/pip" install --quiet speechbrain
ok "Python dependencies installed."

# ── Data directories (persistent, outside INSTALL_DIR) ────────────────────────
mkdir -p "$PROFILES_DIR" "$MEM_DIR" "$CONFIG_DIR"
ok "Data directories: ${PROFILES_DIR}, ${MEM_DIR}"

if [[ ! -f "$CONFIG_FILE" ]]; then
    info "No config at ${CONFIG_FILE} yet — running with defaults (unknown_threshold=0.25, uncertain_threshold=0.40)."
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
        <string>${INSTALL_DIR}/app.py</string>
        <string>--config</string>
        <string>${CONFIG_FILE}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${INSTALL_DIR}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>${INSTALL_DIR}</string>
        <key>VOICEID_MEM_PATH</key>
        <string>${MEM_DIR}</string>
        <key>VOICEID_DISK_PATH</key>
        <string>${PROFILES_DIR}</string>
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

# ── Service starten ───────────────────────────────────────────────────────────
if launchctl list | grep -q "$SERVICE_NAME"; then
    info "Restarting ${SERVICE_NAME} ..."
    launchctl kickstart -k "system/${SERVICE_NAME}"
else
    info "Loading ${SERVICE_NAME} ..."
    launchctl load "$PLIST"
fi

ok "${SERVICE_NAME} is running (${LATEST_VERSION})."
info "Logs: tail -f ${LOG}"
info ""
info "Enroll a speaker:"
info "  cd ${INSTALL_DIR} && source venv/bin/activate"
info "  python enroll_voice.py"
