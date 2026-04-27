#!/usr/bin/env bash
# auto-deploy.sh — Prüft ob neue Commits vorliegen und deployed bei Änderung.
# Wird von hannah-auto-deploy.timer alle 5 Minuten aufgerufen.
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/hannah-core}"
LOG_TAG="hannah-auto-deploy"

log() { logger -t "$LOG_TAG" "$*"; }

cd "$REPO_DIR"

# Aktuelle Commits holen (kein Merge, nur fetch)
git fetch origin --quiet

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/master)

if [ "$LOCAL" = "$REMOTE" ]; then
    exit 0
fi

log "Neue Version gefunden ($LOCAL → $REMOTE), starte Update..."

# Welche Dateien haben sich geändert?
CHANGED=$(git diff --name-only HEAD origin/master)

git restore .
git pull --ff-only --quiet
log "git pull abgeschlossen."

# Nur betroffene Services neu starten
if echo "$CHANGED" | grep -q "^core/"; then
    log "Core-Dateien geändert → hannah.service neu starten"
    systemctl restart hannah
    log "hannah.service neu gestartet."
fi

if echo "$CHANGED" | grep -q "^telegram/"; then
    log "Telegram-Dateien geändert → hannah-telegram neu starten"
    systemctl restart hannah-telegram
    log "hannah-telegram neu gestartet."
fi

log "Update abgeschlossen ($REMOTE)."
