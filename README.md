# Alarmsystem

A lightweight LAN-based emergency alert system for doctor practices.

Any room PC can press a configurable hotkey (default **Alt+N**) to instantly trigger
a full-screen red alarm overlay with an audible sound on every other PC in the practice.
The central server monitors client health and shows a warning banner when a room goes offline.

The UI is in **German**.

---

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                   Local LAN (same subnet)            │
│                                                      │
│  ┌─────────┐   WebSocket/TCP   ┌──────────────────┐  │
│  │ SERVER  │◄──────────────────│  CLIENT (Zimmer 1)│  │
│  │(always  │                   └──────────────────┘  │
│  │  on)    │   WebSocket/TCP   ┌──────────────────┐  │
│  │         │◄──────────────────│  CLIENT (Zimmer 2)│  │
│  │         │        ...              ...           │  │
│  └─────────┘                                        │
└──────────────────────────────────────────────────────┘
```

- **Server** – runs on the always-on PC; receives alarms and broadcasts them to all clients; tracks client health via heartbeats.
- **Client** – runs on every room PC; registers the global hotkey; shows full-screen alarm overlay; sends heartbeats every 5 s.

---

## Windows 11 Deployment (production)

### Option A — Fully automatic (recommended)

Run **one command** on each PC. It downloads Python, all dependencies, builds and launches the GUI installer automatically.

**Right-click PowerShell → "Als Administrator ausführen"**, then paste:

```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force
irm https://raw.githubusercontent.com/pgarciagon/alarm-system/main/scripts/install_windows.ps1 | iex
```

Or **right-click `install_windows.bat` → "Als Administrator ausführen"** — same result, no PowerShell knowledge needed.

The script:
1. Installs Python 3.12 via `winget` if not present
2. Installs all Python packages (`websockets`, `keyboard`, `pygame`, `pyinstaller`)
3. Downloads the repository from GitHub
4. Builds `alarm_installer.exe` with PyInstaller (~1–2 min)
5. Launches the GUI installer

### Option B — Pre-built exe (USB stick)

Build `alarm_installer.exe` once on a Windows machine, then copy to every PC:

```powershell
# In PowerShell or Git Bash, from the repo root:
pip install pyinstaller websockets keyboard pygame
bash scripts/build_executables.sh
# → dist\alarm_installer.exe   ← copy this to USB
```

Run on each PC: right-click → "Als Administrator ausführen".

### GUI installer workflow

1. **Server PC** → choose **SERVER** → install
2. **Each room PC** → choose **CLIENT** → installer auto-detects server IP → set room name → install
3. Done — Task Scheduler handles auto-start at every boot and logon

The installer:
- Requests UAC elevation automatically
- Probes the LAN to detect a running server or existing installation
- Writes `C:\Program Files\AlarmSystem\server_config.toml` or `client_config.toml`
- Registers a Task Scheduler job with highest privileges, restart-on-failure, logon + boot triggers
- Offers "Jetzt starten" / "Später starten" on completion

### Firewall

Open an inbound rule for **TCP port 9999** on the server PC:
```powershell
netsh advfirewall firewall add rule name="AlarmSystem" dir=in action=allow protocol=TCP localport=9999
```

---

## Configuration

### Server — `config/server_config.toml`

```toml
[server]
host                  = "0.0.0.0"   # bind to all interfaces
port                  = 9999
heartbeat_timeout_sec = 15          # mark client down after this many seconds
# silent_alarm = true  → alarm NOT shown on the triggering room's screen (default)
# silent_alarm = false → alarm shown on ALL screens including the sender
silent_alarm          = true
log_file              = ""          # empty = stdout only
```

### Client — `config/client_config.toml`

```toml
[client]
room_name   = "Zimmer 1"        # displayed in alarm messages
server_ip   = "192.168.1.100"   # IP of the server PC
server_port = 9999
hotkey      = "alt+n"           # any combo supported by the keyboard library
alarm_sound = ""                # empty = use bundled alarm.wav
log_file    = ""
```

If no config file is found at startup a default is written to the working directory.

---

## macOS Simulation (development)

### Requirements

```bash
# Python 3.12+ with Tcl/Tk 9.0 is required on macOS 26 (Tahoe).
# Tcl/Tk 8.6 (Python 3.9) crashes with "Tcl_WaitForEvent: Notifier not initialized".
brew install python@3.12 python-tk@3.12

