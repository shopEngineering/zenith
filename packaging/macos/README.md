# ZENITH/OS — macOS `.dmg` packaging

Bundles a Python runtime + the ZENITH/OS app into a double-clickable `ZENITH.app`
and ships it in a drag-install `ZENITH.dmg`. A user does **not** need system Python.

## Build

```bash
./packaging/macos/build.sh
```

This is reproducible and self-contained:

1. Creates a throwaway venv under `build/venv` (never touches system/user
   site-packages) and `pip install`s PyInstaller into it.
2. Runs `pyinstaller packaging/macos/zenith.spec`, which bundles:
   - `zenith_app.py` — the launcher (starts the server, waits for the port,
     opens the browser, then blocks).
   - `server.py` — the stdlib server.
   - `static/` via `--add-data static:static` (shipped read-only inside the app).
3. Ad-hoc code-signs the app: `codesign --force --deep --sign - ZENITH.app`.
4. Packages `dist/ZENITH.dmg` with `hdiutil` (built-in, no `create-dmg`
   dependency) including an `/Applications` symlink for drag-install.

**Output:** `dist/ZENITH.dmg` (and `dist/ZENITH.app`). These are build artifacts
and are git-ignored.

## Install / run

Open `dist/ZENITH.dmg`, drag **ZENITH** onto **Applications**, then launch it.
It starts the server on `http://127.0.0.1:8777` and opens your default browser.

Override the port with the `ZENITH_PORT` env var, e.g. to run a second copy
without disturbing an existing instance on 8777:

```bash
ZENITH_PORT=8799 /Applications/ZENITH.app/Contents/MacOS/ZENITH
```

## Frozen-mode paths (how the bundle stays writable)

In a PyInstaller bundle the app resources under `sys._MEIPASS` are **read-only**.
`server.py` handles this, gated strictly on `sys.frozen`:

- **Bundled, read-only** (`sys._MEIPASS`): `static/`.
- **Writable, per-user** (`~/.zenith/`): `data/` (runtime state, provider config,
  captured audio, etc.) and `scripts/statusline.py` (materialised at runtime).

When run from source (not frozen), paths are unchanged — everything anchors at the
repo dir next to `server.py`.

## What the user still needs installed themselves

The app bundles Python + ZENITH/OS only. It does **not** bundle:

- **`claude`** (Claude Code CLI) — **required** for sessions/terminals. The user
  installs and authenticates it themselves. The app resolves it from
  Homebrew's path (`/opt/homebrew/bin`) or `PATH`.
- **`tmux`** — **strongly recommended** (`brew install tmux`). It backs terminal-session
  persistence. Without it every terminal is a direct child of the server process, so
  **restarting or updating ZENITH destroys every open terminal and the agent sessions
  running inside them** — transcripts survive on disk, the live sessions do not.
  Everything else still works. Settings → System offers a one-click install.

## Architecture

`build.sh` builds for the **host architecture only**. On Apple Silicon that yields
an **arm64** `.dmg` (won't run on Intel Macs). For a universal build you'd install a
universal Python and add `target_arch='universal2'` to the spec — not done here.

## Signing / distribution

By default the app is **ad-hoc signed** (`codesign --sign -`) and **not notarized** —
fine to run on the machine that built it, but other Macs will trip Gatekeeper. To open
an un-notarized build on another Mac, the user does it once:

```bash
xattr -dr com.apple.quarantine /Applications/ZENITH.app   # then launch normally
# …or: right-click ▸ Open, or System Settings ▸ Privacy & Security ▸ "Open Anyway"
```

### Notarize (opens cleanly on any Mac)

Once you have a paid Apple Developer Program membership + a **Developer ID Application**
cert (an *Apple Development* cert is **not** accepted), `notarize.sh` does the whole
flow — Developer ID sign (hardened runtime + timestamp) → build → `notarytool submit
--wait` → `stapler staple`:

```bash
# one-time: store notary creds (app-specific password from appleid.apple.com)
xcrun notarytool store-credentials zenith-notary \
  --apple-id you@example.com --team-id TEAMID --password <app-specific-password>

ZENITH_SIGN_ID="Developer ID Application: Your Name (TEAMID)" ./packaging/macos/notarize.sh
```

If notarization rejects nested code (PyInstaller ships embedded dylibs), sign the
components individually instead of relying on `--deep`.
