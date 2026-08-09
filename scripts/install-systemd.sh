#!/usr/bin/env bash
# Install ZENITH/OS as a systemd USER service so it starts on login/boot and stays
# up (Restart=always). The Linux peer of install-launchd.sh. Idempotent: re-run to
# update. Linux only (needs systemd — not stock WSL1/older WSL2).
set -euo pipefail

UNIT="zenith.service"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER="$ROOT/server.py"
PY="$(command -v python3 || echo /usr/bin/python3)"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_PATH="$UNIT_DIR/$UNIT"

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemd (systemctl) not found — cannot install the auto-start service." >&2
  echo "On WSL, enable it: add 'systemd=true' under [boot] in /etc/wsl.conf, then 'wsl --shutdown'." >&2
  echo "Otherwise just run ZENITH manually with:  zenith" >&2
  exit 1
fi

mkdir -p "$UNIT_DIR"

# Capture the installing shell's full PATH so the service can find tmux + claude
# (a login-less systemd unit otherwise gets a minimal PATH — same trap launchd hit).
cat > "$UNIT_PATH" <<UNIT_EOF
[Unit]
Description=ZENITH/OS
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$PY $SERVER
WorkingDirectory=$ROOT
Environment=PATH=$PATH
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
UNIT_EOF

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT"

# Let the service run at boot even before (and without) an interactive login.
if command -v loginctl >/dev/null 2>&1; then
  loginctl enable-linger "$USER" >/dev/null 2>&1 \
    && echo "Lingering enabled — starts at boot without login." \
    || echo "Note: could not enable linger; service starts on your next login."
fi

echo "Installed $UNIT → $UNIT_PATH"
echo "Logs:  journalctl --user -u zenith -f"
echo "ZENITH/OS will now start on login/boot and be kept alive."
