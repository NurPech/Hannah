#!/usr/bin/env bash
# install.sh — Hannah Proxy installer / updater
#
# Downloads the matching binary from the Update Server and
# installs it as a systemd service.
#
# Usage:
#   ./install.sh              # install or update to latest release
#   ./install.sh --uninstall  # remove service + binary
#
# Env vars:
#   UPDATE_SERVER_URL    Base URL of the Hannah Update Server
#   UPDATE_SERVER_TOKEN  Bearer token for the Update Server (optional, only required for non-public channels)
#   PROXY_CHANNEL        Channel prefix to install from (default: proxy-stable)
#                        Architecture suffix (-amd64 / -arm64) is appended automatically.
#
set -euo pipefail

# ── CONFIG ────────────────────────────────────────────────────────────────────
UPDATE_SERVER_URL="${UPDATE_SERVER_URL:-https://hannah-update.sgessinger.de}"
UPDATE_SERVER_TOKEN="${UPDATE_SERVER_TOKEN:-}"
PROXY_CHANNEL_BASE="${PROXY_CHANNEL:-proxy-stable}"
INSTALL_DIR="/usr/local/bin"
CONFIG_DIR="/etc/hannah-proxy"
SERVICE_NAME="hannah-proxy"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
SERVICE_USER="hannah"
# ──────────────────────────────────────────────────────────────────────────────

info()  { echo "[INFO]  $*"; }
ok()    { echo "[OK]    $*"; }
err()   { echo "[ERROR] $*" >&2; exit 1; }

need() { command -v "$1" &>/dev/null || err "Required tool not found: $1"; }
need curl
need systemctl

[[ $EUID -eq 0 ]] || err "This script must be run as root (try: sudo bash)."

# ── Architecture detection ─────────────────────────────────────────────────────
detect_arch() {
    case "$(uname -m)" in
        x86_64)  echo "amd64" ;;
        aarch64) echo "arm64" ;;
        *) err "Unsupported architecture: $(uname -m)" ;;
    esac
}

# ── Uninstall ─────────────────────────────────────────────────────────────────
uninstall() {
    info "Stopping and disabling ${SERVICE_NAME} ..."
    systemctl stop    "${SERVICE_NAME}" 2>/dev/null || true
    systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
    rm -f "${SERVICE_FILE}"
    systemctl daemon-reload
    rm -f "${INSTALL_DIR}/${SERVICE_NAME}"
    ok "Uninstalled. Config in ${CONFIG_DIR} was kept."
}

[[ "${1:-}" == "--uninstall" ]] && { uninstall; exit 0; }

# ── Resolve channel ───────────────────────────────────────────────────────────
AUTH_HEADER=()
[[ -n "$UPDATE_SERVER_TOKEN" ]] && AUTH_HEADER=(-H "Authorization: Bearer ${UPDATE_SERVER_TOKEN}")

ARCH=$(detect_arch)
PROXY_CHANNEL="${PROXY_CHANNEL_BASE}-${ARCH}"

info "Fetching latest proxy release from ${UPDATE_SERVER_URL} (channel: ${PROXY_CHANNEL}) ..."
LATEST_JSON=$(curl -sf \
    "${AUTH_HEADER[@]}" \
    "${UPDATE_SERVER_URL}/latest?channel=${PROXY_CHANNEL}")
LATEST_VERSION=$(echo "$LATEST_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['version'])")
info "Latest version: ${LATEST_VERSION} (${ARCH})"

# ── Download + extract binary ─────────────────────────────────────────────────
TMPTAR=$(mktemp /tmp/hannah-proxy-XXXXXX.tar.gz)
TMPDIR=$(mktemp -d /tmp/hannah-proxy-XXXXXX)
trap 'rm -f "$TMPTAR"; rm -rf "$TMPDIR"' EXIT

curl -sf \
    "${AUTH_HEADER[@]}" \
    -o "$TMPTAR" \
    "${UPDATE_SERVER_URL}/releases/${LATEST_VERSION}?channel=${PROXY_CHANNEL}"

tar -xzf "$TMPTAR" -C "$TMPDIR"
BINARY="${TMPDIR}/hannah-proxy"
[[ -f "$BINARY" ]] || err "hannah-proxy not found in downloaded archive."
file "$BINARY" | grep -q ELF || err "Extracted file is not a valid ELF binary."
chmod +x "$BINARY"

# ── Install binary ────────────────────────────────────────────────────────────
install -m 755 "$BINARY" "${INSTALL_DIR}/${SERVICE_NAME}"
ok "Binary installed to ${INSTALL_DIR}/${SERVICE_NAME}"

# ── Service user ──────────────────────────────────────────────────────────────
if ! id "$SERVICE_USER" &>/dev/null; then
    info "Creating system user '${SERVICE_USER}' ..."
    useradd -r -s /sbin/nologin "$SERVICE_USER"
fi

# ── Config directory ──────────────────────────────────────────────────────────
if [[ ! -d "$CONFIG_DIR" ]]; then
    mkdir -p "$CONFIG_DIR"
    chown "${SERVICE_USER}:${SERVICE_USER}" "$CONFIG_DIR"
    info "Created ${CONFIG_DIR} — place your config.yaml there."
fi

# ── systemd unit ──────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${SCRIPT_DIR}/hannah-proxy.service" ]]; then
    install -m 644 "${SCRIPT_DIR}/hannah-proxy.service" "$SERVICE_FILE"
    ok "Service unit installed to ${SERVICE_FILE}"
else
    info "No hannah-proxy.service found next to install.sh — skipping unit install."
    info "Download it from the repo and re-run, or manage the service manually."
fi

systemctl daemon-reload

# ── Start / Restart ───────────────────────────────────────────────────────────
if systemctl is-enabled --quiet "${SERVICE_NAME}" 2>/dev/null; then
    info "Restarting ${SERVICE_NAME} ..."
    systemctl restart "${SERVICE_NAME}"
else
    info "Enabling and starting ${SERVICE_NAME} ..."
    systemctl enable --now "${SERVICE_NAME}"
fi

ok "${SERVICE_NAME} ${LATEST_VERSION} is running."
systemctl status "${SERVICE_NAME}" --no-pager -l || true
