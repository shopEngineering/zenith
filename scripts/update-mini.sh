#!/usr/bin/env bash
# Update a remote SOURCE install of ZENITH/OS over SSH and restart it.
#
#   scripts/update-mini.sh [user@host] [remote-dir]
#
# Point it at a remote source install via positional args, the ZENITH_MINI_HOST /
# ZENITH_MINI_DIR env vars, or a gitignored data/deploy.env (auto-sourced) that sets
# them for this machine. Ships with generic placeholder defaults.
#
# It rsyncs only the runtime files (server.py, sibling modules, static/, VERSION) —
# the remote runs them with its own python3, so there's no rebuild and no .dmg. The
# remote's data/ + statusline live under the source dir and are left untouched.
#
# Restart is per-OS (detected via `uname`):
#   macOS  -> sudo launchctl kickstart -k system/com.zenith.os   (LaunchDaemon)
#   Linux  -> systemctl --user restart zenith                    (systemd user unit)
# Override the Linux unit / scope with ZENITH_SYSTEMD_UNIT (default "zenith") and
# ZENITH_SYSTEMD_SCOPE ("user" | "system", default "user").
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# Optional per-machine deploy config (gitignored): sets ZENITH_MINI_HOST/DIR/etc.
[ -f "$ROOT/data/deploy.env" ] && . "$ROOT/data/deploy.env"

HOST="${1:-${ZENITH_MINI_HOST:-user@zenith-host}}"
DIR="${2:-${ZENITH_MINI_DIR:-$HOME/zenith-src}}"
SYSTEMD_UNIT="${ZENITH_SYSTEMD_UNIT:-zenith}"
SYSTEMD_SCOPE="${ZENITH_SYSTEMD_SCOPE:-user}"

echo "==> Detecting remote OS"
OS="$(ssh "$HOST" 'uname -s' 2>/dev/null || echo unknown)"
echo "    ${HOST} is ${OS}"

echo "==> Syncing source to ${HOST}:${DIR}"
ssh "$HOST" "mkdir -p '${DIR}'"
rsync -az server.py zenith_store.py zenith_agents.py VERSION static launch-zenith.sh "${HOST}:${DIR}/"

echo "==> Restarting the daemon"
case "$OS" in
  Darwin)
    ssh "$HOST" "sudo launchctl kickstart -k system/com.zenith.os"
    ;;
  Linux)
    if [ "$SYSTEMD_SCOPE" = "system" ]; then
      ssh "$HOST" "sudo systemctl restart '${SYSTEMD_UNIT}'"
    else
      # --user over SSH needs XDG_RUNTIME_DIR pointed at the user's runtime bus.
      ssh "$HOST" "XDG_RUNTIME_DIR=/run/user/\$(id -u) systemctl --user restart '${SYSTEMD_UNIT}'"
    fi
    ;;
  *)
    echo "    unknown remote OS '${OS}' — restart ZENITH manually on ${HOST}." >&2
    exit 1
    ;;
esac

echo "==> Health check"
ssh "$HOST" 'for i in $(seq 1 20); do curl -fsS -o /dev/null http://127.0.0.1:8777/api/overview 2>/dev/null && break; sleep 1; done
curl -s http://127.0.0.1:8777/api/overview | python3 -c "import sys,json; d=json.load(sys.stdin); print(\"  version\", d.get(\"version\"), \"| projects\", d.get(\"projects\"))"'

echo "==> Done. Reach it with:  ssh -L 8777:127.0.0.1:8777 ${HOST}   then http://localhost:8777"
