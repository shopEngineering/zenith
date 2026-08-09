#!/bin/bash
# Install ZENITH/OS as a launchd user agent so it starts on login/reboot and
# stays up (KeepAlive). Idempotent: re-run to update. macOS only.
set -euo pipefail

LABEL="com.zenith.os"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Launch via the wrapper (not python3 server.py directly) so the Claude auth
# token in ~/.config/zenith/claude-auth.env is sourced — required when running
# as a LaunchDaemon that can't read the GUI keychain (the env file is optional,
# so this is harmless for a keychain-authed dev box). See launch-zenith.sh.
LAUNCHER="$ROOT/launch-zenith.sh"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$ROOT/data/logs"

mkdir -p "$LOGDIR" "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$LAUNCHER</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$ROOT</string>
    <key>EnvironmentVariables</key>
    <dict>
        <!-- launchd's default PATH omits /opt/homebrew/bin, which hides tmux + claude
             from the server (breaks session persistence/re-adopt). Capture the installing
             shell's full PATH so the agent can find them. -->
        <key>PATH</key>
        <string>$PATH</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$LOGDIR/zenith.log</string>
    <key>StandardErrorPath</key>
    <string>$LOGDIR/zenith.log</string>
</dict>
</plist>
PLIST

# Reload cleanly if already loaded.
launchctl unload -w "$PLIST" 2>/dev/null || true
launchctl load -w "$PLIST"

echo "Installed $LABEL → $PLIST"
echo "Logs: $LOGDIR/zenith.log"
echo "ZENITH/OS will now start on login and be kept alive."
