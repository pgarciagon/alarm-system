# Windows Release Build — v1.5.0 Draft

## Context

A draft GitHub release for **v1.5.0** already exists at:
https://github.com/pgarciagon/alarm-system/releases

The macOS DMG has already been uploaded. Your job is to:
1. Build the three Windows `.exe` binaries using PyInstaller
2. Upload them to the existing draft release
3. Publish the release

---

## Prerequisites

Make sure the following are installed on this Windows machine:

- **Python 3.11 or 3.12** — https://python.org (check "Add to PATH" during install)
- **Git** — https://git-scm.com
- **GitHub CLI** — https://cli.github.com
  After installing, run: `gh auth login` and authenticate with your GitHub account

Install Python dependencies:
```bat
pip install pyinstaller websockets keyboard pystray Pillow pygame
```

---

## Step 1 — Clone the repository

```bat
git clone https://github.com/pgarciagon/alarm-system.git
cd alarm-system
```

Or if already cloned, pull the latest:
```bat
cd alarm-system
git pull origin main
```

---

## Step 2 — Build the Windows binaries

Run each PyInstaller spec from the repository root:

```bat
python -m PyInstaller scripts/alarm_client_win.spec --noconfirm --clean
python -m PyInstaller scripts/alarm_server_win.spec --noconfirm --clean
python -m PyInstaller scripts/alarm_installer_win.spec --noconfirm --clean
```

After building, verify the three files exist:
```bat
dir dist\alarm_client.exe
dir dist\alarm_server.exe
dir dist\alarm_installer.exe
```

---

## Step 3 — Upload the binaries to the draft release

```bat
gh release upload v1.5.0 dist\alarm_client.exe dist\alarm_server.exe dist\alarm_installer.exe --repo pgarciagon/alarm-system
```

Verify all assets are present:
```bat
gh release view v1.5.0 --repo pgarciagon/alarm-system
```

Expected assets:
- `AlarmInstaller.dmg` (macOS — already uploaded)
- `alarm_client.exe`
- `alarm_server.exe`
- `alarm_installer.exe`

---

## Step 4 — Publish the release

Once all four assets are confirmed:
```bat
gh release edit v1.5.0 --draft=false --repo pgarciagon/alarm-system
```

The release is now public at:
https://github.com/pgarciagon/alarm-system/releases/tag/v1.5.0

---

## Troubleshooting

**PyInstaller not found:**
```bat
pip install pyinstaller
```

**`gh` not authenticated:**
```bat
gh auth login
```
Choose "GitHub.com" → "HTTPS" → authenticate via browser.

**Upload fails with "release not found":**
Make sure you are authenticated with the correct GitHub account (`pgarciagon`):
```bat
gh auth status
```

**Binary crashes on startup (missing DLL):**
Check that all dependencies were installed before running PyInstaller:
```bat
pip install websockets keyboard pystray Pillow pygame
```
Then rebuild.
