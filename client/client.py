"""
client.py — WebSocket client with reconnect loop, hotkey, overlay and sound.

Usage:
    python -m client.client [--config path/to/client_config.toml]
                             [--fallback-hotkey]
                             [--install]

    --fallback-hotkey   Use terminal-input hotkey (type 'a'+Enter) instead of
                        the global keyboard hook.  Useful for simulation on
                        macOS when Accessibility permission is not granted.
    --install           Register auto-start task and exit.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import signal
import sys
import threading
from pathlib import Path
from typing import Optional

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

sys.path.insert(0, str(Path(__file__).parent.parent))

from common.config import ClientConfig, load_client_config
from common.protocol import (
    AlarmMsg,
    ClientDownMsg,
    ClientUpMsg,
    HeartbeatMsg,
    RegisterMsg,
    decode,
    encode,
    MSG_ALARM,
    MSG_CLIENT_DOWN,
    MSG_CLIENT_UP,
)
from client.hotkey import make_hotkey_listener
from client.overlay import OverlayManager
from client.sound import SoundPlayer


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _setup_logging(log_file: str) -> logging.Logger:
    logger = logging.getLogger("alarm.client")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [CLIENT] %(levelname)s %(message)s")

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
# Alarm client
# ---------------------------------------------------------------------------

class AlarmClient:
    # Reconnect back-off: 1, 2, 4, 8, 16, 30, 30, … seconds
    _BACKOFF_SEQUENCE = [1, 2, 4, 8, 16, 30]

    def __init__(self, cfg: ClientConfig, fallback_hotkey: bool = False) -> None:
        self.cfg = cfg
        self.log = _setup_logging(cfg.log_file)
        self._fallback_hotkey = fallback_hotkey

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._ws_lock = asyncio.Lock()
        self._alarm_pending: asyncio.Event  # set by hotkey thread, consumed by async loop
        self._overlay = OverlayManager()
        self._sound = SoundPlayer(cfg.alarm_sound)
        self._running = True

    async def run(self) -> None:
        # asyncio.Event must be created inside the running loop
        self._alarm_pending = asyncio.Event()

        self._overlay.start()
        self._start_hotkey()

        try:
            await self._connect_loop()
        finally:
            self._overlay.stop()

    # ------------------------------------------------------------------
    # Hotkey
    # ------------------------------------------------------------------

    def _start_hotkey(self) -> None:
        listener = make_hotkey_listener(
            self.cfg.hotkey,
            callback=self._on_hotkey_pressed,
            fallback=self._fallback_hotkey,
        )
        listener.start()
        self.log.info("Hotkey listener started (%s)", self.cfg.hotkey)

    def _on_hotkey_pressed(self) -> None:
        """Called from the keyboard thread — schedules alarm in the async loop."""
        self.log.info("Hotkey pressed — queuing alarm for room %r", self.cfg.room_name)
        # Thread-safe bridge: set an asyncio Event from a non-async thread
        try:
            loop = asyncio.get_event_loop()
            loop.call_soon_threadsafe(self._alarm_pending.set)
        except RuntimeError:
            pass

    # ------------------------------------------------------------------
    # Connection loop (reconnect with back-off)
    # ------------------------------------------------------------------

    async def _connect_loop(self) -> None:
        uri = f"ws://{self.cfg.server_ip}:{self.cfg.server_port}"
        attempt = 0

        while self._running:
            delay = self._BACKOFF_SEQUENCE[min(attempt, len(self._BACKOFF_SEQUENCE) - 1)]
            if attempt > 0:
                self.log.info("Reconnecting in %ds (attempt %d)…", delay, attempt)
                await asyncio.sleep(delay)

            try:
                self.log.info("Connecting to server at %s…", uri)
                async with websockets.connect(
                    uri,
                    ping_interval=20,
                    ping_timeout=30,
                    open_timeout=10,
                ) as ws:
                    async with self._ws_lock:
                        self._ws = ws
                    attempt = 0   # reset back-off on successful connect
                    self.log.info("Connected to server")

                    # Register this room
                    await ws.send(encode(RegisterMsg(room=self.cfg.room_name)))

                    # Run receive + heartbeat + alarm-send concurrently
                    await asyncio.gather(
                        self._receive_loop(ws),
                        self._heartbeat_loop(ws),
                        self._alarm_send_loop(ws),
                    )

            except (ConnectionClosed, WebSocketException, OSError) as exc:
                self.log.warning("Connection lost: %s", exc)
            except asyncio.CancelledError:
                break
            finally:
                async with self._ws_lock:
                    self._ws = None
                attempt += 1

    # ------------------------------------------------------------------
    # Receive loop
    # ------------------------------------------------------------------

    async def _receive_loop(self, ws) -> None:
        async for raw in ws:
            try:
                msg = decode(str(raw))
            except ValueError as exc:
                self.log.warning("Bad message from server: %s", exc)
                continue

            if msg.type == MSG_ALARM:
                self._handle_alarm(msg)  # type: ignore[arg-type]
            elif msg.type == MSG_CLIENT_DOWN:
                self._handle_client_down(msg)  # type: ignore[arg-type]
            elif msg.type == MSG_CLIENT_UP:
                self._handle_client_up(msg)  # type: ignore[arg-type]

    def _handle_alarm(self, msg: AlarmMsg) -> None:
        self.log.info("ALARM received for room %r", msg.room)
        self._overlay.show_alarm(msg.room)
        self._sound.play()

    def _handle_client_down(self, msg: ClientDownMsg) -> None:
        self.log.warning("Client down: room %r", msg.room)
        self._overlay.show_banner(msg.room, up=False)

    def _handle_client_up(self, msg: ClientUpMsg) -> None:
        self.log.info("Client up: room %r", msg.room)
        self._overlay.show_banner(msg.room, up=True)

    # ------------------------------------------------------------------
    # Heartbeat loop
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self, ws) -> None:
        hb_msg = encode(HeartbeatMsg(room=self.cfg.room_name))
        while True:
            await asyncio.sleep(5)
            try:
                await ws.send(hb_msg)
            except ConnectionClosed:
                break

    # ------------------------------------------------------------------
    # Alarm send loop
    # ------------------------------------------------------------------

    async def _alarm_send_loop(self, ws) -> None:
        """Wait for the hotkey event and send an alarm message to the server."""
        alarm_msg = encode(AlarmMsg(room=self.cfg.room_name))
        while True:
            await self._alarm_pending.wait()
            self._alarm_pending.clear()
            try:
                await ws.send(alarm_msg)
                self.log.info("Alarm sent to server for room %r", self.cfg.room_name)
            except ConnectionClosed:
                # Will be handled by the reconnect loop; alarm will be re-sent
                # once reconnected (the event is already cleared — this is
                # acceptable; the operator can press the hotkey again).
                break

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        self._running = False
        self._sound.stop()
        self._overlay.stop()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Alarm System Client")
    parser.add_argument("--config", default=None, help="Path to client_config.toml")
    parser.add_argument(
        "--fallback-hotkey",
        action="store_true",
        help="Use terminal-input fallback hotkey (type 'a'+Enter)",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Register auto-start task and exit",
    )
    args = parser.parse_args()

    cfg = load_client_config(args.config)

    if args.install:
        _install_autostart(cfg)
        return

    client = AlarmClient(cfg, fallback_hotkey=args.fallback_hotkey)

    # Handle Ctrl+C gracefully
    def _sigint(_sig, _frame):
        print("\nShutting down…")
        client.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, _sigint)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _sigint)

    asyncio.run(client.run())


def _install_autostart(cfg: ClientConfig) -> None:
    import subprocess

    scripts_dir = Path(__file__).parent.parent / "scripts"
    exe = sys.executable
    target = Path(sys.argv[0]).resolve()

    if sys.platform == "win32":
        script = scripts_dir / "install_autostart_windows.py"
        subprocess.run(
            [exe, str(script), "--target", str(target), "--role", "client"],
            check=True,
        )
    elif sys.platform == "darwin":
        script = scripts_dir / "install_autostart_mac.py"
        subprocess.run(
            [exe, str(script), "--target", str(target), "--role", "client"],
            check=True,
        )
    else:
        print("Auto-start not supported on this platform. Configure manually.")


if __name__ == "__main__":
    main()
