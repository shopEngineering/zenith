#!/usr/bin/env bash
# Install ZENITH/OS as a systemd USER service (per-user, Restart=always) for the
# .deb install. The app lives read-only under /opt/zenith; this variant points
# writable state at the user's ~/.zenith via ZENITH_STATE_DIR so nothing is
# written under the root-owned prefix. Idempotent — re-run to update.
#
# This ships to /opt/zenith/scripts/install-systemd.sh and is invoked by
# `zenith autostart`. It intentionally differs from the source-checkout
# scripts/install-systemd.sh (which uses a writable repo dir for state).
set -euo pipefail

UNIT="zenith.service"
APP_DIR="/opt/zenith"
SERVER="$APP_DIR/server.py"
STATE_DIR="${ZENITH_STATE_DIR:-$HOME/.zenith}"
PY="$(command -v python3 || echo /usr/bin/python3)"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_PATH="$UNIT_DIR/$UNIT"

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemd (systemctl) not found — cannot install the auto-start service." >&2
  echo "Just run ZENITH manually with:  zenith" >&2
  exit 1
fi

mkdir -p "$UNIT_DIR" "$STATE_DIR/logs"

# Capture the installing shell's full PATH so the login-less service can still
# find tmux + claude (same trap launchd/systemd hit with a minimal default PATH).
cat > "$UNIT_PATH" <<UNIT_EOF
[Unit]
Description=ZENITH/OS
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$PY $SERVER
WorkingDirectory=$APP_DIR
Environment=PATH=$PATH
Environment=ZENITH_STATE_DIR=$STATE_DIR
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
UNIT_EOF

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT"

if command -v loginctl >/dev/null 2>&1; then
  loginctl enable-linger "$USER" >/dev/null 2>&1 \
    && echo "Lingering enabled — starts at boot without login." \
    || echo "Note: could not enable linger; service starts on your next login."
fi

echo "Installed $UNIT → $UNIT_PATH"
echo "Logs:  journalctl --user -u zenith -f"
echo "ZENITH/OS will now start on login/boot and be kept alive."
