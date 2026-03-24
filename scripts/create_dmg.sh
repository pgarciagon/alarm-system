#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# create_dmg.sh — Package AlarmInstaller.app into a distributable .dmg
#
# Run from the repository root AFTER building the .app:
#   /opt/homebrew/opt/python@3.12/bin/pyinstaller scripts/alarm_installer_mac.spec
#   bash scripts/create_dmg.sh
#
# Output: dist/AlarmInstaller.dmg
#
# Requirements: macOS hdiutil (built-in), no extra tools needed.
# ---------------------------------------------------------------------------

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="AlarmInstaller"
APP_SRC="${REPO_ROOT}/dist/${APP_NAME}.app"
DMG_OUT="${REPO_ROOT}/dist/${APP_NAME}.dmg"
DMG_TMP="/tmp/${APP_NAME}_dmg_$$"
VOLUME_NAME="Alarm System Installer"

echo "=== Creating ${APP_NAME}.dmg ==="
echo "    App:    ${APP_SRC}"
echo "    Output: ${DMG_OUT}"
echo ""

# Verify .app exists
if [ ! -d "${APP_SRC}" ]; then
    echo "ERROR: ${APP_SRC} not found."
    echo "       Build it first:"
    echo "       /opt/homebrew/opt/python@3.12/bin/pyinstaller scripts/alarm_installer_mac.spec"
    exit 1
fi

# Clean up previous DMG
[ -f "${DMG_OUT}" ] && rm -f "${DMG_OUT}"

# Create a staging folder
rm -rf "${DMG_TMP}"
mkdir -p "${DMG_TMP}"

# Copy .app into staging folder
echo "--- Copying .app to staging area ---"
cp -R "${APP_SRC}" "${DMG_TMP}/"

# Create a symlink to /Applications for drag-and-drop install
ln -s /Applications "${DMG_TMP}/Applications"

# Write a background README in the DMG
cat > "${DMG_TMP}/LIES MICH.txt" <<'EOF'
Alarmsystem — macOS Installation
=================================

1. Ziehen Sie "AlarmInstaller.app" in den "Applications"-Ordner.
2. Öffnen Sie "AlarmInstaller.app".
3. Wählen Sie SERVER oder CLIENT.
4. Folgen Sie den Anweisungen auf dem Bildschirm.

Hinweis: macOS fragt ggf. nach der Zugänglichkeits-Berechtigung für
den globalen Alarm-Hotkey. Diese Berechtigung finden Sie unter:
Systemeinstellungen → Datenschutz & Sicherheit → Bedienungshilfen

Support: github.com/pgarciagon/alarm-system
EOF

# Calculate size (app + 30% overhead for HFS+ metadata and staging extras)
APP_SIZE_KB=$(du -sk "${APP_SRC}" | awk '{print $1}')
DMG_SIZE_KB=$(( APP_SIZE_KB * 13 / 10 + 8192 ))   # 130% + 8 MB headroom

echo "--- Creating DMG (size: ~${DMG_SIZE_KB} KB) ---"
hdiutil create \
    -srcfolder "${DMG_TMP}" \
    -volname "${VOLUME_NAME}" \
    -fs HFS+ \
    -format UDRW \
    -size "${DMG_SIZE_KB}k" \
    "/tmp/${APP_NAME}_rw_$$.dmg"

echo "--- Converting to compressed read-only DMG ---"
hdiutil convert \
    "/tmp/${APP_NAME}_rw_$$.dmg" \
    -format UDZO \
    -imagekey zlib-level=9 \
    -o "${DMG_OUT}"

# Cleanup
rm -f "/tmp/${APP_NAME}_rw_$$.dmg"
rm -rf "${DMG_TMP}"

echo ""
echo "=== Done ==="
echo ""
echo "Distributable: ${DMG_OUT}"
echo ""
echo "Deployment:"
echo "  1. Copy dist/AlarmInstaller.dmg to a USB stick or share it."
echo "  2. On each Mac: open the DMG, drag AlarmInstaller.app → Applications."
echo "  3. Open AlarmInstaller.app → choose SERVER or CLIENT → install."
echo ""
