#!/usr/bin/env bash
# ZENITH job adapter for prime-agent: turn ONE ordinary argv prompt into a
# one-shot headless run inside the hardened container on the GPU box.
#
#   pa-job.sh "<prompt>" [extra prime-agent flags...]
#
# Invoked by spawn_job through the agent manifest (defaults/agents.json →
# "bin": "pa-job.sh"), which resolves because the server puts its own bin/ on
# PATH. Contract with ZENITH: argv in, JSONL on stdout, progress+errors on
# stderr, stdin never read (jobs run with stdin=DEVNULL).
#
# THE INJECTION BOUNDARY. The prompt is free text from a web form. It is
# base64-encoded here and only ever appears inside the remote command as
# base64 (alphabet A-Za-z0-9+/= — no quote, space, $, backtick, ; or |), so no
# prompt can become syntax in a shell on the other box. The remote launcher
# decodes it into a variable and hands it to docker as one argv element.
#
# Endpoint config resolves env > <zenith>/data/pa.json > refuse. Nothing about
# the box is baked into this file: data/ is gitignored, so a private machine's
# address never reaches a commit (publish.sh rejects one in anything shipped).
set -euo pipefail

PROMPT="${1:-}"
[ -n "$PROMPT" ] || { echo "pa-job.sh: empty prompt" >&2; exit 2; }
shift

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="${ZENITH_STATE_DIR:-$(dirname "$HERE")}"
CFG="${ZENITH_PA_CONFIG:-$STATE/data/pa.json}"

cfg() {   # cfg <key> <fallback>
  local v=""
  if [ -f "$CFG" ]; then
    v="$(python3 -c 'import json,sys
try:
    print(json.load(open(sys.argv[1])).get(sys.argv[2]) or "")
except Exception:
    print("")' "$CFG" "$1" 2>/dev/null)"
  fi
  printf '%s' "${v:-$2}"
}

HOST="${ZENITH_PA_HOST:-$(cfg host "")}"
LAUNCHER="${ZENITH_PA_JOB:-$(cfg job "")}"
WT_ROOT="${ZENITH_PA_WORKTREE_ROOT:-$(cfg worktree_root "~/scratch/pa-work")}"

if [ -z "$HOST" ] || [ -z "$LAUNCHER" ]; then
  echo "pa-job.sh: prime-agent is not configured — set \"host\" and \"job\" in $CFG (or ZENITH_PA_HOST / ZENITH_PA_JOB)" >&2
  exit 3
fi

# Everything below the prompt is interpolated UNQUOTED into the remote command
# (it has to be: the remote shell is what expands `~`). None of it comes from
# the prompt — the two paths come from pa.json and the trailing argv is the
# manifest's mode_flags — but validate anyway, on the same charset the server's
# pa_worktree() enforces, so neither a fat-fingered pa.json nor a hand-edited
# manifest can become remote syntax.
for v in "$LAUNCHER" "$WT_ROOT"; do
  case "$v" in -*|*..*) echo "pa-job.sh: bad path in config: $v" >&2; exit 3 ;; esac
  printf '%s' "$v" | grep -Eq '^[A-Za-z0-9._~/-]{1,200}$' \
    || { echo "pa-job.sh: bad path in config: $v" >&2; exit 3; }
done
for v in "$@"; do
  printf '%s' "$v" | grep -Eq '^[A-Za-z0-9._=-]{1,80}$' \
    || { echo "pa-job.sh: refusing unsafe agent flag: $v" >&2; exit 3; }
done

# Per-job scratch worktree, built from safe components only (epoch + $RANDOM);
# no part of it comes from user input. The remote launcher creates it.
WT="$WT_ROOT/job-$(date +%s)-$RANDOM"

# BSD base64 wraps at 76 columns and the remote command must be one line.
B64="$(printf '%s' "$PROMPT" | base64 | tr -d '\n')"

echo "pa-job: $HOST  worktree $WT" >&2
exec ssh -n -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=30 \
  "$HOST" -- "exec $LAUNCHER $WT '$B64' $*"
