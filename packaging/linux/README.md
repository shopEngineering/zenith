# ZENITH/OS — Linux packaging

Two Linux artifacts, both built and verified **inside Ubuntu/Debian containers**
(the repo's dev host is macOS, which has neither `dpkg-deb` nor a Linux Python to
freeze). `dist/` is git-ignored — the artifacts are never committed.

## 1. `.deb` (primary — source-based, uses the system Python)

Depends on `python3 (>= 3.9)`; does **not** bundle an interpreter. Installs:

- `/opt/zenith/` — `server.py`, `static/`, `scripts/` (read-only app).
- `/usr/bin/zenith` — launcher (`start|stop|restart|status|logs|autostart|autostart-off`).

All writable runtime state (pidfile, logs, `data/`, statusline script) goes to the
invoking user's `~/.zenith` via the `ZENITH_STATE_DIR` env var the launcher exports —
nothing is written under the root-owned `/opt/zenith`.

### Build

```bash
docker run --rm -v "$PWD":/app:ro -v "$PWD/dist":/out ubuntu:22.04 bash -c '
  apt-get update && apt-get install -y dpkg-dev &&
  DIST=/out bash /app/packaging/linux/build-deb.sh'
```

Output: `dist/zenith-os_0.1.0_all.deb` (~113 KB).

### Install (on a real Debian/Ubuntu box)

```bash
sudo apt-get install -y ./zenith-os_0.1.0_all.deb   # pulls python3
zenith            # start + open in browser
zenith autostart  # keep running across reboots (per-user systemd --user service)
```

`postinst` does **not** enable any system service — ZENITH must run as the desktop
user (it reads that user's Claude Code data). Autostart is opt-in per user.

## 2. `.AppImage` (secondary — self-contained, bundles Python)

A PyInstaller onedir frozen with `zenith-linux.spec` (reuses the OS-agnostic
`packaging/macos/zenith_app.py` launcher), wrapped in an AppDir + the AppImage
type-2 runtime. Uses server.py's existing frozen-mode paths (`sys._MEIPASS` for
statics, `~/.zenith` for writable state). Needs no system Python at all.

### Build

```bash
# ARCH=x86_64 for Intel/AMD desktops; ARCH=aarch64 for arm64.
docker run --rm --platform linux/amd64 \
  -v "$PWD":/app:ro -v "$PWD/dist":/out ubuntu:22.04 bash -c '
  apt-get update &&
  apt-get install -y python3 python3-venv python3-pip binutils wget file ca-certificates squashfs-tools &&
  ARCH=x86_64 DIST=/out bash /app/packaging/linux/build-appimage.sh'
```

Output: `dist/ZENITH-x86_64.AppImage` (~8 MB).

The AppImage is assembled as `runtime-<arch>` + `mksquashfs(AppDir)` rather than by
invoking `appimagetool` — appimagetool ships as its own AppImage whose static-pie
runtime QEMU-user cannot exec when cross-building (e.g. an x86_64 AppImage on an
arm64 host). `mksquashfs` runs fine under emulation, so this method builds any arch
from any host.

### Run

```bash
./ZENITH-x86_64.AppImage                          # normal desktop (FUSE present)
./ZENITH-x86_64.AppImage --appimage-extract-and-run   # no-FUSE hosts / containers
```

## What a Linux user still needs

- **Claude Code** — the `claude` CLI must be installed and logged in separately
  (https://claude.com/claude-code). ZENITH drives it; it does not ship it.
- **tmux** (**strongly recommended**) — backs persistent terminals. Without it every
  terminal is a child of the server process, so **restarting ZENITH kills all of them**
  along with the agent sessions inside. The `.deb` lists it under `Recommends` (apt
  installs those by default; `--no-install-recommends` skips it). AppImage users should
  install it themselves, or use Settings → System → INSTALL.
