# Alarm System

A lightweight LAN-based emergency alert system for doctor practices.

Any room PC can press a configurable hotkey (default **Alt+N**) to instantly trigger
a full-screen red alarm overlay with an audible sound on **every other PC** in the
practice. The central server also monitors client health and displays a warning
banner when a room's alert system goes offline.

---

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                   Local LAN (same subnet)            │
│                                                      │
│  ┌─────────┐   WebSocket/TCP   ┌──────────────────┐  │
│  │ SERVER  │◄──────────────────│  CLIENT (Room 1) │  │
│  │(always  │                   └──────────────────┘  │
│  │  on)    │   WebSocket/TCP   ┌──────────────────┐  │
│  │         │◄──────────────────│  CLIENT (Room 2) │  │
│  │         │        ...              ...           │  │
│  └─────────┘                                        │
└──────────────────────────────────────────────────────┘
```

- **Server** – runs on the always-on PC; receives alarms and broadcasts them to all clients; tracks client health.
- **Client** – runs on every room PC; registers the global hotkey; shows full-screen alarm; sends heartbeats.

---

## Requirements

- Python 3.9+ (3.11+ recommended; tkinter required for the client overlay)
- Windows 11 in production; macOS for development/simulation

```bash
pip install -r requirements.txt
# dev extras (tests + packaging):
pip install -r requirements-dev.txt
```

---

## Configuration

### Server — `config/server_config.toml`

```toml
[server]
host                  = "0.0.0.0"   # bind to all interfaces
port                  = 9999
heartbeat_timeout_sec = 15          # mark client down after this many seconds
log_file              = ""          # empty = stdout only
```

### Client — `config/client_config.toml`

```toml
[client]
room_name   = "Room 1"          # displayed in alarm messages
server_ip   = "192.168.1.100"   # IP of the server PC
server_port = 9999
hotkey      = "alt+n"           # any combo supported by the keyboard library
alarm_sound = ""                # empty = use bundled alarm.wav
log_file    = ""
```

If no config file is found at startup, a default one is written to the current
working directory so you can edit it.

---

## Running (development / macOS)

### Start the server

```bash
python -m server.server --config config/server_config.toml
```

### Start a client

```bash
# Uses the global hotkey (requires Accessibility permission on macOS)
python -m client.client --config config/client_config.toml

# Fallback mode: type 'a' + Enter in the terminal to trigger an alarm
python -m client.client --config config/client_config.toml --fallback-hotkey
```

### Multi-room simulation (macOS — opens Terminal windows)

```bash
./sim/run_simulation.sh 5   # simulate 5 rooms
```

### Headless simulation (no GUI — good for CI)

```bash
python sim/simulate.py --rooms 5 --alarm-from "Room 3" --duration 15
```

---

## Running tests

```bash
pytest tests/ -v
```

All 24 tests cover:
- Protocol encode/decode (all message types, error cases, round-trips)
- Server alarm broadcast to all clients
- `client_down` broadcast on disconnect
- `client_up` broadcast on reconnect
- Config defaults

---

## Windows 11 Deployment

### 1. Build executables

```bat
REM On Windows (or cross-compile via PyInstaller):
pip install pyinstaller
bash scripts/build_executables.sh
```

This produces:
- `dist/alarm_server.exe`
- `dist/alarm_client.exe`

### 2. Deploy the server

Copy to the always-on server PC:
```
alarm_server.exe
server_config.toml   ← edit host/port if needed
```

Run once as Administrator to register auto-start:
```bat
alarm_server.exe --install
```

Open Windows Firewall inbound rule for TCP port 9999.

### 3. Deploy each client

Copy to each room PC:
```
alarm_client.exe
client_config.toml   ← set room_name and server_ip for this room
```

Run once as Administrator to register auto-start:
```bat
alarm_client.exe --install
```

> **Note:** The Task Scheduler job runs with "highest privileges" so the
> hotkey works even when elevated applications have focus.

---

## Repository structure

```
alarm-system/
├── PLAN.md                         Project plan & architecture
├── README.md
├── pyproject.toml
├── requirements.txt
├── requirements-dev.txt
│
├── common/
│   ├── protocol.py                 Message types + JSON encode/decode
│   └── config.py                  TOML config loading + defaults
│
├── server/
│   └── server.py                  WebSocket hub + health monitor
│
├── client/
│   ├── client.py                  Main client loop (WebSocket + hotkey)
│   ├── hotkey.py                  Global hotkey registration
│   ├── overlay.py                 Full-screen tkinter alarm overlay
│   └── sound.py                   pygame-based looped sound playback
│
├── assets/
│   └── alarm.wav                  Bundled alarm sound (two-tone beep)
│
├── config/
│   ├── server_config.toml         Default server config
│   └── client_config.toml         Default client config (edit per room)
│
├── scripts/
│   ├── install_autostart_windows.py   Windows Task Scheduler registration
│   ├── install_autostart_mac.py       macOS launchd plist registration
│   └── build_executables.sh           PyInstaller build script
│
├── sim/
│   ├── run_simulation.sh          Multi-window macOS simulation script
│   └── simulate.py                Headless asyncio simulation
│
└── tests/
    ├── test_protocol.py           17 protocol unit tests
    └── test_server.py             7 server integration tests
```

---

## Hotkey notes

| Platform | Requirement |
|----------|-------------|
| Windows  | Run as Administrator (or Task Scheduler "highest privileges") for global capture across all apps |
| macOS    | Grant Accessibility permission once: System Settings → Privacy & Security → Accessibility |

---

## Alarm overlay

- Full-screen red background (`#CC0000`) flashing with dark red every 500 ms
- Large white text: `⚠ ALARM — ROOM X ⚠`
- Timestamp of when the alarm was triggered
- **Dismiss** button or press **ESC**
- Amber corner banner for `client_down` warnings; auto-dismissed green banner for `client_up`
