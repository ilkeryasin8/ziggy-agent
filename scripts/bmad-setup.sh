#!/bin/bash
# BMad Method setup for Claude Code, pinned to one release
# Usage: ./scripts/bmad-setup.sh [--if-needed]
#
# Installs BMad's skills (.claude/skills/) and runtime (_bmad/) into this checkout; both are gitignored, so
# each developer runs it once (and again after a pull that changes BMAD_VERSION). Only needed to work on
# Ziggy with BMad's skills: Ziggy itself installs BMad in a target with `bmad`'s own setup.
# With --if-needed it exits at once when the pinned release is already installed, so it can run from a
# SessionStart hook in your own .claude/settings.local.json. BMAD_COMMUNICATION_LANGUAGE sets the
# language BMad's agents talk in (default English). To upgrade, change BMAD_VERSION in its own PR.

set -e

BMAD_VERSION="6.12.0"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MANIFEST="$PROJECT_DIR/_bmad/_config/manifest.yaml"

installed_version() { sed -n 's/^  version: //p' "$MANIFEST" 2>/dev/null | head -1; }

install() {
    local user_name
    user_name="$(git -C "$PROJECT_DIR" config user.name || true)"
    npx -y "bmad-method@${BMAD_VERSION}" install \
        --yes \
        --directory "$PROJECT_DIR" \
        --modules bmm \
        --tools claude-code \
        --no-shims \
        ${user_name:+--user-name "$user_name"} \
        --communication-language "${BMAD_COMMUNICATION_LANGUAGE:-English}" \
        --document-output-language English \
        </dev/null
}

if [ "$1" = "--if-needed" ]; then
    # Session-start mode: never block the session; stdout reaches Claude as context.
    if [ -f "$PROJECT_DIR/_bmad/config.user.toml" ] && [ "$(installed_version)" = "$BMAD_VERSION" ]; then
        exit 0
    fi
    for tool in npx uv; do
        if ! command -v "$tool" >/dev/null; then
            echo "BMad ${BMAD_VERSION} is not set up: '$tool' is missing. Tell the user to install it (Node.js 20.12+ for npx, https://docs.astral.sh/uv/ for uv) and run ./scripts/bmad-setup.sh."
            exit 0
        fi
    done
    log="$(mktemp -t bmad-setup.XXXXXX)"
    if install >"$log" 2>&1; then
        echo "BMad ${BMAD_VERSION} was set up for this checkout at session start."
        rm -f "$log"
    else
        echo "BMad ${BMAD_VERSION} setup failed at session start (log: $log). Tell the user to run ./scripts/bmad-setup.sh."
    fi
    exit 0
fi

command -v npx >/dev/null || { echo "❌ npx not found: install Node.js 20.12 or later" >&2; exit 1; }
command -v uv >/dev/null || { echo "❌ uv not found: https://docs.astral.sh/uv/ (bmad-build halts without it)" >&2; exit 1; }

echo "🔧 Installing BMad Method ${BMAD_VERSION} for Claude Code..."
install
echo "✅ BMad ready. Personal overrides go in _bmad/custom/config.user.toml (gitignored)."
