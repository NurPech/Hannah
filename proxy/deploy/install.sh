#!/usr/bin/env bash
# install.sh — Hannah Proxy installer / updater
#
# Downloads the matching binary from the GitLab Package Registry and
# installs it as a systemd service.
#
# Usage:
#   ./install.sh              # install or update to latest release
#   ./install.sh v1.2.3       # install specific tag
#   ./install.sh --uninstall  # remove service + binary
#
# Required env vars:
#   GITLAB_URL     GitLab instance hosting the Package Registry
#   PROJECT_ID     Numeric project ID (Settings → General → Project ID)
#   GITLAB_TOKEN   Token with "read_package_registry" scope (optional for public projects)
#
# Note: This script downloads a pre-built binary from a Package Registry.
# If you're building from source: cd proxy && go build ./cmd/proxy
# Then copy the binary to /usr/local/bin/hannah-proxy manually.
#
set -euo pipefail

# ── CONFIG ────────────────────────────────────────────────────────────────────
GITLAB_URL="${GITLAB_URL:-}"           # required: GitLab instance URL
PROJECT_ID="${PROJECT_ID:-}"          # required: numeric project ID (Settings → General)
PACKAGE_NAME="hannah-proxy"
INSTALL_DIR="/usr/local/bin"
CONFIG_DIR="/etc/hannah-proxy"
SERVICE_NAME="hannah-proxy"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
SERVICE_USER="hannah-proxy"
# ──────────────────────────────────────────────────────────────────────────────

# ── Helpers ───────────────────────────────────────────────────────────────────
info()  { echo "[INFO]  $*"; }
ok()    { echo "[OK]    $*"; }
err()   { echo "[ERROR] $*" >&2; exit 1; }

need() { command -v "$1" &>/dev/null || err "Required tool not found: $1"; }
need curl
need systemctl

# ── Validate required config ──────────────────────────────────────────────────
[[ -z "$GITLAB_URL"  ]] && err "GITLAB_URL is required (e.g. https://gitlab.example.com)"
[[ -z "$PROJECT_ID"  ]] && err "PROJECT_ID is required (numeric GitLab project ID)"

# ── Auth ──────────────────────────────────────────────────────────────────────
GITLAB_TOKEN="${GITLAB_TOKEN:-}"
AUTH_HEADER=""
if [[ -n "$GITLAB_TOKEN" ]]; then
    AUTH_HEADER="PRIVATE-TOKEN: ${GITLAB_TOKEN}"
fi

api_get() {
    local url="$1"
    if [[ -n "$AUTH_HEADER" ]]; then
        curl -fsSL --header "$AUTH_HEADER" "$url"
    else
        curl -fsSL "$url"
    fi
}

# ── Architecture detection ─────────────────────────────────────────────────────
detect_arch() {
    case "$(uname -m)" in
        x86_64)  echo "amd64" ;;
        aarch64) echo "arm64" ;;
        *) err "Unsupported architecture: $(uname -m)" ;;
    esac
}

# ── Latest version lookup ──────────────────────────────────────────────────────
latest_version() {
    [[ -z "$PROJECT_ID" ]] && err "PROJECT_ID is not set. Edit install.sh or export PROJECT_ID=<id>."
    local url="${GITLAB_URL}/api/v4/projects/${PROJECT_ID}/packages?package_name=${PACKAGE_NAME}&order_by=created_at&sort=desc&per_page=1"
    local version
    version=$(api_get "$url" | grep -o '"version":"[^"]*"' | head -1 | cut -d'"' -f4)
    [[ -z "$version" ]] && err "Could not determine latest version. Check PROJECT_ID and GITLAB_TOKEN."
    echo "$version"
}

# ── Uninstall ─────────────────────────────────────────────────────────────────
uninstall() {
    info "Stopping and disabling ${SERVICE_NAME} ..."
    systemctl stop  "${SERVICE_NAME}" 2>/dev/null || true
    systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
    rm -f "${SERVICE_FILE}"
    systemctl daemon-reload
    rm -f "${INSTALL_DIR}/${SERVICE_NAME}"
    ok "Uninstalled. Config in ${CONFIG_DIR} was kept."
}

# ── Main ──────────────────────────────────────────────────────────────────────
[[ "${1:-}" == "--uninstall" ]] && { uninstall; exit 0; }

ARCH=$(detect_arch)
VERSION="${1:-$(latest_version)}"
BINARY_NAME="${PACKAGE_NAME}-linux-${ARCH}"
DOWNLOAD_URL="${GITLAB_URL}/api/v4/projects/${PROJECT_ID}/packages/generic/${PACKAGE_NAME}/${VERSION}/${BINARY_NAME}"

info "Installing ${PACKAGE_NAME} ${VERSION} (${ARCH}) ..."

# ── Download ──────────────────────────────────────────────────────────────────
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

info "Downloading ${DOWNLOAD_URL} ..."
if [[ -n "$AUTH_HEADER" ]]; then
    curl -fSL --header "$AUTH_HEADER" "$DOWNLOAD_URL" -o "$TMP"
else
    curl -fSL "$DOWNLOAD_URL" -o "$TMP"
fi
chmod +x "$TMP"

# Sanity-check: must be an ELF binary
file "$TMP" | grep -q ELF || err "Downloaded file is not a valid ELF binary."

# ── Install binary ────────────────────────────────────────────────────────────
install -m 755 "$TMP" "${INSTALL_DIR}/${SERVICE_NAME}"
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

ok "${SERVICE_NAME} ${VERSION} is running."
systemctl status "${SERVICE_NAME}" --no-pager -l || true
