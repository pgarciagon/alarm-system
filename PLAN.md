# Alarm System — Project Plan

## 1. Overview

A lightweight emergency alert system for a doctor's practice. Any PC can trigger
a full-screen visual + audio alarm on **all other PCs** by pressing a configurable
hotkey. A central server coordinates state and monitors health of every client.

---

## 2. Requirements Summary

| ID | Requirement |
|----|-------------|
| R1 | Configurable hotkey (default `ALT+N`) triggers a full-screen alarm on every PC |
| R2 | Alarm message shows "ALARM on ROOM X" (X = configurable room name) |
| R3 | Alarm is accompanied by an audible sound |
| R4 | Works regardless of which application has focus |
| R5 | Auto-starts on boot (server and all clients) |
| R6 | If a client is unreachable, all others display "Alert system not working on ROOM X" |
| R7 | Codebase runs on macOS for development/simulation, deploys to Windows 11 |
| R8 | System is simple and resource-light |

---

## 3. Architecture

```
┌──────────────────────────────────────────────────────┐
│                   Local LAN (same subnet)            │
│                                                      │
│  ┌─────────┐   WebSocket/TCP   ┌──────────────────┐  │
│  │ SERVER  │◄──────────────────│  CLIENT (Room A) │  │
│  │(always  │                   └──────────────────┘  │
│  │  on)    │   WebSocket/TCP   ┌──────────────────┐  │
│  │         │◄──────────────────│  CLIENT (Room B) │  │
│  │         │                   └──────────────────┘  │
│  │         │        ...              ...             │
│  └─────────┘                                        │
└──────────────────────────────────────────────────────┘
```

### Components

| Component | Role |
|-----------|------|
| **Server** | Central hub: receives alarm events, broadcasts to all clients, tracks client health, serves config |
| **Client** | Runs on every room PC: registers with server, listens for global hotkey, shows/hides alarm overlay, sends heartbeat |

---

## 4. Technology Stack

### Language: **Python 3.11+**

- Ships as self-contained executables via **PyInstaller** (one `.exe` for server, one `.exe` for client)
- Cross-platform: identical source runs on macOS (dev/sim) and Windows 11 (production)
- Minimal dependencies, easy to read and maintain

### Key Libraries

| Purpose | Library | Notes |
|---------|---------|-------|
| WebSocket server/client | `websockets` (asyncio) | Lightweight, pure-Python |
| Global hotkey capture | `keyboard` | Works system-wide on Win & Mac |
| Overlay window | `tkinter` (stdlib) | No extra install; full-screen, always-on-top |
| Audio playback | `playsound` or `pygame.mixer` | Simple, cross-platform |
| Config file | `tomllib` / `tomli` (stdlib in 3.11) | Human-readable TOML config |
| Auto-start (Windows) | Windows Task Scheduler via `subprocess` at first run | Service-like behaviour |
| Auto-start (macOS dev) | `launchd` plist | For simulation |

> **Why WebSocket over raw TCP?**
> Built-in framing, easy to extend, excellent library support, works through most
> local firewalls without extra config.

> **Why not a Windows Service?**
> Services require elevated install complexity. Task Scheduler with
> `ONLOGON` + `ONSTART` triggers covers the requirement with zero UAC headaches
> and is straightforward to script.

---

## 5. Communication Protocol

All messages are **JSON over WebSocket**.

### Client → Server

```jsonc
// Register on connect
{ "type": "register", "room": "Room 3" }

// Trigger alarm
{ "type": "alarm", "room": "Room 3" }

// Periodic heartbeat (every 5 s)
{ "type": "heartbeat", "room": "Room 3" }

// Dismiss alarm (optional, for future use)
{ "type": "dismiss", "room": "Room 3" }
```

### Server → Client (broadcast)

```jsonc
// Broadcast alarm to all clients
{ "type": "alarm", "room": "Room 3" }

// A client missed heartbeats — warn all others
{ "type": "client_down", "room": "Room 5" }

// A previously-down client reconnected
{ "type": "client_up", "room": "Room 5" }
```

---

## 6. Server Behaviour

1. **Listen** on a configurable TCP port (default `9999`) for WebSocket connections.
2. Maintain a registry of connected clients: `{ room_name → (websocket, last_heartbeat) }`.
3. On `alarm` message: immediately broadcast `{ type: alarm, room: X }` to **all** connected clients including the sender.
4. Health monitor loop (every 10 s): any client whose `last_heartbeat` is older than 15 s is marked `DOWN`; a `client_down` broadcast is sent once per transition. When the client reconnects a `client_up` broadcast is sent.
5. On client disconnect: immediately trigger the `client_down` broadcast.
6. **Config** is read from `server_config.toml` at startup; no config file → sensible defaults + auto-generate.

---

## 7. Client Behaviour

1. On startup: read `client_config.toml` (room name, server IP/port, hotkey).
2. Register global hotkey listener (runs in background thread, intercepting at OS level).
3. Connect to server via WebSocket; reconnect automatically with exponential back-off (1 s, 2 s, 4 s … max 30 s) if server is unavailable.
4. Send heartbeat every 5 seconds while connected.
5. On hotkey press: send `{ type: alarm, room: <own_room> }` to server.
6. On receiving `alarm` message: show full-screen overlay + play alarm sound.
7. On receiving `client_down` / `client_up`: show/hide a non-intrusive status banner.
8. Overlay dismissed by pressing `ESC` or clicking a "DISMISS" button.

