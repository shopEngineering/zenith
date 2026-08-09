#!/bin/bash
# Remove the ZENITH/OS launchd user agent. macOS only.
set -euo pipefail

LABEL="com.zenith.os"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ -f "$PLIST" ]; then
    launchctl unload -w "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Uninstalled $LABEL (removed $PLIST)."
else
    echo "$LABEL not installed (no $PLIST)."
fi
