#!/bin/bash
# ZENITH launcher: sources the Claude Code auth token (a 0600 file) so the
# `claude -p` jobs ZENITH spawns can authenticate. Needed because ZENITH may run
# as a LaunchDaemon (session 0), which cannot read the GUI login keychain where
# `claude /login` stores credentials. Token comes from `claude setup-token` and
# lives in ~/.config/zenith/claude-auth.env (CLAUDE_CODE_OAUTH_TOKEN=...).
#
# The env file is optional: absent (e.g. a fresh dev box using the GUI keychain),
# this is a no-op and server.py starts normally.
set -a
[ -f "$HOME/.config/zenith/claude-auth.env" ] && . "$HOME/.config/zenith/claude-auth.env"
# zenith.env carries host-process config the daemon needs — e.g. ZENITH_NM_TOKEN
# / ZENITH_NM_API so the Watchers panel can reach NexusMind's owner-authed API
# (ZENITH's calls arrive as the docker-gateway IP, not loopback, so a bearer
# token is required). Optional; absent on a box without the console panels.
[ -f "$HOME/.config/zenith/zenith.env" ] && . "$HOME/.config/zenith/zenith.env"
set +a
DIR="$(cd "$(dirname "$0")" && pwd)"
exec /usr/bin/python3 "$DIR/server.py"
