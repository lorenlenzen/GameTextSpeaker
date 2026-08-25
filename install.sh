#!/usr/bin/env bash
# Installs a "Game Text Speaker" launcher into your application menu.
#
# This doesn't need root and doesn't move or copy any of this project's
# files -- it writes a tiny .desktop entry into the standard per-user
# location (~/.local/share/applications), pointing at wherever THIS copy
# of the repo happens to live. That's the key thing a checked-in .desktop
# file can't do on its own: everyone who clones this repo puts it
# somewhere different, so the launcher has to be generated at install
# time, not shipped pre-filled-in.
#
# Safe to re-run any time, including after moving this folder -- it always
# regenerates the entry from game-text-speaker.desktop.template using the
# CURRENT location of this script.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST_DIR="$HOME/.local/share/applications"
DEST_FILE="$DEST_DIR/game-text-speaker.desktop"
TEMPLATE="$APP_DIR/game-text-speaker.desktop.template"

if [ ! -f "$TEMPLATE" ]; then
    echo "error: $TEMPLATE not found (run this script from inside the cloned repo)." >&2
    exit 1
fi

mkdir -p "$DEST_DIR"
sed "s|INSTALL_DIR|$APP_DIR|g" "$TEMPLATE" > "$DEST_FILE"
chmod +x "$DEST_FILE"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DEST_DIR" >/dev/null 2>&1 || true
fi

echo "Installed -- \"Game Text Speaker\" should now show up in your application menu."
echo "(Launcher written to: $DEST_FILE)"
echo
if [ ! -d "$APP_DIR/venv" ]; then
    echo "Note: no venv/ found yet at $APP_DIR -- the launcher won't work until you"
    echo "finish setup. Run:"
    echo "  python3 setup.py"
fi
