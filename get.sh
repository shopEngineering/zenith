#!/usr/bin/env bash
# ZENITH/OS one-command installer.
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/shopEngineering/zenith/main/get.sh)
#
# (use the bash <(...) form, not "curl | bash", so it can prompt for sudo when a
# package manager needs it.) Detects your OS, installs missing deps, fetches ZENITH,
# and sets up auto-start. The one thing you install yourself: Claude Code (`claude`)
# and its login — that can't be bundled or shared.
set -euo pipefail

REPO="${ZENITH_REPO:-https://github.com/shopEngineering/zenith.git}"
DEST="${ZENITH_HOME:-$HOME/.zenith}"

c(){ printf '\033[%sm%s\033[0m' "$1" "$2"; }
say(){ printf '%s\n' "$*"; }
ok(){ say "  $(c 32 '✓') $*"; }
warn(){ say "  $(c 33 '!') $*"; }
bad(){ say "  $(c 31 '✗') $*"; }
step(){ say ""; say "$(c '36;1' "▸ $*")"; }
have(){ command -v "$1" >/dev/null 2>&1; }

say ""; say "$(c '36;1' '  ZENITH/OS')  one-command install"; say ""

# --- platform + package manager ---
IS_WSL=0; grep -qi microsoft /proc/version 2>/dev/null && IS_WSL=1
case "$(uname -s)" in
  Darwin) PLATFORM=macOS ;;
  Linux)  PLATFORM=$([ "$IS_WSL" = 1 ] && echo WSL || echo Linux) ;;
  *) bad "unsupported OS: $(uname -s) — macOS/Linux/WSL only for now"; exit 1 ;;
esac
say "  platform: $(c 36 "$PLATFORM")"

SUDO=""; [ "$(id -u)" != 0 ] && have sudo && SUDO="sudo"
PM_INSTALL=""
if [ "$PLATFORM" = macOS ]; then
  have brew && PM_INSTALL="brew install"
else
  if   have apt-get; then PM_INSTALL="$SUDO apt-get install -y"
  elif have dnf;     then PM_INSTALL="$SUDO dnf install -y"
  elif have pacman;  then PM_INSTALL="$SUDO pacman -S --noconfirm"
  elif have zypper;  then PM_INSTALL="$SUDO zypper install -y"
  fi
fi

# --- dependencies ---
step "dependencies"
MISS=0
if [ "$PLATFORM" = macOS ] && [ -z "$PM_INSTALL" ]; then
  warn "Homebrew not found — it's how deps get installed on macOS"
  say  "     install it first:  $(c 36 'https://brew.sh')"
  MISS=1
fi

ensure(){  # ensure <cmd> <pkg> [opt]
  local cmd="$1" pkg="$2" opt="${3:-}"
  if have "$cmd"; then ok "$cmd"; return; fi
  if [ -n "$PM_INSTALL" ]; then
    warn "$cmd missing — installing ($pkg)…"
    if $PM_INSTALL "$pkg" >/dev/null 2>&1; then ok "$cmd installed"; return; fi
  fi
  if [ "$opt" = opt ]; then warn "$cmd optional — skipped (voice falls back to browser speech)";
  else bad "$cmd required — install it and re-run:  ${PM_INSTALL:-<your package manager>} $pkg"; MISS=1; fi
}
ensure python3 python3
ensure tmux    tmux
ensure git     git
ensure ffmpeg  ffmpeg opt
[ "$MISS" = 1 ] && { say ""; bad "resolve the above, then re-run."; exit 1; }

if have claude; then ok "claude CLI — $(claude --version 2>&1 | head -1)"; else
  warn "claude CLI not found — ZENITH installs fine, but Claude sessions need it"
  say  "     install + log in:  $(c 36 'https://claude.com/claude-code')"
fi

# --- fetch (or update) ZENITH ---
step "fetching ZENITH → $DEST"
if [ -d "$DEST/.git" ]; then
  ( cd "$DEST" && git pull --ff-only ) >/dev/null 2>&1 && ok "updated" || warn "kept existing checkout (local changes?)"
else
  git clone --depth 1 "$REPO" "$DEST" >/dev/null 2>&1 && ok "cloned" || { bad "clone failed: $REPO"; exit 1; }
fi

# --- set up launcher + auto-start (install.sh handles the OS-specific supervisor) ---
step "installing"
bash "$DEST/install.sh"

say ""; say "  $(c '32;1' 'ready.')  ZENITH is at  $(c 36 'http://localhost:8777')"; say ""
