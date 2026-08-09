#!/usr/bin/env bash
# Build a self-contained ZENITH/OS AppImage (bundled Python via PyInstaller).
# Runs natively on a Linux runner matching the target arch (x86_64 or aarch64) —
# e.g. `ubuntu-latest` for x86_64, `ubuntu-24.04-arm` for aarch64. Cross-arch
# builds (e.g. via Docker + QEMU on Apple Silicon) also work but are slow.
# Output: dist/ZENITH-<arch>.AppImage (arch auto-detected from `uname -m`,
# override with ARCH=... env var).
#
# Steps: venv + pip install pyinstaller → build onedir from zenith-linux.spec →
# assemble AppDir (AppRun + .desktop + icon) → mksquashfs + type-2 runtime →
# single-file AppImage.
#
# Container deps: python3 python3-venv python3-pip binutils wget file
#                 ca-certificates squashfs-tools
#
# Containers usually lack FUSE, so verify the produced AppImage with
# `--appimage-extract-and-run`.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
export ZENITH_REPO_ROOT="${REPO_ROOT}"

ARCH="${ARCH:-$(uname -m)}"
DIST="${DIST:-${REPO_ROOT}/dist}"
WORK="${WORK:-/tmp/zenith-appimage-build}"
VENV="${WORK}/venv"
APPDIR="${WORK}/ZENITH.AppDir"
OUT="${DIST}/ZENITH-${ARCH}.AppImage"

echo "==> Repo root: ${REPO_ROOT}   arch: ${ARCH}"
rm -rf "${WORK}"
mkdir -p "${WORK}" "${DIST}"

# 1. venv + PyInstaller.
echo "==> Creating build venv + installing PyInstaller"
python3 -m venv "${VENV}"
# shellcheck disable=SC1091
source "${VENV}/bin/activate"
python -m pip install --quiet --upgrade pip
python -m pip install --quiet pyinstaller

# 2. Build the onedir binary from the Linux spec.
echo "==> Running PyInstaller"
pyinstaller --noconfirm \
  --distpath "${WORK}/dist" \
  --workpath "${WORK}/build" \
  "${SCRIPT_DIR}/zenith-linux.spec"
deactivate

BIN_DIR="${WORK}/dist/zenith"
[ -x "${BIN_DIR}/zenith" ] || { echo "ERROR: PyInstaller binary not found at ${BIN_DIR}/zenith" >&2; exit 1; }

# 3. Assemble the AppDir.
echo "==> Assembling AppDir"
rm -rf "${APPDIR}"
mkdir -p "${APPDIR}/usr/bin"
cp -a "${BIN_DIR}" "${APPDIR}/usr/bin/zenith"          # onedir → usr/bin/zenith/{zenith,_internal,...}
install -m 0755 "${SCRIPT_DIR}/AppRun" "${APPDIR}/AppRun"
cp "${SCRIPT_DIR}/zenith.desktop" "${APPDIR}/zenith.desktop"
python3 "${SCRIPT_DIR}/make-icon.py" "${APPDIR}/zenith.png"
cp "${APPDIR}/zenith.png" "${APPDIR}/.DirIcon"

# 4. Assemble the AppImage as: type-2 runtime + squashfs(AppDir).
#    We do NOT invoke appimagetool's own AppImage: its runtime is a static-pie
#    ELF that QEMU-user cannot exec when cross-building (e.g. an x86_64 build on
#    an arm64 host), which breaks appimagetool. mksquashfs is a plain dynamic
#    binary that runs fine under emulation, and the type-2 runtime is downloadable
#    standalone — concatenating the two is exactly what appimagetool does anyway.
command -v mksquashfs >/dev/null || { echo "ERROR: mksquashfs not found (apt install squashfs-tools)" >&2; exit 1; }

echo "==> Fetching type-2 runtime for ${ARCH}"
RUNTIME="${WORK}/runtime-${ARCH}"
wget -q "https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-${ARCH}" -O "${RUNTIME}"

echo "==> Building squashfs + assembling AppImage"
SQFS="${WORK}/app.sqfs"
rm -f "${SQFS}" "${OUT}"
mksquashfs "${APPDIR}" "${SQFS}" -root-owned -noappend -no-progress -quiet -comp gzip
cat "${RUNTIME}" "${SQFS}" > "${OUT}"
chmod +x "${OUT}"

SIZE="$(du -h "${OUT}" | cut -f1)"
echo "==> Built ${OUT} (${SIZE})"
