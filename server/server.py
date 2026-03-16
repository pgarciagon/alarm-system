"""
server.py — WebSocket hub + health monitor.

Usage:
    python -m server.server [--config path/to/server_config.toml] [--install]
                            [--gui | --no-gui]

    --install   Register a Windows Task Scheduler / macOS launchd auto-start
                entry and exit.  Run once with admin rights on first deploy.
    --gui       Show the dashboard GUI (default on Windows).
    --no-gui    Run headless without GUI.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import websockets
try:
    from websockets.server import ServerConnection as _WS  # websockets v14+
except ImportError:
    from websockets.server import _WS as _WS  # type: ignore[assignment] # v12

# Make the package importable when run as __main__ from the repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

from common.config import ServerConfig, load_server_config
from common.protocol import (
    AlarmMsg,
    ClientDownMsg,
    ClientListMsg,
    ClientUpMsg,
    HeartbeatMsg,
    RegisterMsg,
    RemoveClientMsg,
    SetHotkeyMsg,
    decode,
    encode,
    MSG_ALARM,
    MSG_HEARTBEAT,
    MSG_REGISTER,
    MSG_DISMISS,
    MSG_REMOVE_CLIENT,
    MSG_SET_HOTKEY,
)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _setup_logging(log_file: str) -> logging.Logger:
    logger = logging.getLogger("alarm.server")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [SERVER] %(levelname)s %(message)s")

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    if log_file:
        fh = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger


# ---------------------------------------------------------------------------
# Client registry entry
# ---------------------------------------------------------------------------

class ClientEntry:
    __slots__ = ("ws", "last_heartbeat", "is_down", "hotkey")

    def __init__(self, ws: _WS, hotkey: str = "") -> None:
        self.ws = ws
        self.last_heartbeat: float = time.monotonic()
        self.is_down: bool = False
        self.hotkey: str = hotkey


# ---------------------------------------------------------------------------
# Snapshot (thread-safe read from GUI)
# ---------------------------------------------------------------------------

@dataclass
class ClientSnapshot:
    room: str
    is_down: bool
    last_heartbeat: float  # time.monotonic() timestamp
    hotkey: str = ""


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class AlarmServer:
    def __init__(self, cfg: ServerConfig) -> None:
        self.cfg = cfg
        self.log = _setup_logging(cfg.log_file)
        # room_name → ClientEntry
        self._clients: Dict[str, ClientEntry] = {}
        self._lock = asyncio.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self.log.info(
            "Starting Alarm Server on %s:%d (heartbeat timeout=%ds)",
            self.cfg.host, self.cfg.port, self.cfg.heartbeat_timeout_sec,
        )
        async with websockets.serve(
            self._handle_client,
            self.cfg.host,
            self.cfg.port,
            ping_interval=20,
            ping_timeout=30,
        ):
            await asyncio.gather(
                self._health_monitor(),
                self._stop_event.wait(),
            )

    # ------------------------------------------------------------------
    # Thread-safe public API (callable from GUI thread)
    # ------------------------------------------------------------------

    def get_client_snapshot(self) -> List[ClientSnapshot]:
        """Return a snapshot of all clients.  GIL-safe for cross-thread reads."""
        return [
            ClientSnapshot(room=room, is_down=e.is_down,
                          last_heartbeat=e.last_heartbeat, hotkey=e.hotkey)
            for room, e in self._clients.items()
        ]

    def request_shutdown(self) -> None:
        """Signal the server to stop (thread-safe)."""
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._stop_event.set)

    def remove_client(self, room: str) -> None:
        """Remove a client from the registry (thread-safe, called from GUI)."""
        if self._loop and self._loop.is_running():
            msg = RemoveClientMsg(room=room)
            self._loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(self._on_remove_client(msg))
            )

    def set_client_hotkey(self, room: str, hotkey: str) -> None:
        """Change a client's hotkey (thread-safe, called from GUI)."""
        if self._loop and self._loop.is_running():
            msg = SetHotkeyMsg(room=room, hotkey=hotkey)
            self._loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(self._on_set_hotkey(msg))
            )

    # ------------------------------------------------------------------
    # Per-connection handler
    # ------------------------------------------------------------------

    async def _handle_client(self, ws: _WS) -> None:
        room: Optional[str] = None
        remote = ws.remote_address
        self.log.info("New connection from %s", remote)
        try:
            async for raw in ws:
                try:
                    msg = decode(str(raw))
                except ValueError as exc:
                    self.log.warning("Bad message from %s: %s", remote, exc)
                    continue

                if msg.type == MSG_REGISTER:
                    room = await self._on_register(ws, msg)  # type: ignore[arg-type]

                elif msg.type == MSG_HEARTBEAT:
                    await self._on_heartbeat(msg)  # type: ignore[arg-type]

                elif msg.type == MSG_ALARM:
                    await self._on_alarm(msg)  # type: ignore[arg-type]

                elif msg.type == MSG_DISMISS:
                    pass  # reserved for future use

                elif msg.type == MSG_REMOVE_CLIENT:
                    await self._on_remove_client(msg)  # type: ignore[arg-type]

                elif msg.type == MSG_SET_HOTKEY:
                    await self._on_set_hotkey(msg)  # type: ignore[arg-type]

                else:
                    self.log.warning("Unknown message type %r from %s", msg.type, remote)

        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            if room:
                await self._on_disconnect(room)

    # ------------------------------------------------------------------
    # Message handlers
    # ------------------------------------------------------------------

    async def _on_register(self, ws: _WS, msg: RegisterMsg) -> str:
        room = msg.room
        hotkey = getattr(msg, 'hotkey', '') or ''
        async with self._lock:
            existing = self._clients.get(room)
            if existing and existing.is_down:
                # Room reconnecting after being marked down
                existing.ws = ws
                existing.last_heartbeat = time.monotonic()
                existing.is_down = False
                existing.hotkey = hotkey
                self.log.info("Room %r reconnected", room)
                await self._broadcast(ClientUpMsg(room=room), exclude=None)
            else:
                self._clients[room] = ClientEntry(ws, hotkey=hotkey)
                self.log.info("Room %r registered (total clients: %d)", room, len(self._clients))
            await self._broadcast_client_list()

        return room

    async def _on_heartbeat(self, msg: HeartbeatMsg) -> None:
        async with self._lock:
            entry = self._clients.get(msg.room)
            if entry:
                was_down = entry.is_down
                entry.last_heartbeat = time.monotonic()
                if was_down:
                    entry.is_down = False
                    self.log.info("Room %r heartbeat recovered", msg.room)
                    await self._broadcast(ClientUpMsg(room=msg.room), exclude=None)
                    await self._broadcast_client_list()

    async def _on_alarm(self, msg: AlarmMsg) -> None:
        exclude = msg.room if self.cfg.silent_alarm else None
        self.log.info(
            "ALARM triggered from room %r — broadcasting to %s",
            msg.room,
            "all other clients" if exclude else "all clients (including sender)",
        )
        await self._broadcast(AlarmMsg(room=msg.room), exclude=exclude)

    async def _on_remove_client(self, msg: RemoveClientMsg) -> None:
        async with self._lock:
            entry = self._clients.pop(msg.room, None)
            if entry:
                self.log.info("Room %r removed by server", msg.room)
                try:
                    await entry.ws.close()
                except Exception:
                    pass
                await self._broadcast(ClientDownMsg(room=msg.room), exclude=msg.room)
                await self._broadcast_client_list()

    async def _on_set_hotkey(self, msg: SetHotkeyMsg) -> None:
        async with self._lock:
            entry = self._clients.get(msg.room)
            if entry:
                entry.hotkey = msg.hotkey
                if not entry.is_down:
                    try:
                        await entry.ws.send(encode(SetHotkeyMsg(room=msg.room, hotkey=msg.hotkey)))
                    except Exception:
                        pass
                self.log.info("Hotkey for room %r set to %r", msg.room, msg.hotkey)
                await self._broadcast_client_list()

    async def _on_disconnect(self, room: str) -> None:
        async with self._lock:
            entry = self._clients.get(room)
            if entry and not entry.is_down:
                entry.is_down = True
                self.log.warning("Room %r disconnected", room)
                await self._broadcast(ClientDownMsg(room=room), exclude=room)
                await self._broadcast_client_list()

    # ------------------------------------------------------------------
    # Health monitor
    # ------------------------------------------------------------------

    async def _health_monitor(self) -> None:
        """Periodically check for clients that stopped sending heartbeats."""
        while True:
            await asyncio.sleep(10)
            now = time.monotonic()
            async with self._lock:
                for room, entry in list(self._clients.items()):
                    age = now - entry.last_heartbeat
                    if not entry.is_down and age > self.cfg.heartbeat_timeout_sec:
                        entry.is_down = True
                        self.log.warning(
                            "Room %r heartbeat timeout (last seen %.1fs ago)", room, age
                        )
                        await self._broadcast(ClientDownMsg(room=room), exclude=room)
                        await self._broadcast_client_list()

    # ------------------------------------------------------------------
    # Client list helper
    # ------------------------------------------------------------------

    def _build_client_list_msg(self) -> ClientListMsg:
        """Build a client_list message from current state (call inside self._lock)."""
        clients = [
            {"room": room, "is_down": entry.is_down, "hotkey": entry.hotkey}
            for room, entry in self._clients.items()
        ]
        return ClientListMsg(clients=clients)

    async def _broadcast_client_list(self) -> None:
        """Broadcast the current client list to all connected clients (call inside self._lock)."""
        await self._broadcast(self._build_client_list_msg(), exclude=None)

    # ------------------------------------------------------------------
    # Broadcast helper
    # ------------------------------------------------------------------

    async def _broadcast(self, msg, exclude: Optional[str] = None) -> None:
        """Send *msg* to every connected (not-down) client except *exclude* room."""
        payload = encode(msg)
        dead_rooms: List[str] = []

        for room, entry in self._clients.items():
            if room == exclude:
                continue
            if entry.is_down:
                continue
            try:
                await entry.ws.send(payload)
            except websockets.exceptions.ConnectionClosed:
                dead_rooms.append(room)

        # Mark any that failed during broadcast as down (will be cleaned next cycle)
        for room in dead_rooms:
            entry = self._clients[room]
            if not entry.is_down:
                entry.is_down = True
                self.log.warning("Room %r went away during broadcast", room)
                # Re-broadcast the client_down without holding lock (already inside lock)
                # Schedule it as a task to avoid recursion issues
                asyncio.create_task(
                    self._broadcast(ClientDownMsg(room=room), exclude=room)
                )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _run_server_in_thread(server: AlarmServer) -> None:
    """Run the server's asyncio loop in a background thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(server.run())
    finally:
        loop.close()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Alarm System Server")
    parser.add_argument("--config", default=None, help="Path to server_config.toml")
    parser.add_argument(
        "--install",
        action="store_true",
        help="Register auto-start task and exit",
    )
    gui_group = parser.add_mutually_exclusive_group()
    gui_group.add_argument("--gui", action="store_true", default=None,
                           help="Show dashboard GUI (default on Windows)")
    gui_group.add_argument("--no-gui", action="store_true",
                           help="Run headless without GUI")
    args = parser.parse_args()

    cfg = load_server_config(args.config)

    if args.install:
        _install_autostart(cfg)
        return

    use_gui = args.gui if args.gui is not None else (not args.no_gui and sys.platform == "win32")

    server = AlarmServer(cfg)

    if use_gui:
        from server.dashboard import ServerDashboard

        bg = threading.Thread(
            target=_run_server_in_thread, args=(server,),
            daemon=True, name="asyncio-server",
        )
        bg.start()
        dashboard = ServerDashboard(server, cfg)
        dashboard.run_mainloop()
        server.request_shutdown()
        bg.join(timeout=5)
    else:
        asyncio.run(server.run())


def _install_autostart(cfg: ServerConfig) -> None:
    """Delegate to the platform-specific installer script."""
    import subprocess

    scripts_dir = Path(__file__).parent.parent / "scripts"
    exe = sys.executable
    target = Path(sys.argv[0]).resolve()

    if sys.platform == "win32":
        script = scripts_dir / "install_autostart_windows.py"
        subprocess.run([exe, str(script), "--target", str(target), "--role", "server"], check=True)
    elif sys.platform == "darwin":
        script = scripts_dir / "install_autostart_mac.py"
        subprocess.run([exe, str(script), "--target", str(target), "--role", "server"], check=True)
    else:
        print("Auto-start not supported on this platform. Configure manually.")


if __name__ == "__main__":
    main()
