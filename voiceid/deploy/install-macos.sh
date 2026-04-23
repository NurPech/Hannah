#!/usr/bin/env bash
# install-macos.sh — Hannah Voice-ID installer für macOS (Apple Silicon)
#
# Klont das Repo, richtet ein Python-venv ein und installiert einen LaunchDaemon.
# Läuft als root (kein UserName im plist, analog zu Ollama und faster-whisper-server).
#
# Usage:
#   sudo bash install-macos.sh              # install oder update
#   sudo bash install-macos.sh --uninstall  # service entfernen (Profile bleiben)
#
# Env vars:
#   REPO_URL     Git-Clone-URL (default: public GitHub repo)
#   REPO_TOKEN   Optionaler Token für private Forks
#
set -euo pipefail

# ── CONFIG ────────────────────────────────────────────────────────────────────
REPO_URL="${REPO_URL:-https://github.com/OWNER/hannah.git}"
REPO_TOKEN="${REPO_TOKEN:-}"
INSTALL_DIR="/opt/hannah-voiceid"
PROFILES_DIR="/opt/hannah-voiceid/voice_profiles"
MEM_DIR="/opt/hannah-voiceid/mem"          # Embedding-Cache (kein RAM-Disk auf macOS nötig)
MEM_SYMLINK="/mnt/hannah_mem"              # app.py erwartet diesen Pfad
SERVICE_NAME="com.hannah.voiceid"
PLIST="/Library/LaunchDaemons/${SERVICE_NAME}.plist"
LOG="/var/log/hannah-voiceid.log"
# ──────────────────────────────────────────────────────────────────────────────

info()  { echo "[INFO]  $*"; }
ok()    { echo "[OK]    $*"; }
err()   { echo "[ERROR] $*" >&2; exit 1; }

need() { command -v "$1" &>/dev/null || err "Benötigtes Tool nicht gefunden: $1"; }
need git
need python3

[[ "$(uname)" == "Darwin" ]] || err "Dieses Script ist nur für macOS."
[[ "$EUID" -eq 0 ]] || err "Bitte als root ausführen: sudo bash $0"

if [[ -n "$REPO_TOKEN" ]]; then
    REPO_URL="${REPO_URL/https:\/\//https://oauth2:${REPO_TOKEN}@}"
fi

# ── Uninstall ─────────────────────────────────────────────────────────────────
uninstall() {
    info "Stoppe und entferne ${SERVICE_NAME} ..."
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    rm -f "$MEM_SYMLINK"
    rm -rf "$INSTALL_DIR"
    ok "Deinstalliert. Stimm-Profile in ${PROFILES_DIR} wurden behalten (falls vorhanden)."
}

[[ "${1:-}" == "--uninstall" ]] && { uninstall; exit 0; }

# ── Repo: clone oder update ───────────────────────────────────────────────────
if [[ -d "${INSTALL_DIR}/.git" ]]; then
    info "Repository aktualisieren in ${INSTALL_DIR} ..."
    git -C "${INSTALL_DIR}" pull --ff-only
else
    info "Repository klonen nach ${INSTALL_DIR} ..."
    git clone --depth=1 "${REPO_URL}" "${INSTALL_DIR}"
fi

# ── Python venv ───────────────────────────────────────────────────────────────
VENV="${INSTALL_DIR}/venv"
if [[ ! -d "$VENV" ]]; then
    info "Python venv erstellen ..."
    python3 -m venv "$VENV"
fi

info "Python-Abhängigkeiten installieren (torch + speechbrain, kann mehrere Minuten dauern) ..."
"${VENV}/bin/pip" install --upgrade pip --quiet
"${VENV}/bin/pip" install --quiet fastapi uvicorn python-multipart pyyaml
# Standard-torch für macOS enthält MPS-Support für Apple Silicon
"${VENV}/bin/pip" install --quiet torch torchaudio
"${VENV}/bin/pip" install --quiet speechbrain
ok "Python-Abhängigkeiten installiert."

# ── Verzeichnisse ─────────────────────────────────────────────────────────────
mkdir -p "$PROFILES_DIR"
mkdir -p "$MEM_DIR"
ok "Verzeichnisse angelegt: ${PROFILES_DIR}, ${MEM_DIR}"

# /mnt/hannah_mem → /opt/hannah-voiceid/mem (app.py erwartet diesen Pfad)
mkdir -p /mnt
if [[ ! -L "$MEM_SYMLINK" ]]; then
    ln -s "$MEM_DIR" "$MEM_SYMLINK"
    ok "Symlink angelegt: ${MEM_SYMLINK} → ${MEM_DIR}"
else
    info "Symlink ${MEM_SYMLINK} existiert bereits."
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
        <string>${INSTALL_DIR}/voiceid/app.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${INSTALL_DIR}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>/opt/hannah-voiceid</string>
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
ok "LaunchDaemon installiert: ${PLIST}"

# ── Service starten ───────────────────────────────────────────────────────────
if launchctl list | grep -q "$SERVICE_NAME"; then
    info "Service neu starten ..."
    launchctl unload "$PLIST" 2>/dev/null || true
fi

launchctl load "$PLIST"
ok "${SERVICE_NAME} gestartet."
info "Logs: tail -f ${LOG}"
info "Beim ersten Start wird das SpeechBrain-Modell heruntergeladen (~200 MB)."
info ""
info "Sprecher enrollen:"
info "  cd ${INSTALL_DIR} && source venv/bin/activate"
info "  python voiceid/enroll_voice.py --roomie leonie --host localhost"
