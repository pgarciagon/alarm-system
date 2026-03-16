# -*- mode: python ; coding: utf-8 -*-
#
# alarm_installer_mac.spec — PyInstaller spec for the macOS installer .app
#
# Produces:
#   dist/AlarmInstaller.app   ← drag into DMG
#
# Build from the repository root:
#   /opt/homebrew/opt/python@3.12/bin/pyinstaller scripts/alarm_installer_mac.spec
#
# Then package into DMG:
#   bash scripts/create_dmg.sh

import os
from pathlib import Path

REPO = Path(SPECPATH).parent  # repo root (one level up from scripts/)

a = Analysis(
    [str(REPO / 'scripts' / 'installer_mac.py')],
    pathex=[str(REPO)],
    binaries=[],
    datas=[
        # Bundled assets
        (str(REPO / 'assets' / 'alarm.wav'),          'assets'),
        (str(REPO / 'assets' / 'alarm.ico'),           'assets'),
        (str(REPO / 'assets' / 'alarm_server.ico'),    'assets'),
        (str(REPO / 'assets' / 'alarm_client.ico'),    'assets'),
        # Default configs
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
        'pystray._darwin',
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
    [],
    exclude_binaries=True,
    name='AlarmInstaller',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,       # No terminal window — GUI app
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(REPO / 'assets' / 'alarm.icns'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='AlarmInstaller',
)

app = BUNDLE(
    coll,
    name='AlarmInstaller.app',
    icon=str(REPO / 'assets' / 'alarm.icns'),
    bundle_identifier='com.alarm-system.installer',
    info_plist={
        'CFBundleName':             'AlarmInstaller',
        'CFBundleDisplayName':      'Alarm System Installer',
        'CFBundleShortVersionString': '1.0.0',
        'CFBundleVersion':          '1.0.0',
        'NSHighResolutionCapable':  True,
        'NSHumanReadableCopyright': '© 2025 AlarmSystem',
        # Allow running without translocation issues
        'LSMinimumSystemVersion':   '12.0',
    },
)
