#!/usr/bin/env bash
# Remove the ZENITH/OS systemd user service. Linux only.
set -euo pipefail

UNIT="zenith.service"
UNIT_PATH="$HOME/.config/systemd/user/$UNIT"

if command -v systemctl >/dev/null 2>&1; then
    systemctl --user disable --now "$UNIT" 2>/dev/null || true
fi
if [ -f "$UNIT_PATH" ]; then
    rm -f "$UNIT_PATH"
    command -v systemctl >/dev/null 2>&1 && systemctl --user daemon-reload 2>/dev/null || true
    echo "Uninstalled $UNIT (removed $UNIT_PATH)."
else
    echo "$UNIT not installed (no $UNIT_PATH)."
fi
# Note: linger is left enabled (harmless, may be used by other services).
# To disable it too:  loginctl disable-linger "$USER"
