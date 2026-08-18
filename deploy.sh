#!/usr/bin/env bash
# Deploy this working tree to the directory the ZENITH daemon actually runs from.
#
#   ./deploy.sh                  sync + verify + restart
#   ./deploy.sh --no-restart     sync + verify only (enough for frontend-only changes)
#   ./deploy.sh --dry-run        show what would change, touch nothing
#   ZENITH_HOME=/path ./deploy.sh
#
# WHY THIS EXISTS. The daemon does not run from the repo. On macOS,
# /Library/LaunchDaemons/com.zenith.os.plist runs $ZENITH_HOME/launch-zenith.sh with
# KeepAlive=true, and $ZENITH_HOME is a PLAIN DIRECTORY — not a checkout. `git pull`
# cannot reach it, so committing alone changes nothing about what is running, and the
# two silently drift (an endpoint 404s while the code for it sits in git).
#
# This is the DEV-BOX path only. A normal install from get.sh clones into ~/.zenith,
# which IS a checkout, and updates with `zenith update` (git pull + restart).
#
# Safety: the deployed copy is compiled and self-checked BEFORE the server is
# restarted, and server.py is restored from its backup if either fails — a bad deploy
# should never take the daemon down, because KeepAlive would then respawn it into a
# crash loop.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="${ZENITH_HOME:-$HOME/zenith-src}"
PORT="${ZENITH_PORT:-8777}"
PY="${ZENITH_PYTHON:-/usr/bin/python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python3

RESTART=1; DRYRUN=0
for a in "$@"; do
  case "$a" in
    --no-restart) RESTART=0 ;;
    --dry-run)    DRYRUN=1 ;;
    -h|--help)    sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown flag: $a (try --help)" >&2; exit 2 ;;
  esac
done

# Runtime payload only. data/ is deliberately absent: it holds config.json,
# gpu_nodes.json, deploy.env and the event store — machine state the repo must
# never overwrite.
#
# This list is EXHAUSTIVE: a new top-level module must be added here, or the
# deployed server.py imports something that isn't on the target. The self-check
# catches it and rolls the whole payload back, so nothing breaks — but the deploy
# fails. zenith_cases.py cost exactly one such round-trip on 2026-08-14.
FILES=(server.py zenith_agents.py zenith_store.py zenith_cases.py launch-zenith.sh VERSION)
DIRS=(static scripts defaults bin)

c(){ printf '\033[%sm%s\033[0m' "$1" "$2"; }
ok(){   printf '  %s %s\n' "$(c 32 '✓')" "$*"; }
warn(){ printf '  %s %s\n' "$(c 33 '!')" "$*"; }
die(){  printf '  %s %s\n' "$(c 31 '✗')" "$*" >&2; exit 1; }
step(){ printf '\n%s\n' "$(c '36;1' "▸ $*")"; }

[ "$SRC" != "$DEST" ] || die "ZENITH_HOME is the repo itself — nothing to deploy"
[ -d "$DEST" ] || die "no such directory: $DEST (set ZENITH_HOME)"
[ -f "$DEST/server.py" ] || die "$DEST does not look like a ZENITH install (no server.py)"

# --- what would change -------------------------------------------------------
step "changes"
CHANGED=()
for f in "${FILES[@]}"; do
  [ -f "$SRC/$f" ] || continue
  cmp -s "$SRC/$f" "$DEST/$f" || CHANGED+=("$f")
done
for d in "${DIRS[@]}"; do
  [ -d "$SRC/$d" ] || continue
  while IFS= read -r f; do
    rel="${f#"$SRC"/}"
    [ "$rel" = "scripts/statusline.py" ] && continue      # generated; see the copy loop
    cmp -s "$f" "$DEST/$rel" 2>/dev/null || CHANGED+=("$rel")
  done < <(find "$SRC/$d" -type f ! -name '*.pyc' ! -name '.DS_Store')
