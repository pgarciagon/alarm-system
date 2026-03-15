#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# build_executables.sh — Build standalone executables with PyInstaller
#
# Run from the repository root:
#   ./scripts/build_executables.sh
#
# Outputs:
#   dist/alarm_server    (or alarm_server.exe on Windows)
#   dist/alarm_client    (or alarm_client.exe on Windows)
#
# Requirements:
#   pip install pyinstaller
# ---------------------------------------------------------------------------

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

echo "=== Building Alarm System executables ==="

# Detect OS to set executable extension
if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" || "$OSTYPE" == "win32" ]]; then
    EXT=".exe"
else
    EXT=""
fi

# ---------------------------------------------------------------------------
# Common PyInstaller flags
# ---------------------------------------------------------------------------
COMMON_FLAGS=(
    --onefile
    --noconfirm
    --clean
    # Bundle the alarm sound asset
    --add-data "assets/alarm.wav:assets"
    # Bundle default configs so the exe can generate them on first run
    --add-data "config/server_config.toml:config"
    --add-data "config/client_config.toml:config"
)

# Windows: run without a console window (no black popup)
if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" ]]; then
    COMMON_FLAGS+=(--noconsole)
fi

# ---------------------------------------------------------------------------
# Build server
# ---------------------------------------------------------------------------
echo ""
echo "--- Building server ---"
pyinstaller "${COMMON_FLAGS[@]}" \
    --name "alarm_server" \
    --paths "$REPO_ROOT" \
    server/server.py

# ---------------------------------------------------------------------------
# Build client
# ---------------------------------------------------------------------------
echo ""
echo "--- Building client ---"
pyinstaller "${COMMON_FLAGS[@]}" \
    --name "alarm_client" \
    --paths "$REPO_ROOT" \
    client/client.py

echo ""
echo "=== Build complete ==="
echo "Server: dist/alarm_server${EXT}"
echo "Client: dist/alarm_client${EXT}"
echo ""
echo "Deployment checklist:"
echo "  1. Copy dist/alarm_server${EXT} + config/server_config.toml to the server PC."
echo "  2. Run: alarm_server${EXT} --install   (once, as admin)"
echo "  3. For each room PC: copy dist/alarm_client${EXT} + config/client_config.toml"
echo "     (edit room_name and server_ip in client_config.toml first)."
echo "  4. Run: alarm_client${EXT} --install   (once, as admin)"
