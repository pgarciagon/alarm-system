# -*- mode: python ; coding: utf-8 -*-
#
# alarm_installer.spec — PyInstaller spec for the combined Windows installer.
#
# Produces a single alarm_installer.exe that contains:
#   - The GUI installer (scripts/installer.py)
#   - The server module (server/server.py)
#   - The client module (client/client.py)
#   - All Python dependencies (websockets, keyboard, pygame, tkinter, tomllib)
#   - Bundled assets (assets/alarm.wav, config/*.toml)
#
# Build from the repository root:
#   pyinstaller scripts/alarm_installer.spec
#
# Output: dist/alarm_installer.exe

import os
from pathlib import Path

REPO = Path(SPECPATH).parent  # repo root (one level up from scripts/)

a = Analysis(
    [str(REPO / 'scripts' / 'installer.py')],
    pathex=[str(REPO)],
    binaries=[],
    datas=[
        # Bundled assets
        (str(REPO / 'assets' / 'alarm.wav'),          'assets'),
        (str(REPO / 'assets' / 'alarm.ico'),           'assets'),
        (str(REPO / 'assets' / 'alarm_server.ico'),    'assets'),
        (str(REPO / 'assets' / 'alarm_client.ico'),    'assets'),
        # Default configs (installer writes its own, but include for reference)
        (str(REPO / 'config' / 'server_config.toml'), 'config'),
        (str(REPO / 'config' / 'client_config.toml'), 'config'),
    ],
    hiddenimports=[
        # Server
        'server.server',
        'server.dashboard',
        # Client
        'client.client',
        'client.overlay',
        'client.sound',
        'client.hotkey',
        # Common
        'common.config',
        'common.protocol',
        'common.tray_icon',
        # WebSocket
        'websockets',
        'websockets.asyncio',
        'websockets.asyncio.server',
        'websockets.asyncio.client',
        'websockets.server',
        'websockets.client',
        'websockets.connection',
        'websockets.exceptions',
        'websockets.frames',
        'websockets.http11',
        'websockets.streams',
        'websockets.legacy',
        'websockets.legacy.server',
        'websockets.legacy.client',
        # System tray
        'pystray',
        'pystray._win32',
        'PIL',
        'PIL.Image',
        'PIL.ImageDraw',
        # stdlib / backport
        'tomllib',
        'tomli',
        # Sound
        'pygame',
        'pygame.mixer',
        # Hotkey
        'keyboard',
        # tkinter
        'tkinter',
        'tkinter.ttk',
        'tkinter.messagebox',
        # asyncio internals sometimes missed
        'asyncio',
        'asyncio.events',
        'asyncio.tasks',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='alarm_installer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # No black console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Windows: request UAC elevation so Task Scheduler registration works
    uac_admin=True,
    icon=str(REPO / 'assets' / 'alarm.ico'),
)