done
if [ ${#CHANGED[@]} -eq 0 ]; then
  ok "already in sync — nothing to copy"
  [ "$RESTART" = 1 ] || exit 0
else
  for f in "${CHANGED[@]}"; do printf '    %s\n' "$f"; done
  ok "${#CHANGED[@]} file(s) to deploy"
fi
# Deploying uncommitted work is normal (that IS how you test it), but on a box where
# several agent sessions share the checkout it is easy to ship someone ELSE's
# half-finished edit to the live daemon. So: name what is uncommitted, don't block.
if git -C "$SRC" rev-parse --git-dir >/dev/null 2>&1; then
  DIRTY="$(git -C "$SRC" status --porcelain -- "${FILES[@]}" "${DIRS[@]}" 2>/dev/null || true)"
  if [ -n "$DIRTY" ]; then
    warn "deploying UNCOMMITTED changes:"
    printf '%s\n' "$DIRTY" | sed 's/^/      /'
    warn "if any of that is not yours, commit or stash before deploying"
  fi
fi

[ "$DRYRUN" = 0 ] || { warn "dry run — stopping here"; exit 0; }

# --- verify the SOURCE first, so a typo never reaches the daemon -------------
step "verify source"
"$PY" -m py_compile "$SRC/server.py" || die "server.py does not compile — fix before deploying"
ok "compiles"

# --- copy --------------------------------------------------------------------
step "deploy → $DEST"
STAMP="$(date +%Y%m%d-%H%M%S)"
BAK="$DEST/server.py.bak-$STAMP-predeploy"
cp -p "$DEST/server.py" "$BAK"

# Snapshot EVERYTHING about to be overwritten, not just server.py. $DEST is a
# plain directory, not a checkout, so an edit made there has no other copy
# anywhere — and static/ was previously replaced with no backup at all. That
# asymmetry cost a night's frontend work: a deploy silently overwrote
# static/app.js, static/apps.js and static/index.html, and there was nothing to
# restore from. Cheap insurance (~1.5 MB a deploy) against an unrecoverable loss.
SNAP="$DEST/.predeploy/$STAMP"
mkdir -p "$SNAP"
SNAP_N=0
for f in "${FILES[@]}"; do
  [ -f "$DEST/$f" ] || continue
  mkdir -p "$SNAP/$(dirname "$f")"
  cp -p "$DEST/$f" "$SNAP/$f"; SNAP_N=$((SNAP_N + 1))
done
for d in "${DIRS[@]}"; do
  [ -d "$DEST/$d" ] || continue
  while IFS= read -r f; do
    rel="${f#"$DEST"/}"
    mkdir -p "$SNAP/$(dirname "$rel")"
    cp -p "$f" "$SNAP/$rel"; SNAP_N=$((SNAP_N + 1))
  done < <(find "$DEST/$d" -type f ! -name '*.pyc' ! -name '.DS_Store')
done
ok "backup → $(basename "$BAK") + $SNAP_N file(s) in .predeploy/$STAMP"

# Keep the last 10 snapshots. Unpruned these grow without bound, and the useful
# one is always recent — anything older is in git.
while IFS= read -r old; do rm -rf "$old"; done < <(
  find "$DEST/.predeploy" -mindepth 1 -maxdepth 1 -type d | sort -r | tail -n +11)

for f in "${FILES[@]}"; do
  [ -f "$SRC/$f" ] && cp -p "$SRC/$f" "$DEST/$f"
done
for d in "${DIRS[@]}"; do
  [ -d "$SRC/$d" ] || continue
  mkdir -p "$DEST/$d"
  while IFS= read -r f; do
    rel="${f#"$SRC"/}"
    # scripts/statusline.py is GENERATED by the server from STATUSLINE_SCRIPT_SRC and is
    # gitignored for that reason. Copying it pushes whatever stale copy sits in the working
    # tree OVER the fresh one the server just wrote — which is how a months-old statusline
    # survived several restarts and silently dropped every transcript-derived widget.
    [ "$rel" = "scripts/statusline.py" ] && continue
    mkdir -p "$DEST/$(dirname "$rel")"
    cp -p "$f" "$DEST/$rel"
  done < <(find "$SRC/$d" -type f ! -name '*.pyc' ! -name '.DS_Store')
done
ok "copied"

# --- verify the DEPLOYED copy, roll back if it is broken ---------------------
step "verify deployed copy"
if ! ( cd "$DEST" && "$PY" -m py_compile server.py && "$PY" server.py check ); then
  # Roll the WHOLE payload back, not just server.py. Restoring Python while
  # leaving the new static assets in place leaves the daemon serving a frontend
  # its backend does not implement — a combination neither half was tested in.
  # (Files the deploy ADDED are left behind; nothing references them once the
  # rest is reverted.)
  while IFS= read -r f; do
    rel="${f#"$SNAP"/}"
    mkdir -p "$DEST/$(dirname "$rel")"
    cp -p "$f" "$DEST/$rel"
  done < <(find "$SNAP" -type f)
  die "deployed server.py failed its checks — the whole payload was rolled back
     from .predeploy/$STAMP (server.py also kept at $(basename "$BAK"))"
fi
ok "compiles + self-check passes"

[ "$RESTART" = 1 ] || { step "done"; ok "not restarting (--no-restart) — reload the browser for frontend changes"; exit 0; }

# --- restart -----------------------------------------------------------------
# macOS: the LaunchDaemon has KeepAlive=true, so killing it IS the restart.
# Linux: prefer the systemd user unit if one is installed.
step "restart"
if command -v systemctl >/dev/null 2>&1 && systemctl --user status zenith >/dev/null 2>&1; then
  systemctl --user restart zenith && ok "systemctl --user restart zenith"
else
  PIDS="$(pgrep -f "$DEST/server.py" || true)"
  if [ -n "$PIDS" ]; then
    # shellcheck disable=SC2086
    kill $PIDS && ok "signalled $PIDS (KeepAlive respawns it)"
  else
    warn "server was not running — start it with: launchctl kickstart -k system/com.zenith.os"
  fi
fi

step "health"
for i in $(seq 1 40); do
  if "$PY" - "$PORT" <<'PYEOF' 2>/dev/null
import sys, urllib.request
urllib.request.urlopen("http://127.0.0.1:%s/api/overview" % sys.argv[1], timeout=3).read()
PYEOF
  then
    ok "responding on :$PORT after ${i}s"
    printf '\n  %s  http://127.0.0.1:%s   %s\n\n' "$(c '32;1' 'ready.')" "$PORT" \
      "$(c 33 '(hard-reload the browser to pick up frontend changes)')"
    exit 0
  fi
  sleep 1
done
die "server did not answer on :$PORT within 40s — check /Users/\$USER/zenith.log"
