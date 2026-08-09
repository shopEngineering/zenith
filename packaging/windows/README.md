# ZENITH/OS — Windows packaging

Produces a native Windows installer (`ZENITH-Setup.exe`) and a portable single-file
`ZENITH-windows-x64.exe`, both with a **Python runtime bundled in** (users need no
Python). PyInstaller can't cross-compile, so this builds on a real Windows machine —
in practice, **GitHub Actions** (`.github/workflows/windows-build.yml`).

## How it builds (CI)
On a `windows-latest` runner:
1. `pip install pyinstaller pywinpty`
2. `pyinstaller packaging/windows/zenith-windows.spec` → one-file `dist/ZENITH.exe`
   (bundles the launcher + `server.py` + `static/`, and collects `pywinpty` so the
   in-app terminals work; `server.py`'s `sys.frozen` paths write state to
   `%USERPROFILE%\.zenith`).
3. **Smoke test** — launches the exe with `ZENITH_NO_BROWSER=1` on port 8799 and
   requires `GET /api/overview` → 200 before continuing.
4. **Inno Setup** (`zenith.iss`) wraps it into `ZENITH-Setup.exe` (per-user, no admin).
5. Uploads both as run artifacts, and to a release when triggered with a tag.

## Trigger
- **Manually:** Actions ▸ *windows-build* ▸ Run workflow (optionally a release tag to
  attach to), or `gh workflow run windows-build.yml -f tag=v0.1.0`.
- **Automatically:** whenever a GitHub release is *published*.

## Install (end user)
- **Installer:** run `ZENITH-Setup.exe` → installs per-user, Start Menu shortcut,
  optional desktop icon, optional **"start at login"** (a Startup-folder shortcut —
  the Windows equivalent of launchd/systemd auto-start).
- **Portable:** just run `ZENITH-windows-x64.exe`.

Both open `http://127.0.0.1:8777`.

## Still needed by the user
- **[Claude Code](https://claude.com/claude-code)** (`claude`) on `PATH`, logged in — required for sessions.
- **tmux** is not used on native Windows; terminals use ConPTY via the bundled `pywinpty`.

## Limitations
- **x64 only** (matches the runner). ARM64 Windows is future work.
- **Not code-signed** — SmartScreen will show "Windows protected your PC" on first run
  (click **More info ▸ Run anyway**). Authenticode signing with an EV/OV cert is the
  future step, analogous to macOS notarization.
- Auto-start uses a Startup shortcut (starts at login); it does not respawn on crash.
- The `.exe`/installer are built by CI and are **not** committed (git-ignored).
