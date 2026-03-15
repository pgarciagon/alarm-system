#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# build_executables.sh — Build standalone Windows executables with PyInstaller
#
# Run from the repository root on a Windows machine (Git Bash / MSYS2):
#   ./scripts/build_executables.sh
#
# Or on macOS/Linux (cross-build not supported by PyInstaller — use a Windows
# VM or CI runner for actual Windows .exe files):
#   ./scripts/build_executables.sh
#
# Primary output (what to distribute):
#   dist/alarm_installer.exe   ← single file for all 12 PCs
#
# Secondary outputs (standalone binaries, already bundled inside installer):
#   dist/alarm_server.exe
#   dist/alarm_client.exe
#
# Requirements:
#   pip install pyinstaller websockets keyboard pygame tomli
# ---------------------------------------------------------------------------

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

echo "=== Building Alarm System executables ==="
echo "    Repo: $REPO_ROOT"
echo ""

# Detect OS to set separator and extension
if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" || "$OSTYPE" == "win32" ]]; then
    EXT=".exe"
    SEP=";"          # PyInstaller --add-data separator on Windows
    NOCONSOLE="--noconsole"
    UAC="--uac-admin"
else
    EXT=""
    SEP=":"          # Unix separator
    NOCONSOLE=""
    UAC=""
fi

# ---------------------------------------------------------------------------
# Common PyInstaller flags
# ---------------------------------------------------------------------------
COMMON_FLAGS=(
    --onefile
    --noconfirm
    --clean
    --paths "$REPO_ROOT"
    --add-data "assets/alarm.wav${SEP}assets"
    --add-data "assets/alarm.ico${SEP}assets"
    --add-data "assets/alarm_server.ico${SEP}assets"
    --add-data "assets/alarm_client.ico${SEP}assets"
    --add-data "config/server_config.toml${SEP}config"
    --add-data "config/client_config.toml${SEP}config"
)

# ---------------------------------------------------------------------------
# Build server
# ---------------------------------------------------------------------------
echo "--- Building alarm_server ---"
pyinstaller "${COMMON_FLAGS[@]}" \
    ${NOCONSOLE} \
    --name "alarm_server" \
    --icon "assets/alarm_server.ico" \
    --hidden-import websockets \
    --hidden-import websockets.server \
    --hidden-import common.config \
    --hidden-import common.protocol \
    --hidden-import common.tray_icon \
    --hidden-import server.dashboard \
    --hidden-import server.tray_icon \
    --hidden-import pystray \
    --hidden-import PIL \
    --hidden-import tomllib \
    --hidden-import tomli \
    server/server.py

# ---------------------------------------------------------------------------
# Build client
# ---------------------------------------------------------------------------
echo ""
echo "--- Building alarm_client ---"
pyinstaller "${COMMON_FLAGS[@]}" \
    ${NOCONSOLE} \
    --name "alarm_client" \
    --icon "assets/alarm_client.ico" \
    --hidden-import client.overlay \
    --hidden-import client.sound \
    --hidden-import client.hotkey \
    --hidden-import common.config \
    --hidden-import common.protocol \
    --hidden-import common.tray_icon \
    --hidden-import pystray \
    --hidden-import PIL \
    --hidden-import websockets \
    --hidden-import pygame \
    --hidden-import pygame.mixer \
    --hidden-import keyboard \
    --hidden-import tkinter \
    --hidden-import tkinter.ttk \
    --hidden-import tomllib \
    --hidden-import tomli \
    client/client.py

# ---------------------------------------------------------------------------
# Build installer (the main distributable — contains everything)
# ---------------------------------------------------------------------------
echo ""
echo "--- Building alarm_installer (combined installer) ---"
pyinstaller \
    --noconfirm \
    --clean \
    ${UAC} \
    scripts/alarm_installer.spec

echo ""
echo "=== Build complete ==="
echo ""
echo "Primary distributable:"
echo "  dist/alarm_installer${EXT}   ← copy this to every PC and run it"
echo ""
echo "Individual binaries (already bundled inside installer):"
echo "  dist/alarm_server${EXT}"
echo "  dist/alarm_client${EXT}"
echo ""
echo "Deployment:"
echo "  1. Copy dist/alarm_installer${EXT} to a USB stick or shared folder."
echo "  2. On the SERVER PC: run alarm_installer${EXT}, choose 'Server'."
echo "  3. On each ROOM PC:  run alarm_installer${EXT}, choose 'Client'."
echo "     → The installer auto-detects the server IP."
echo "  4. Done — auto-start is configured via Task Scheduler."
