"""
hotkey.py — Global hotkey registration.

Uses the `keyboard` library which captures key events at the OS driver level,
meaning it works regardless of which application currently has focus.

On macOS the process needs Accessibility permissions (System Settings →
Privacy & Security → Accessibility).
On Windows the process should run with administrator privileges so it can
capture keys from elevated windows too.
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import Callable

log = logging.getLogger("alarm.client.hotkey")


class HotkeyListener:
    """
    Registers a system-wide hotkey and calls *callback* when it is pressed.

    Parameters
    ----------
    hotkey : str
        A combo string understood by the `keyboard` library, e.g. ``"alt+n"``.
    callback : Callable[[], None]
        Called (in the keyboard-event thread) when the hotkey fires.
    """

    def __init__(self, hotkey: str, callback: Callable[[], None]) -> None:
        self._hotkey = hotkey
        self._callback = callback
        self._registered = False
        self._suppress = False  # set True to prevent hotkey reaching other apps

    def start(self) -> None:
        """Register the hotkey. Safe to call from any thread."""
        try:
            import keyboard  # local import so unit tests can mock it

            keyboard.add_hotkey(
                self._hotkey,
                self._on_hotkey,
                suppress=self._suppress,
            )
            self._registered = True
            log.info("Global hotkey registered: %s", self._hotkey)
        except ImportError:
            log.error(
                "The 'keyboard' package is not installed. "
                "Run: pip install keyboard"
            )
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to register hotkey %r: %s", self._hotkey, exc)
            if sys.platform == "darwin":
                log.error(
                    "On macOS, Accessibility permissions are required. "
                    "Go to System Settings → Privacy & Security → Accessibility "
                    "and add this application."
                )
            elif sys.platform == "win32":
                log.error(
                    "On Windows, try running as Administrator."
                )

    def stop(self) -> None:
        """Unregister the hotkey."""
        if not self._registered:
            return
        try:
            import keyboard

            keyboard.remove_hotkey(self._hotkey)
            self._registered = False
            log.info("Hotkey unregistered: %s", self._hotkey)
        except Exception as exc:  # noqa: BLE001
            log.warning("Error unregistering hotkey: %s", exc)

    def _on_hotkey(self) -> None:
        log.debug("Hotkey %r pressed", self._hotkey)
        try:
            self._callback()
        except Exception as exc:  # noqa: BLE001
            log.error("Hotkey callback raised: %s", exc)


class MacHotkeyListener:
    """
    macOS global hotkey listener.

    Runs pynput.GlobalHotKeys in a *child process* so that any CGEventTap /
    macOS 26 instability cannot SIGTRAP the main Python process.
    The subprocess writes "TRIGGERED\\n" to stdout; a daemon reader thread
    picks that up and calls the callback safely.
    """

    _MODIFIERS = {"cmd", "ctrl", "alt", "option", "shift"}

    def __init__(self, hotkey: str, callback: Callable[[], None]) -> None:
        self._hotkey = hotkey.lower()
        self._callback = callback
        self._proc = None
        self._reader: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Format conversion: "cmd+n" → "<cmd>+n"  /  "ctrl+shift+a" → "<ctrl>+<shift>+a"
    # ------------------------------------------------------------------
    def _to_pynput(self, hotkey: str) -> str:
        parts = hotkey.lower().split("+")
        out = []
        for p in parts:
            norm = "alt" if p == "option" else p
            out.append(f"<{norm}>" if norm in self._MODIFIERS else norm)
        return "+".join(out)

    # ------------------------------------------------------------------
    # Subprocess management
    # ------------------------------------------------------------------
    def start(self) -> None:
        pynput_hk = self._to_pynput(self._hotkey)
        script = str(
            __import__("pathlib").Path(__file__).parent.parent
            / "common" / "hotkey_subprocess.py"
        )
        try:
            import subprocess
            self._proc = subprocess.Popen(
                [sys.executable, script, pynput_hk],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self._reader = threading.Thread(
                target=self._read_loop, daemon=True, name="hotkey-reader"
            )
            self._reader.start()
            log.info("macOS hotkey subprocess started for %r (pynput: %r)",
                     self._hotkey, pynput_hk)
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to start hotkey subprocess: %s", exc)

    def _read_loop(self) -> None:
        """Daemon thread: reads lines from the subprocess stdout."""
        if self._proc is None or self._proc.stdout is None:
            return
        try:
            for raw in self._proc.stdout:
                if raw.strip() == b"TRIGGERED":
                    try:
                        self._callback()
                    except Exception as exc:  # noqa: BLE001
                        log.error("Hotkey callback raised: %s", exc)
        except Exception:  # noqa: BLE001
            pass  # process ended

    def stop(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception as exc:  # noqa: BLE001
                log.warning("Error stopping hotkey subprocess: %s", exc)
            self._proc = None
        self._reader = None
        log.info("macOS hotkey subprocess stopped: %s", self._hotkey)


class _FallbackHotkeyListener:
    """
    Terminal-based fallback used during macOS simulation when the `keyboard`
    library cannot capture global events (e.g. no Accessibility permission).

    Reads a single character from stdin in a background thread and calls the
    callback when the user types the trigger character (default: 'a').
    """

    def __init__(self, callback: Callable[[], None], trigger_char: str = "a") -> None:
        self._callback = callback
        self._trigger = trigger_char
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.warning(
            "Using FALLBACK hotkey listener. "
            "Type '%s' + Enter in this terminal to trigger an alarm.",
            self._trigger,
        )

    def stop(self) -> None:
        self._running = False

    def _loop(self) -> None:
        # IMPORTANT: Do NOT use input() here.
        # When tkinter is active, Python replaces PyOS_ReadlineFunctionPointer
        # with EventHook() which calls Tcl_DoOneEvent — calling Tcl from a
        # background thread triggers Tcl_Panic / SIGTRAP on macOS.
        # Reading directly from the raw fd bypasses that hook entirely.
        import io
        raw = io.open(sys.stdin.fileno(), mode="rb", closefd=False, buffering=0)
        buf = b""
        while self._running:
            try:
                ch = raw.read(1)
                if not ch:
                    break
                if ch == b"\n":
                    line = buf.decode(errors="replace")
                    buf = b""
                    if self._trigger in line.lower():
                        self._callback()
                else:
                    buf += ch
            except Exception:  # noqa: BLE001
                break


def make_hotkey_listener(
    hotkey: str,
    callback: Callable[[], None],
    fallback: bool = False,
) -> "HotkeyListener | MacHotkeyListener | _FallbackHotkeyListener":
    """
    Factory that returns the best available hotkey listener.

    - On macOS: uses ``MacHotkeyListener`` (pynput-based, no root required).
    - On other platforms: uses ``HotkeyListener`` (keyboard-library-based).
    - If *fallback* is True: terminal stdin listener (for testing/no-permissions).
    """
    if fallback:
        return _FallbackHotkeyListener(callback)
    if sys.platform == "darwin":
        return MacHotkeyListener(hotkey, callback)
    return HotkeyListener(hotkey, callback)
