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

from common.config import ClientConfig, load_client_config, save_client_config
from common.protocol import (
    AlarmMsg,
    ClientDownMsg,
    ClientUpMsg,
    HeartbeatMsg,
    RegisterMsg,
    SetHotkeyMsg,
    SetRoomNameMsg,
    decode,
    encode,
    MSG_ALARM,
    MSG_CLIENT_DOWN,
    MSG_CLIENT_LIST,
    MSG_CLIENT_UP,
    MSG_SET_HOTKEY,
    MSG_SET_ROOM_NAME,
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
        self._ws = None  # current websocket (set while connected)
        self._running = True

    # ------------------------------------------------------------------
    # Entry point — called from the asyncio background thread
    # ------------------------------------------------------------------

    def run_in_thread(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except RuntimeError as exc:
            # "Event loop stopped before Future completed" is expected when
            # shutdown() calls loop.stop() while coroutines are still pending.
            if "Event loop stopped before Future completed" not in str(exc):
                self.log.exception("Unexpected asyncio error: %s", exc)
        finally:
            # Cancel any remaining tasks so the loop closes cleanly
            try:
                pending = asyncio.all_tasks(self._loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self._loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:
                pass
            self._loop.close()

    async def _main(self) -> None:
        self._alarm_pending = asyncio.Event()
        self._start_hotkey()
        await self._connect_loop()

    # ------------------------------------------------------------------
    # Hotkey
    # ------------------------------------------------------------------

    def _start_hotkey(self) -> None:
        self._hotkey_listener = make_hotkey_listener(
            self.cfg.hotkey,
            callback=self._on_hotkey_pressed,
            fallback=self._fallback_hotkey,
        )
        self._hotkey_listener.start()
        self.log.info("Hotkey listener started (%s)", self.cfg.hotkey)

    def _restart_hotkey(self, new_hotkey: str) -> None:
        """Change the global hotkey at runtime."""
        if hasattr(self, '_hotkey_listener'):
            self._hotkey_listener.stop()
        self.cfg.hotkey = new_hotkey
        self._hotkey_listener = make_hotkey_listener(
            new_hotkey,
            callback=self._on_hotkey_pressed,
            fallback=self._fallback_hotkey,
        )
        self._hotkey_listener.start()
        self.log.info("Hotkey changed to %s", new_hotkey)
        save_client_config(self.cfg)

    def _on_hotkey_pressed(self) -> None:
        self.log.info("Hotkey pressed — queuing alarm for room %r", self.cfg.room_name)
        if self._loop and self._alarm_pending:
            self._loop.call_soon_threadsafe(self._alarm_pending.set)

    # ------------------------------------------------------------------
    # Connection loop
    # ------------------------------------------------------------------

    async def _connect_loop(self) -> None:
        attempt = 0
        self._reconnect_event = asyncio.Event()

        while self._running:
            # Re-read server address each iteration so reconnect_to() takes effect
            uri = f"ws://{self.cfg.server_ip}:{self.cfg.server_port}"
            delay = self._BACKOFF[min(attempt, len(self._BACKOFF) - 1)]
            if attempt > 0:
                self.log.info("Reconnecting in %ds (attempt %d)…", delay, attempt)
                try:
                    await asyncio.wait_for(self._reconnect_event.wait(), timeout=delay)
                    self.log.info("Reconnect forced by user")
                except asyncio.TimeoutError:
                    pass
            self._reconnect_event.clear()

            try:
                self.log.info("Connecting to %s…", uri)
                async with websockets.connect(
                    uri,
                    ping_interval=20,
                    ping_timeout=30,
                    open_timeout=10,
                ) as ws:
                    self._ws = ws
                    attempt = 0
                    self.log.info("Connected to server")
                    self._overlay.set_connected(True)
                    await ws.send(encode(RegisterMsg(room=self.cfg.room_name, hotkey=self.cfg.hotkey)))
                    await asyncio.gather(
                        self._receive_loop(ws),
                        self._heartbeat_loop(ws),
                        self._alarm_send_loop(ws),
                    )

            except (ConnectionClosed, WebSocketException, OSError) as exc:
                self.log.warning("Connection lost: %s", exc)
                self._ws = None
                self._overlay.set_connected(False)
            except asyncio.CancelledError:
                break
            finally:
                attempt += 1

    def reconnect_to(self, new_ip: str) -> None:
        """Switch server IP and force an immediate reconnect (thread-safe)."""
        self.cfg.server_ip = new_ip
        save_client_config(self.cfg)
        self.log.info("Server changed to %s — forcing reconnect", new_ip)
        if self._loop and self._loop.is_running() and hasattr(self, "_reconnect_event"):
            self._loop.call_soon_threadsafe(self._reconnect_event.set)

    def send_register_update(self) -> None:
        """Re-send RegisterMsg on the current connection (thread-safe)."""
        ws = self._ws
        if ws and self._loop and self._loop.is_running():
            async def _send():
                try:
                    await ws.send(encode(RegisterMsg(room=self.cfg.room_name, hotkey=self.cfg.hotkey)))
                    self.log.info("Sent register update (room=%r, hotkey=%r)", self.cfg.room_name, self.cfg.hotkey)
                except Exception:
                    pass
            self._loop.call_soon_threadsafe(self._loop.create_task, _send())

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
            elif msg.type == MSG_SET_HOTKEY:
                new_hotkey = msg.hotkey  # type: ignore[union-attr]
                self.log.info("Hotkey changed to %r by server", new_hotkey)
                self.cfg.hotkey = new_hotkey
                save_client_config(self.cfg)
                self._overlay.update_hotkey(new_hotkey)
                self._restart_hotkey(new_hotkey)
            elif msg.type == MSG_SET_ROOM_NAME:
                new_name = msg.new_name  # type: ignore[union-attr]
                self.log.info("Room name changed to %r by server", new_name)
                self.cfg.room_name = new_name
                save_client_config(self.cfg)
                self._overlay.update_room_name(new_name)
                # Re-register under the new name
                await ws.send(encode(RegisterMsg(room=new_name, hotkey=self.cfg.hotkey)))

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
            # Cancel all running tasks, then stop the loop
            def _cancel_and_stop():
                for task in asyncio.all_tasks(self._loop):
                    task.cancel()
                self._loop.stop()
            self._loop.call_soon_threadsafe(_cancel_and_stop)


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
            hotkey=cfg.hotkey,
            change_hotkey_cb=self._on_hotkey_changed,
            change_room_name_cb=self._on_room_name_changed,
            reconnect_cb=self._on_server_changed,
            toggle_mute_cb=self._on_toggle_mute,
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

    def _on_hotkey_changed(self, new_hotkey: str) -> None:
        """Called from GUI when user edits hotkey locally."""
        self._core._restart_hotkey(new_hotkey)
        self._core.send_register_update()

    def _on_room_name_changed(self, new_name: str) -> None:
        """Called from GUI when user edits room name locally."""
        self._core.cfg.room_name = new_name
        save_client_config(self._core.cfg)
        self._core.send_register_update()

    def _on_server_changed(self, new_ip: str) -> None:
        """Called from GUI after user picks a server from the scan dialog."""
        self._overlay.update_server_info(f"{new_ip}:{self.cfg.server_port}")
        self._core.reconnect_to(new_ip)

    def _on_toggle_mute(self, muted: bool) -> None:
        """Called from GUI when user toggles silent mode."""
        self._sound.set_muted(muted)

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