---

## 8. Overlay Design

- **Tkinter** window, `overrideredirect(True)` + `attributes('-topmost', True)` + geometry spanning full screen.
- Background: **red** (`#CC0000`).
- Text: white, very large font (`Arial 72 bold`): `⚠ ALARM — ROOM 3 ⚠`
- Sub-text (smaller): timestamp of the alarm.
- Flashing animation: toggle background between red and dark-red every 500 ms.
- Sound: bundled `.wav` file looped until dismissed.
- `client_down` warning: smaller amber banner (not full-screen) in the corner.

---

## 9. Configuration Files

### `server_config.toml`
```toml
[server]
host = "0.0.0.0"
port = 9999
heartbeat_timeout_sec = 15
```

### `client_config.toml`
```toml
[client]
room_name  = "Room 1"          # Unique name shown in alarm
server_ip  = "192.168.1.100"   # IP of the always-on server
server_port = 9999
hotkey     = "alt+n"           # Any combo supported by `keyboard` lib
```

---

## 10. Repository Structure

```
alarm-system/
├── PLAN.md                   ← this file
├── README.md
├── pyproject.toml            ← project metadata + dependencies
├── requirements.txt
│
├── common/
│   ├── __init__.py
│   ├── protocol.py           ← message dataclasses + JSON helpers
│   └── config.py             ← config loading / defaults
│
├── server/
│   ├── __init__.py
│   └── server.py             ← asyncio WebSocket server + health monitor
│
├── client/
│   ├── __init__.py
│   ├── client.py             ← WebSocket client + reconnect loop
│   ├── hotkey.py             ← global hotkey registration
│   ├── overlay.py            ← tkinter full-screen alarm overlay
│   └── sound.py              ← audio playback wrapper
│
├── assets/
│   └── alarm.wav             ← bundled alarm sound
│
├── config/
│   ├── server_config.toml    ← example / default server config
│   └── client_config.toml    ← example / default client config
│
├── scripts/
│   ├── install_autostart_windows.py   ← registers Task Scheduler entry
│   ├── install_autostart_mac.py       ← writes launchd plist (dev)
│   └── build_executables.sh           ← PyInstaller build script
│
└── tests/
    ├── test_protocol.py
    └── test_server.py
```

---

## 11. macOS Development & Simulation

Since development happens on a Mac, the same Python code runs unmodified:

- `keyboard` library works on macOS (requires Accessibility permission once).
- `tkinter` is available via `python.org` macOS installer or `brew install python-tk`.
- Run multiple terminal windows to simulate N clients + 1 server on `localhost`.
- Each simulated client uses a different `room_name` and all point to `127.0.0.1`.
- A small **`sim/run_simulation.sh`** script will launch server + N clients in one command using separate processes.

---

## 12. Windows 11 Deployment

1. Build `server.exe` and `client.exe` with PyInstaller (`--onefile --noconsole`).
2. Place `server.exe` + `server_config.toml` on the server PC.
3. Run `server.exe --install` once → registers Windows Task Scheduler job.
4. For each room PC: place `client.exe` + `client_config.toml` (with correct `room_name` and `server_ip`).
5. Run `client.exe --install` once → registers Windows Task Scheduler job.
6. No further configuration needed; everything auto-starts on boot/login.

> **Hotkey elevation note:** The `keyboard` library on Windows requires the
> process to run with administrator rights to capture keys when another
> elevated application has focus. The Task Scheduler job should be configured
> with "Run with highest privileges" to cover this edge case.

---

## 13. Development Phases

### Phase 1 — Core (MVP)
- [ ] Repo initialisation + `pyproject.toml`
- [ ] `common/protocol.py` — message types
- [ ] `common/config.py` — TOML config loading
- [ ] `server/server.py` — WebSocket hub + health monitor
- [ ] `client/client.py` — connection + reconnect loop
- [ ] `client/hotkey.py` — global hotkey detection
- [ ] `client/overlay.py` — full-screen alarm overlay
- [ ] `client/sound.py` — alarm sound playback
- [ ] macOS simulation script

### Phase 2 — Polish & Robustness
- [ ] `client_down` / `client_up` status banner on clients
- [ ] Dismiss functionality (ESC / button)
- [ ] Configurable alarm sound path
- [ ] Logging to rotating file (server + client)
- [ ] Unit tests for protocol and server logic

### Phase 3 — Windows Packaging & Deployment
- [ ] PyInstaller build scripts
- [ ] Auto-start installer scripts (Windows Task Scheduler + macOS launchd)
- [ ] Deployment documentation
- [ ] End-to-end test on Windows 11 VM

---

## 14. Risk & Mitigation

| Risk | Mitigation |
|------|-----------|
| `keyboard` lib needs admin on Windows for elevated-app hotkeys | Schedule task with "highest privileges" |
| Server IP changes (DHCP) | Assign static IP or hostname to server; document in `client_config.toml` |
| Firewall blocks port 9999 | Document Windows Firewall inbound rule; add to deployment script |
| tkinter not bundled on some Python builds | Use official python.org installer; PyInstaller bundles it |
| Sound file missing | Bundle `alarm.wav` in executable via PyInstaller `--add-data` |

---

## 15. GitHub Repository

- **URL:** `https://github.com/pgarciagon/alarm-system` (private)
- Branch strategy: `main` (stable) + feature branches
- A `README.md` will describe setup, config, and deployment steps

---

*Plan version: 1.0 — 2026-03-15*
