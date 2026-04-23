#!/usr/bin/env bash
# install.sh — Hannah Core installer / updater
#
# Clones the hannah repo, sets up a Python venv, and installs a systemd service.
#
# Usage:
#   ./install.sh              # install or update
#   ./install.sh --uninstall  # remove service (keeps config)
#
# Env vars:
#   REPO_URL     Full Git clone URL (default: public GitHub repo)
#   REPO_TOKEN   Optional token for private forks (injected into URL)
#
set -euo pipefail

# ── CONFIG ────────────────────────────────────────────────────────────────────
REPO_URL="${REPO_URL:-https://github.com/OWNER/hannah.git}"   # public default
REPO_TOKEN="${REPO_TOKEN:-}"
INSTALL_DIR="/opt/hannah-core"
CONFIG_DIR="/etc/hannah"
SERVICE_NAME="hannah"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
SERVICE_USER="hannah"
# ──────────────────────────────────────────────────────────────────────────────

info()  { echo "[INFO]  $*"; }
ok()    { echo "[OK]    $*"; }
err()   { echo "[ERROR] $*" >&2; exit 1; }

need() { command -v "$1" &>/dev/null || err "Required tool not found: $1"; }
need git
need python3
need systemctl

# Inject token into URL if provided (works for GitHub, GitLab, Gitea, …)
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
    rm -rf "${INSTALL_DIR}"
    ok "Uninstalled. Config in ${CONFIG_DIR} was kept."
}

[[ "${1:-}" == "--uninstall" ]] && { uninstall; exit 0; }

# ── Repo: clone or update ─────────────────────────────────────────────────────
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

info "Installing Python dependencies ..."
"${VENV}/bin/pip" install --upgrade pip --quiet
"${VENV}/bin/pip" install --quiet -r "${INSTALL_DIR}/core/requirements.txt"
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
    info "Example: ${INSTALL_DIR}/core/config.yaml (if it exists)"
fi

# ── systemd unit ──────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${SCRIPT_DIR}/hannah.service" ]]; then
    install -m 644 "${SCRIPT_DIR}/hannah.service" "$SERVICE_FILE"
else
    install -m 644 "${INSTALL_DIR}/core/deploy/hannah.service" "$SERVICE_FILE"
fi
ok "Service unit installed."

systemctl daemon-reload

# ── Start / Restart ───────────────────────────────────────────────────────────
if [[ ! -f "${CONFIG_DIR}/config.yaml" ]]; then
    ok "Installed. Place config.yaml in ${CONFIG_DIR} and run:"
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

ok "${SERVICE_NAME} is running."
systemctl status "${SERVICE_NAME}" --no-pager -l || true
