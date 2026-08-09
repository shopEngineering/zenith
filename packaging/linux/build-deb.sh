#!/usr/bin/env bash
# Build the ZENITH/OS .deb (source-based — depends on the system python3, does
# NOT bundle an interpreter). MUST run inside an Ubuntu/Debian container: it uses
# dpkg-deb, which macOS lacks. Output: dist/zenith-os_<version>_all.deb
#
#   docker run --rm -v "$PWD":/app:ro -v "$PWD/dist":/out ubuntu:22.04 \
#       bash -c 'apt-get update && apt-get install -y dpkg-dev &&
#                DIST=/out bash /app/packaging/linux/build-deb.sh'
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
VERSION="$(tr -d '[:space:]' < "${REPO_ROOT}/VERSION")"   # single source of truth
PKG="zenith-os"
ARCH="all"
DIST="${DIST:-${REPO_ROOT}/dist}"
STAGE="$(mktemp -d)"
DEB="${DIST}/${PKG}_${VERSION}_${ARCH}.deb"

echo "==> Repo root: ${REPO_ROOT}"
echo "==> Staging:   ${STAGE}"

# --- filesystem layout the package installs ---
#   /opt/zenith/{server.py,static/,scripts/}   (read-only app)
#   /usr/bin/zenith                            (launcher)
install -d "${STAGE}/opt/zenith" "${STAGE}/opt/zenith/scripts" "${STAGE}/usr/bin" "${STAGE}/DEBIAN"

# App payload. Copy static/ and server.py; NOT data/ (user-specific, runtime).
cp "${REPO_ROOT}/server.py" "${STAGE}/opt/zenith/server.py"
cp -R "${REPO_ROOT}/static"  "${STAGE}/opt/zenith/static"
cp -R "${REPO_ROOT}/defaults" "${STAGE}/opt/zenith/defaults"   # shipped config defaults
cp "${REPO_ROOT}/VERSION"    "${STAGE}/opt/zenith/VERSION"   # read by server for the update check

# Scripts the launcher needs at runtime: the package-specific systemd installer
# (writes state to ~/.zenith) + the shared uninstaller.
cp "${SCRIPT_DIR}/install-systemd.sh"           "${STAGE}/opt/zenith/scripts/install-systemd.sh"
cp "${REPO_ROOT}/scripts/uninstall-systemd.sh"  "${STAGE}/opt/zenith/scripts/uninstall-systemd.sh"

# The launcher → /usr/bin/zenith
cp "${SCRIPT_DIR}/zenith-launcher.sh" "${STAGE}/usr/bin/zenith"

# --- control files (stamp Version from the VERSION file) ---
cp "${SCRIPT_DIR}/control"  "${STAGE}/DEBIAN/control"
sed -i "s/^Version: .*/Version: ${VERSION}/" "${STAGE}/DEBIAN/control"
cp "${SCRIPT_DIR}/postinst" "${STAGE}/DEBIAN/postinst"

# --- permissions ---
find "${STAGE}/opt/zenith" -type d -exec chmod 0755 {} +
find "${STAGE}/opt/zenith" -type f -exec chmod 0644 {} +
chmod 0755 "${STAGE}/opt/zenith/scripts/"*.sh
chmod 0755 "${STAGE}/usr/bin/zenith"
chmod 0755 "${STAGE}/DEBIAN/postinst"
chmod 0755 "${STAGE}/DEBIAN"

# Root-owns everything (dpkg-deb --root-owner-group so we don't need fakeroot).
mkdir -p "${DIST}"
rm -f "${DEB}"
dpkg-deb --root-owner-group --build "${STAGE}" "${DEB}"

rm -rf "${STAGE}"

SIZE="$(du -h "${DEB}" | cut -f1)"
echo "==> Built ${DEB} (${SIZE})"
dpkg-deb --info "${DEB}" | sed 's/^/    /'
echo "==> Contents:"
dpkg-deb --contents "${DEB}" | sed 's/^/    /'
