#!/usr/bin/env bash
# Removes the application-menu launcher installed by install.sh. Doesn't
# touch anything else -- your venv, settings, and region/marker files are
# untouched, and this repo's own files aren't deleted.
set -euo pipefail

DEST_FILE="$HOME/.local/share/applications/game-text-speaker.desktop"

if [ -f "$DEST_FILE" ]; then
    rm -f "$DEST_FILE"
    echo "Removed $DEST_FILE"
else
    echo "Nothing to remove -- $DEST_FILE doesn't exist."
fi

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
fi
