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
    macOS hotkey listener using pynput.keyboard.Listener (CGEventTap in a
    background thread).  Avoids GlobalHotKeys which starts its own NSRunLoop
    and crashes when tkinter already owns the main thread.
    """

    # Map user-facing modifier names → pynput Key sets
    _MOD_NAMES = ("cmd", "ctrl", "alt", "option", "shift")

    def __init__(self, hotkey: str, callback: Callable[[], None]) -> None:
        self._hotkey = hotkey.lower()
        self._callback = callback
        self._listener = None
        self._pressed: set = set()
        self._target_mods: frozenset = frozenset()
        self._target_key: str = ""
        self._parse_hotkey()

    def _parse_hotkey(self) -> None:
        parts = self._hotkey.split("+")
        mods = [p for p in parts[:-1] if p in self._MOD_NAMES]
        # normalise "option" → "alt"
        mods = ["alt" if m == "option" else m for m in mods]
        self._target_mods = frozenset(mods)
        self._target_key = parts[-1]

    def _mod_name(self, key) -> str | None:
        try:
            from pynput.keyboard import Key
            if key in (Key.cmd, Key.cmd_l, Key.cmd_r):     return "cmd"
            if key in (Key.ctrl, Key.ctrl_l, Key.ctrl_r):  return "ctrl"
            if key in (Key.alt, Key.alt_l, Key.alt_r):     return "alt"
            if key in (Key.shift, Key.shift_l, Key.shift_r): return "shift"
        except Exception:  # noqa: BLE001
            pass
        return None

    def _key_name(self, key) -> str | None:
        try:
            from pynput.keyboard import KeyCode
            if isinstance(key, KeyCode) and key.char:
                return key.char.lower()
            return key.name.lower()
        except Exception:  # noqa: BLE001
            return None

    def _on_press(self, key) -> None:
        mod = self._mod_name(key)
        if mod:
            self._pressed.add(mod)
        else:
            if self._pressed == self._target_mods:
                if self._key_name(key) == self._target_key:
                    try:
                        self._callback()
                    except Exception as exc:  # noqa: BLE001
                        log.error("Hotkey callback raised: %s", exc)

    def _on_release(self, key) -> None:
        mod = self._mod_name(key)
        if mod:
            self._pressed.discard(mod)

    def start(self) -> None:
        try:
            from pynput.keyboard import Listener
            self._listener = Listener(
                on_press=self._on_press,
                on_release=self._on_release,
            )
            self._listener.start()
            log.info("macOS hotkey registered via pynput Listener: %s", self._hotkey)
        except ImportError:
            log.error("pynput is not installed. Run: pip install pynput")
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to register macOS hotkey %r: %s", self._hotkey, exc)

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception as exc:  # noqa: BLE001
                log.warning("Error stopping pynput listener: %s", exc)
            self._listener = None
            log.info("macOS hotkey unregistered: %s", self._hotkey)


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
