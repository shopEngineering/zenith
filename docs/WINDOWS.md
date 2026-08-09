# Running ZENITH/OS on Windows

ZENITH/OS is cross-platform Python (stdlib only). Everything except the **terminal**
(sessions browser, projects, docs, NexusMind, jobs, loops, swarms, research, voice, statusline
config) works natively on Windows with nothing extra. The terminal needs a real PTY, which on
Windows means one of the two paths below.

## Option A — WSL (recommended)

This is how Claude Code itself is usually run on Windows, and ZENITH runs unchanged (it's Linux
inside WSL) — full feature parity, including terminals that survive restarts.

### Easiest path (install once, then one word)

1. **In an Administrator PowerShell**, run `windows-setup.ps1` (double-click won't elevate — right-click
   PowerShell → Run as administrator, then run the script). It installs WSL + Ubuntu (one reboot) and
   prints the finishing steps.
2. **In Ubuntu** (open "Ubuntu" from the Start menu), run the 3 lines it printed:
   ```bash
   sudo apt update && sudo apt install -y python3 tmux git
   # get Claude Code working so `claude` runs (https://claude.com/claude-code), then:
   git clone https://github.com/shopEngineering/zenith.git && cd zenith-os && ./install.sh
   ```
3. From then on, just type **`zenith`** in Ubuntu — it starts the server and opens ZENITH in your
   Windows browser. `zenith stop | restart | status | update | logs` for the rest.

Terminals, `claude`, tmux persistence, voice, session ATTACH, and the statusline all work exactly
as on macOS/Linux. `localhost` forwards from WSL to Windows automatically.

### If WSL is already set up
Just clone and run the installer inside WSL: `./install.sh`, then `zenith`.

## Option B — Native Windows (experimental)

Runs directly under Windows Python with PowerShell (or cmd / Git Bash) as the shell and native
`claude`. The real-terminal path uses **ConPTY via pywinpty**:

```powershell
# from an elevated-enough PowerShell, with Python 3.10+ on PATH
pip install pywinpty          # required ONLY for the in-OS terminal
python server.py              # → http://127.0.0.1:8777
```

- Without `pywinpty`, the whole app still runs — only opening a terminal returns a clear
  "install pywinpty or use WSL" message. Every other app works.
- The default shell is `powershell.exe` (override by setting the `SHELL` env var, e.g. to a
  Git-Bash `bash.exe`, before launching).
- `claude` is found on `PATH` (`claude.cmd`/`claude.exe`); make sure Claude Code works from a
  plain PowerShell first.
- **tmux persistence** is a no-op on native Windows (there's no tmux) — terminals still survive
  a browser refresh via the server-side buffer, they just don't persist across a server restart.
- The **statusline** install writes a `"<python-exe>" "<script>"` command into
  `%USERPROFILE%\.claude\settings.json` using this interpreter's absolute path, so it runs even
  though `python3` usually isn't on the Windows PATH.

### What's identical on every platform
Sessions browser, projects launcher + recents, files, NexusMind, dashboard, loops, swarms/war
games, research forge, model providers + chat, themes, voice (Whisper install differs — see
below), window management, and the statusline widget config.

### Notes / caveats (native Windows)
- **Whisper voice:** `pip install pywhispercpp` provides local transcription; if it isn't
  installed, voice falls back to the browser Web Speech API (Edge/Chrome) automatically.
- **launchd autostart** (`scripts/install-launchd.sh`) is macOS-only. For Windows autostart, add a
  Task Scheduler entry running `python <path>\server.py` at logon (a `.bat` in `shell:startup` also
  works).
- Native Windows terminal support is written but has had less real-world testing than the
  macOS/Linux/WSL path — if anything misbehaves in a native terminal, WSL is the proven fallback.
