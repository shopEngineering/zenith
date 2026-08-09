#!/usr/bin/env bash
# Reproducible build of ZENITH.app + ZENITH.dmg (macOS).
#
#   ./packaging/macos/build.sh
#
# Steps: fresh venv → pip install pyinstaller → pyinstaller zenith.spec →
# ad-hoc codesign the .app (so Gatekeeper runs it locally) → hdiutil-package a
# drag-install .dmg with an /Applications symlink. Output: dist/ZENITH.dmg.
#
# The .app/.dmg/dist/build outputs are artifacts and are NOT committed.
set -euo pipefail

# Resolve repo root (two levels up from this script) regardless of CWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
export ZENITH_REPO_ROOT="${REPO_ROOT}"

BUILD_DIR="${REPO_ROOT}/build"
DIST_DIR="${REPO_ROOT}/dist"
VENV_DIR="${BUILD_DIR}/venv"
APP="${DIST_DIR}/ZENITH.app"
DMG="${DIST_DIR}/ZENITH.dmg"
SPEC="${SCRIPT_DIR}/zenith.spec"

echo "==> Repo root: ${REPO_ROOT}"

# 1. Fresh venv (isolated; never touches system/user site-packages).
echo "==> Creating build venv"
rm -rf "${VENV_DIR}"
python3 -m venv "${VENV_DIR}"
# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
python -m pip install --quiet --upgrade pip
python -m pip install --quiet pyinstaller

# 2. Build the .app from the spec. Clean prior outputs first.
echo "==> Running PyInstaller"
rm -rf "${APP}" "${DIST_DIR}/ZENITH" "${BUILD_DIR}/ZENITH"
pyinstaller --noconfirm \
  --distpath "${DIST_DIR}" \
  --workpath "${BUILD_DIR}" \
  "${SPEC}"

deactivate

if [[ ! -d "${APP}" ]]; then
  echo "ERROR: ${APP} was not produced" >&2
  exit 1
fi

# 3. Code sign. Default: ad-hoc (Gatekeeper runs it locally only). Set
#    ZENITH_SIGN_ID to a "Developer ID Application: …" identity to produce a
#    notarizable build with a hardened runtime + secure timestamp (see notarize.sh).
SIGN_ID="${ZENITH_SIGN_ID:--}"
if [[ "${SIGN_ID}" == "-" ]]; then
  echo "==> Ad-hoc code signing"
  codesign --force --deep --sign - "${APP}"
else
  echo "==> Code signing with Developer ID (hardened runtime): ${SIGN_ID}"
  codesign --force --deep --options runtime --timestamp --sign "${SIGN_ID}" "${APP}"
fi
codesign --verify --deep --strict "${APP}" && echo "    signature OK"

# 4. Package a drag-install .dmg with an /Applications symlink (hdiutil, no deps).
echo "==> Building DMG"
STAGE="$(mktemp -d)"
cp -R "${APP}" "${STAGE}/"
ln -s /Applications "${STAGE}/Applications"
rm -f "${DMG}"
# hdiutil can briefly report "Resource busy" while fsevents/Spotlight scans the
# freshly-copied bundle. Retry a few times with a short settle delay.
n=0
until hdiutil create \
        -volname "ZENITH" \
        -srcfolder "${STAGE}" \
        -ov -format UDZO \
        "${DMG}" >/dev/null 2>&1; do
  n=$((n + 1))
  if [[ ${n} -ge 6 ]]; then
    echo "ERROR: hdiutil create failed after ${n} attempts" >&2
    exit 1
  fi
  echo "    hdiutil busy, retrying (${n})..."
  sleep 3
done
rm -rf "${STAGE}"

SIZE="$(du -h "${DMG}" | cut -f1)"
echo "==> Done"
echo "    App: ${APP}"
echo "    DMG: ${DMG} (${SIZE})"
