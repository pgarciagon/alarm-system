"""
client.py — WebSocket client with reconnect loop, hotkey, overlay and sound.

macOS AppKit requires tkinter to run on the main OS thread.
Therefore this module runs asyncio in a background daemon thread and
keeps the main thread for tkinter's mainloop().

Usage:
    python -m client.client [--config path/to/client_config.toml]
                             [--fallback-hotkey]
                             [--install]

    --fallback-hotkey   Use terminal-input hotkey (type 'a'+Enter) instead of
                        the global keyboard hook.  Useful for macOS simulation
                        when Accessibility permission is not granted.
    --install           Register auto-start task and exit.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Optional

# Suppress pygame startup banner before any pygame import happens
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
# Tell SDL not to touch the display server (avoids Tcl notifier conflict on macOS)
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

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
    MSG_CLIENT_LIST,
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
# Async network + hotkey core  (runs in background thread)
# ---------------------------------------------------------------------------

class _AsyncCore:
    """Everything that lives inside the asyncio event loop."""

    _BACKOFF = [1, 2, 4, 8, 16, 30]

    def __init__(
        self,
        cfg: ClientConfig,
        overlay: OverlayManager,
        sound: SoundPlayer,
        log: logging.Logger,
        fallback_hotkey: bool,
    ) -> None:
        self.cfg = cfg
        self._overlay = overlay
        self._sound = sound
        self.log = log
        self._fallback_hotkey = fallback_hotkey
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._alarm_pending: Optional[asyncio.Event] = None
        self._running = True

    # ------------------------------------------------------------------
    # Entry point — called from the asyncio background thread
    # ------------------------------------------------------------------

    def run_in_thread(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        finally:
            self._loop.close()

    async def _main(self) -> None:
        self._alarm_pending = asyncio.Event()
        self._start_hotkey()
        await self._connect_loop()

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
        self.log.info("Hotkey pressed — queuing alarm for room %r", self.cfg.room_name)
        if self._loop and self._alarm_pending:
            self._loop.call_soon_threadsafe(self._alarm_pending.set)

    # ------------------------------------------------------------------
    # Connection loop
    # ------------------------------------------------------------------

    async def _connect_loop(self) -> None:
        uri = f"ws://{self.cfg.server_ip}:{self.cfg.server_port}"
        attempt = 0

        while self._running:
            delay = self._BACKOFF[min(attempt, len(self._BACKOFF) - 1)]
            if attempt > 0:
                self.log.info("Reconnecting in %ds (attempt %d)…", delay, attempt)
                await asyncio.sleep(delay)

            try:
                self.log.info("Connecting to %s…", uri)
                async with websockets.connect(
                    uri,
                    ping_interval=20,
                    ping_timeout=30,
                    open_timeout=10,
                ) as ws:
                    attempt = 0
                    self.log.info("Connected to server")
                    await ws.send(encode(RegisterMsg(room=self.cfg.room_name)))
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
                attempt += 1

    # ------------------------------------------------------------------
    # Receive loop
    # ------------------------------------------------------------------

    async def _receive_loop(self, ws) -> None:
        async for raw in ws:
            try:
                msg = decode(str(raw))
            except ValueError as exc:
                self.log.warning("Bad message: %s", exc)
                continue

            if msg.type == MSG_ALARM:
                self.log.info("ALARM received from room %r", msg.room)  # type: ignore[union-attr]
                self._overlay.show_alarm(msg.room)  # type: ignore[union-attr]
                self._sound.play()
            elif msg.type == MSG_CLIENT_DOWN:
                self.log.warning("Client down: room %r", msg.room)  # type: ignore[union-attr]
                self._overlay.show_banner(msg.room, up=False)  # type: ignore[union-attr]
            elif msg.type == MSG_CLIENT_UP:
                self.log.info("Client up: room %r", msg.room)  # type: ignore[union-attr]
                self._overlay.show_banner(msg.room, up=True)  # type: ignore[union-attr]
            elif msg.type == MSG_CLIENT_LIST:
                self._overlay.update_client_list(msg.clients)  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # Heartbeat loop
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self, ws) -> None:
        hb = encode(HeartbeatMsg(room=self.cfg.room_name))
        while True:
            await asyncio.sleep(5)
            try:
                await ws.send(hb)
            except ConnectionClosed:
                break

    # ------------------------------------------------------------------
    # Alarm send loop
    # ------------------------------------------------------------------

    async def _alarm_send_loop(self, ws) -> None:
        alarm_msg = encode(AlarmMsg(room=self.cfg.room_name))
        while True:
            await self._alarm_pending.wait()  # type: ignore[union-attr]
            self._alarm_pending.clear()  # type: ignore[union-attr]
            try:
                await ws.send(alarm_msg)
                self.log.info("Alarm sent for room %r", self.cfg.room_name)
            except ConnectionClosed:
                break

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        self._running = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)


# ---------------------------------------------------------------------------
# Top-level AlarmClient  (coordinates main thread + background thread)
# ---------------------------------------------------------------------------

class AlarmClient:
    def __init__(self, cfg: ClientConfig, fallback_hotkey: bool = False,
                 show_gui: bool = True) -> None:
        self.cfg = cfg
        self.log = _setup_logging(cfg.log_file)
        self._sound = SoundPlayer(cfg.alarm_sound)
        self._overlay = OverlayManager(
            stop_sound_cb=self._sound.stop,
            show_gui=show_gui,
            room_name=cfg.room_name,
            server_info=f"{cfg.server_ip}:{cfg.server_port}",
            stop_client_cb=self.stop,
        )
        self._core = _AsyncCore(
            cfg=cfg,
            overlay=self._overlay,
            sound=self._sound,
            log=self.log,
            fallback_hotkey=fallback_hotkey,
        )

    def run(self) -> None:
        """
        Start the asyncio core in a background thread, then run tkinter's
        mainloop on the current (main) thread.  Blocks until stop() is called.
        """
        bg = threading.Thread(
            target=self._core.run_in_thread,
            daemon=True,
            name="asyncio-core",
        )
        bg.start()

        # Blocks the main thread — required on macOS
        self._overlay.run_mainloop()

        # mainloop returned → clean up
        self._sound.stop()
        self._core.shutdown()
        bg.join(timeout=3)

    def stop(self) -> None:
        """Signal both the overlay and the async core to shut down."""
        self._sound.stop()
        self._core.shutdown()
        self._overlay.stop()   # posts _Stop → causes mainloop() to return


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Alarm System Client")
    parser.add_argument("--config",          default=None,  help="Path to client_config.toml")
    parser.add_argument("--fallback-hotkey", action="store_true",
                        help="Use terminal-input fallback hotkey (type 'a'+Enter)")
    parser.add_argument("--install",         action="store_true",
                        help="Register auto-start task and exit")
    gui_group = parser.add_mutually_exclusive_group()
    gui_group.add_argument("--gui", action="store_true", default=None,
                           help="Show status GUI (default)")
    gui_group.add_argument("--no-gui", action="store_true",
                           help="Run without status GUI")
    args = parser.parse_args()

    cfg = load_client_config(args.config)

    if args.install:
        _install_autostart(cfg)
        return

    show_gui = args.gui if args.gui is not None else (not args.no_gui)
    client = AlarmClient(cfg, fallback_hotkey=args.fallback_hotkey, show_gui=show_gui)

    def _sigint(_sig, _frame):
        print("\nShutting down…")
        client.stop()

    signal.signal(signal.SIGINT, _sigint)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _sigint)

    client.run()


def _install_autostart(cfg: ClientConfig) -> None:
    import subprocess

    scripts_dir = Path(__file__).parent.parent / "scripts"
    exe    = sys.executable
    target = Path(sys.argv[0]).resolve()

    if sys.platform == "win32":
        script = scripts_dir / "install_autostart_windows.py"
        subprocess.run([exe, str(script), "--target", str(target), "--role", "client"], check=True)
    elif sys.platform == "darwin":
        script = scripts_dir / "install_autostart_mac.py"
        subprocess.run([exe, str(script), "--target", str(target), "--role", "client"], check=True)
    else:
        print("Auto-start not supported on this platform. Configure manually.")


if __name__ == "__main__":
    main()
