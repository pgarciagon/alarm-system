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

a_installer = Analysis(
    [str(REPO / 'scripts' / 'installer.py')],
    pathex=[str(REPO)],
    binaries=[],
    datas=[
        # Bundled assets
        (str(REPO / 'assets' / 'alarm.wav'),      'assets'),
        # Default configs (installer writes its own, but include for reference)
        (str(REPO / 'config' / 'server_config.toml'), 'config'),
        (str(REPO / 'config' / 'client_config.toml'), 'config'),
    ],
    hiddenimports=[
        # Server dependencies
        'server.server',
        'websockets',
        'websockets.server',
        'websockets.connection',
        'websockets.exceptions',
        'websockets.frames',
        'websockets.http11',
        'websockets.streams',
        # Client dependencies
        'client.client',
        'client.overlay',
        'client.sound',
        'client.hotkey',
        # Common
        'common.config',
        'common.protocol',
        # stdlib / backport
        'tomllib',
        'tomli',
        # Sound
        'pygame',
        'pygame.mixer',
        # Hotkey
        'keyboard',
        # tkinter (usually auto-detected but list explicitly)
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

# Also analyse server and client so their code + imports are included
a_server = Analysis(
    [str(REPO / 'server' / 'server.py')],
    pathex=[str(REPO)],
    binaries=[],
    datas=[],
    hiddenimports=['websockets', 'websockets.server', 'common.config', 'common.protocol', 'tomllib', 'tomli'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

a_client = Analysis(
    [str(REPO / 'client' / 'client.py')],
    pathex=[str(REPO)],
    binaries=[],
    datas=[
        (str(REPO / 'assets' / 'alarm.wav'), 'assets'),
    ],
    hiddenimports=['client.overlay', 'client.sound', 'client.hotkey',
                   'common.config', 'common.protocol',
                   'websockets', 'pygame', 'pygame.mixer', 'keyboard',
                   'tkinter', 'tkinter.ttk', 'tomllib', 'tomli'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

# Merge all three analyses into one bundle
MERGE(
    (a_installer, 'installer',    'installer'),
    (a_server,    'server',       'server/server'),
    (a_client,    'client',       'client/client'),
)

pyz = PYZ(a_installer.pure)

exe = EXE(
    pyz,
    a_installer.scripts,
    a_installer.binaries,
    a_installer.datas,
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
    icon=None,              # Add an .ico path here if you have one
)
