#!/usr/bin/env bash
# Update external tools used by claude-ext extensions.
# Run periodically (e.g., weekly via cron) to keep tools current.
#
# Usage: ./scripts/update-external-tools.sh [--dry-run]
#
# External tools:
#   - agent-browser: interactive browser automation (npm)
#   - scrapling: web scraping with anti-bot bypass (pip)

set -euo pipefail

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
fi

# Resolve project venv
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_PIP="${PROJECT_DIR}/.venv/bin/pip"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${GREEN}[update]${NC} $*"; }
warn() { echo -e "${YELLOW}[update]${NC} $*"; }
err()  { echo -e "${RED}[update]${NC} $*"; }

# -- agent-browser (npm) ------------------------------------------------------

update_agent_browser() {
    local npm_prefix
    npm_prefix="$(npm config get prefix 2>/dev/null || echo "")"

    if ! command -v agent-browser &>/dev/null; then
        warn "agent-browser not installed, skipping."
        return 0
    fi

    local current
    current="$(agent-browser --version 2>/dev/null | awk '{print $NF}' || echo "unknown")"
    log "agent-browser: current version ${current}"

    local latest
    latest="$(npm view agent-browser version 2>/dev/null || echo "unknown")"
    log "agent-browser: latest version ${latest}"

    if [[ "$current" == "$latest" ]]; then
        log "agent-browser: already up to date."
        return 0
    fi

    if $DRY_RUN; then
        log "agent-browser: would update ${current} → ${latest} (dry-run)"
        return 0
    fi

    log "agent-browser: updating ${current} → ${latest}..."
    npm install -g agent-browser@latest 2>&1
    local new_ver
    new_ver="$(agent-browser --version 2>/dev/null | awk '{print $NF}' || echo "unknown")"
    log "agent-browser: updated to ${new_ver}"
}

# -- scrapling (pip) -----------------------------------------------------------

update_scrapling() {
    if [[ ! -x "$VENV_PIP" ]]; then
        err "Project venv not found at ${VENV_PIP}"
        return 1
    fi

    local current
    current="$("$VENV_PIP" show scrapling 2>/dev/null | grep "^Version:" | awk '{print $2}')"
    if [[ -z "$current" ]]; then
        warn "scrapling not installed, skipping."
        return 0
    fi
    log "scrapling: current version ${current}"

    local latest
    latest="$("$VENV_PIP" index versions scrapling 2>/dev/null | head -1 | grep -oP '\([0-9.]+\)' | tr -d '()' || true)"
    if [[ -z "$latest" ]]; then
        latest="$(curl -s "https://pypi.org/pypi/scrapling/json" | "$VENV_PIP" -c "import sys,json; print(json.load(sys.stdin)['info']['version'])" 2>/dev/null || echo "unknown")"
    fi
    log "scrapling: latest version ${latest}"

    if [[ "$current" == "$latest" ]]; then
        log "scrapling: already up to date."
        return 0
    fi

    if $DRY_RUN; then
        log "scrapling: would update ${current} → ${latest} (dry-run)"
        return 0
    fi

    log "scrapling: updating ${current} → ${latest}..."
    "$VENV_PIP" install --upgrade "scrapling[all]" 2>&1
    local new_ver
    new_ver="$("$VENV_PIP" show scrapling 2>/dev/null | grep "^Version:" | awk '{print $2}')"
    log "scrapling: updated to ${new_ver}"
}

# -- main ----------------------------------------------------------------------

log "Updating external tools..."
if $DRY_RUN; then
    log "(dry-run mode — no changes will be made)"
fi
echo

update_agent_browser
echo
update_scrapling

echo
log "Done."
