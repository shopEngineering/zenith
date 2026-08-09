#!/usr/bin/env bash
# ZENITH/OS launcher — installed to /usr/bin/zenith by the .deb package.
#
# The app itself lives under a root-owned, read-only prefix (/opt/zenith); this
# launcher runs it AS THE INVOKING USER and keeps ALL writable runtime state
# (pidfile, logs, data/, statusline script) under the user's ~/.zenith. That is
# achieved by exporting ZENITH_STATE_DIR, which server.py honours in source mode.
#
# Commands: start | stop | restart | status | logs | update | autostart | autostart-off
set -euo pipefail

ZENITH_DIR="/opt/zenith"                       # read-only app (server.py, static/, scripts/)
STATE_DIR="${ZENITH_STATE_DIR:-$HOME/.zenith}" # writable per-user state
export ZENITH_STATE_DIR="$STATE_DIR"

PORT="${ZENITH_PORT:-8777}"
URL="http://localhost:$PORT"
PIDFILE="$STATE_DIR/zenith.pid"
LOG="$STATE_DIR/logs/zenith.log"

running() { python3 -c "import urllib.request; urllib.request.urlopen('$URL/api/overview', timeout=2)" >/dev/null 2>&1; }

open_url() {
  if grep -qi microsoft /proc/version 2>/dev/null; then          # WSL → Windows browser
    command -v wslview >/dev/null 2>&1 && { wslview "$URL"; return; }
    command -v explorer.exe >/dev/null 2>&1 && { explorer.exe "$URL" >/dev/null 2>&1 || true; return; }
  fi
  command -v xdg-open >/dev/null 2>&1 && xdg-open "$URL" >/dev/null 2>&1 || true
}

start() {
  mkdir -p "$STATE_DIR/logs"
  if running; then
    echo "ZENITH already running → $URL"
  else
    ( cd "$ZENITH_DIR" && ZENITH_PORT="$PORT" ZENITH_STATE_DIR="$STATE_DIR" \
        nohup python3 "$ZENITH_DIR/server.py" >"$LOG" 2>&1 & echo $! >"$PIDFILE" )
    printf "starting ZENITH"
    for _ in $(seq 1 30); do running && break; printf .; sleep 0.4; done; echo
    running || { echo "failed to start — see $LOG"; return 1; }
    echo "ZENITH online → $URL"
  fi
  open_url
}
stop() {
  [ -f "$PIDFILE" ] && kill "$(cat "$PIDFILE")" 2>/dev/null && rm -f "$PIDFILE" && echo "stopped" || echo "not running"
}

case "${1:-start}" in
  start|open|"") start ;;
  stop)    stop ;;
  restart) stop; sleep 1; start ;;
  status)  running && echo "running → $URL" || echo "stopped" ;;
  logs)    tail -f "$LOG" ;;
  update)  echo "Installed from a .deb — update with: sudo apt-get install --only-upgrade zenith-os" ;;
  autostart)     bash "$ZENITH_DIR/scripts/install-systemd.sh" ;;      # per-user systemd service
  autostart-off) bash "$ZENITH_DIR/scripts/uninstall-systemd.sh" ;;
  *) echo "usage: zenith [start|stop|restart|status|logs|update|autostart|autostart-off]" ;;
esac