pip install -r requirements.txt
```

### Multi-room simulation (opens Terminal windows)

```bash
./sim/run_simulation.sh 3   # simulate 3 rooms
# In any client window: type  a  + Enter  to trigger an alarm
```

### Headless simulation (no GUI — CI friendly)

```bash
python sim/simulate.py --rooms 5 --alarm-from "Room 3" --duration 15
```

### Manual start

```bash
# Server
python -m server.server --config config/server_config.toml

# Client (global hotkey — requires Accessibility permission on macOS)
python -m client.client --config config/client_config.toml

# Client (fallback: type 'a' + Enter in terminal)
python -m client.client --config config/client_config.toml --fallback-hotkey
```

---

## Running tests

```bash
pytest tests/ -v
```

24 tests cover protocol encode/decode, alarm broadcast, `client_down`/`client_up` events, heartbeat timeout, config defaults.

---

## Alarm overlay

- Full-screen red background (`#CC0000`) flashing with dark red every 500 ms
- Large white text: `⚠ ALARM — ZIMMER X ⚠`
- Timestamp `Ausgelöst um HH:MM:SS`
- **BESTÄTIGEN (ESC)** button — dismisses the overlay and stops the sound
- Amber corner banner `Alarmsystem NICHT VERFÜGBAR in Zimmer X` when a room goes offline (auto-dismisses after 8 s)
- Green corner banner `Alarmsystem WIEDERHERGESTELLT in Zimmer X` when a room reconnects (auto-dismisses after 5 s)

---

## Repository structure

```
alarm-system/
├── README.md
├── PLAN.md                         Project plan & architecture notes
├── pyproject.toml
├── requirements.txt
├── requirements-dev.txt
│
├── common/
│   ├── protocol.py                 Message types + JSON encode/decode
│   └── config.py                   TOML config loading + defaults
│
├── server/
│   └── server.py                   WebSocket hub + health monitor
│
├── client/
│   ├── client.py                   Main client (asyncio bg thread + tkinter main thread)
│   ├── hotkey.py                   Global hotkey + terminal fallback
│   ├── overlay.py                  Full-screen tkinter alarm overlay (German UI)
│   └── sound.py                    pygame-based looped sound playback
│
├── assets/
│   └── alarm.wav                   Bundled two-tone alarm sound
│
├── config/
│   ├── server_config.toml          Default server config
│   └── client_config.toml          Default client config (edit per room)
│
├── scripts/
│   ├── install_windows.bat         Double-click installer (runs PowerShell script)
│   ├── install_windows.ps1         One-command bootstrap: Python + deps + build + GUI
│   ├── installer.py                Windows GUI installer (server/client chooser)
│   ├── alarm_installer.spec        PyInstaller spec → alarm_installer.exe
│   ├── build_executables.sh        Build script (server + client + installer)
│   ├── install_autostart_windows.py  Direct Task Scheduler registration
│   └── install_autostart_mac.py    macOS launchd plist registration
│
├── sim/
│   ├── run_simulation.sh           macOS multi-window simulation launcher
│   ├── launch.py                   Python launcher (opens Terminal.app windows)
│   └── simulate.py                 Headless asyncio simulation (CI)
│
└── tests/
    ├── test_protocol.py            17 protocol unit tests
    └── test_server.py              7 server integration tests
```

---

## Hotkey notes

| Platform | Requirement |
|----------|-------------|
| Windows  | Run as Administrator (Task Scheduler "highest privileges") for global capture across all apps |
| macOS    | Grant Accessibility permission once: System Settings → Privacy & Security → Accessibility |

---

## macOS Tcl/Tk compatibility

| Python | Tcl/Tk | macOS 26 (Tahoe) |
|--------|--------|-----------------|
| 3.9 (Homebrew) | 8.6 | ❌ Crashes — `Tcl_WaitForEvent: Notifier not initialized` |
| 3.12+ (Homebrew) | 9.0 | ✅ Works |

Always use `brew install python@3.12 python-tk@3.12` on macOS 26+.
