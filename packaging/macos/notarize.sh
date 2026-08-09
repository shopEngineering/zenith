#!/usr/bin/env bash
# Sign ZENITH.app with a Developer ID, build the .dmg, notarize it with Apple, and
# staple the ticket — so the app opens cleanly on ANY Mac (no Gatekeeper warning).
#
# Prerequisites (one-time):
#   1. Paid Apple Developer Program membership.
#   2. A "Developer ID Application" certificate in your login keychain
#      (Xcode ▸ Settings ▸ Accounts ▸ Manage Certificates ▸ + ▸ Developer ID Application).
#      NOTE: an "Apple Development" cert is NOT accepted by the notary service.
#   3. Stored notary credentials (app-specific password from appleid.apple.com):
#        xcrun notarytool store-credentials zenith-notary \
#          --apple-id you@example.com --team-id TEAMID --password <app-specific-password>
#
# Run:
#   ZENITH_SIGN_ID="Developer ID Application: Your Name (TEAMID)" ./packaging/macos/notarize.sh
#   # optional: ZENITH_NOTARY_PROFILE=zenith-notary (default)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
DMG="${REPO_ROOT}/dist/ZENITH.dmg"
PROFILE="${ZENITH_NOTARY_PROFILE:-zenith-notary}"

: "${ZENITH_SIGN_ID:?set ZENITH_SIGN_ID to your 'Developer ID Application: …' identity}"

# The identity must exist AND be a Developer ID (Apple Development certs are rejected).
if ! security find-identity -v -p codesigning | grep -Fq "${ZENITH_SIGN_ID}"; then
  echo "ERROR: signing identity not found in keychain: ${ZENITH_SIGN_ID}" >&2
  echo "       available:" >&2
  security find-identity -v -p codesigning >&2
  exit 1
fi
case "${ZENITH_SIGN_ID}" in
  *"Developer ID Application"*) : ;;
  *) echo "ERROR: notarization needs a 'Developer ID Application' cert; got: ${ZENITH_SIGN_ID}" >&2; exit 1 ;;
esac

# Confirm notary credentials are stored (fail early with a clear message otherwise).
if ! xcrun notarytool history --keychain-profile "${PROFILE}" >/dev/null 2>&1; then
  echo "ERROR: no stored notary profile '${PROFILE}'. Create it once with:" >&2
  echo "  xcrun notarytool store-credentials ${PROFILE} --apple-id <id> --team-id <TEAMID> --password <app-specific-pw>" >&2
  exit 1
fi

echo "==> Building + signing with Developer ID (hardened runtime)"
ZENITH_SIGN_ID="${ZENITH_SIGN_ID}" bash "${SCRIPT_DIR}/build.sh"

echo "==> Submitting to Apple notary service (can take a few minutes)…"
xcrun notarytool submit "${DMG}" --keychain-profile "${PROFILE}" --wait

echo "==> Stapling the notarization ticket to the .dmg"
xcrun stapler staple "${DMG}"
xcrun stapler validate "${DMG}" && echo "    stapled OK"

echo "==> Gatekeeper assessment:"
spctl -a -vv -t open --context context:primary-signature "${DMG}" 2>&1 || true
echo "==> Notarized DMG ready: ${DMG}"
