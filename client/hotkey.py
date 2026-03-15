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
) -> HotkeyListener | _FallbackHotkeyListener:
    """
    Factory that returns the best available hotkey listener.

    If *fallback* is True, a terminal-based listener is returned.
    Otherwise a system-wide ``HotkeyListener`` is returned.
    """
    if fallback:
        # Use 'a' as the fallback trigger; ignore the actual hotkey string
        return _FallbackHotkeyListener(callback)
    return HotkeyListener(hotkey, callback)
