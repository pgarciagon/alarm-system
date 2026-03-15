"""
overlay.py — Full-screen alarm overlay using tkinter.

The overlay is always-on-top, covers the entire screen, flashes red/dark-red,
and displays the room name.  A small amber banner shows client-down warnings.

The overlay runs in its OWN thread because tkinter must be driven from the
thread that created it.  All external calls go through thread-safe queues.
"""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

log = logging.getLogger("alarm.client.overlay")

# ---------------------------------------------------------------------------
# Colour constants
# ---------------------------------------------------------------------------

_RED_BRIGHT = "#CC0000"
_RED_DARK   = "#7A0000"
_AMBER      = "#CC7700"
_WHITE      = "#FFFFFF"
_BLACK      = "#000000"

# ---------------------------------------------------------------------------
# Internal command objects (sent via queue to the overlay thread)
# ---------------------------------------------------------------------------

@dataclass
class _ShowAlarm:
    room: str

@dataclass
class _HideAlarm:
    pass

@dataclass
class _ShowBanner:
    room: str
    up: bool   # True → client_up  (green), False → client_down (amber)

@dataclass
class _Stop:
    pass


# ---------------------------------------------------------------------------
# Overlay manager
# ---------------------------------------------------------------------------

class OverlayManager:
    """
    Manages the tkinter overlay in a dedicated daemon thread.

    Usage::

        mgr = OverlayManager()
        mgr.start()
        mgr.show_alarm("Room 3")
        mgr.hide_alarm()
        mgr.show_banner("Room 5", up=False)
        mgr.stop()
    """

    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="overlay-thread"
        )
        self._thread.start()

    def stop(self) -> None:
        self._q.put(_Stop())
        if self._thread:
            self._thread.join(timeout=3)

    def show_alarm(self, room: str) -> None:
        self._q.put(_ShowAlarm(room=room))

    def hide_alarm(self) -> None:
        self._q.put(_HideAlarm())

    def show_banner(self, room: str, up: bool) -> None:
        self._q.put(_ShowBanner(room=room, up=up))

    # ------------------------------------------------------------------
    # Overlay thread
    # ------------------------------------------------------------------

    def _run(self) -> None:
        root = tk.Tk()
        root.withdraw()   # hidden root, just to anchor child windows

        state = _OverlayState(root, self._q)
        state.poll()
        root.mainloop()

    # ------------------------------------------------------------------
    # Public convenience: wait for the thread to be ready (optional)
    # ------------------------------------------------------------------

    def join(self) -> None:
        if self._thread:
            self._thread.join()


# ---------------------------------------------------------------------------
# Internal state (lives inside the overlay thread)
# ---------------------------------------------------------------------------

class _OverlayState:
    FLASH_INTERVAL_MS = 500
    POLL_INTERVAL_MS  = 50    # how often we check the command queue

    def __init__(self, root: tk.Tk, q: queue.Queue) -> None:
        self._root = root
        self._q = q
        self._alarm_win: Optional[tk.Toplevel] = None
        self._banner_wins: dict[str, tk.Toplevel] = {}
        self._flash_bright = True
        self._flash_job: Optional[str] = None

    # ------------------------------------------------------------------
    # Queue poll
    # ------------------------------------------------------------------

    def poll(self) -> None:
        """Drain the command queue and schedule the next poll."""
        try:
            while True:
                cmd = self._q.get_nowait()
                self._dispatch(cmd)
        except queue.Empty:
            pass

        self._root.after(self.POLL_INTERVAL_MS, self.poll)

    def _dispatch(self, cmd) -> None:
        if isinstance(cmd, _ShowAlarm):
            self._show_alarm(cmd.room)
        elif isinstance(cmd, _HideAlarm):
            self._hide_alarm()
        elif isinstance(cmd, _ShowBanner):
            self._show_banner(cmd.room, cmd.up)
        elif isinstance(cmd, _Stop):
            self._root.quit()

    # ------------------------------------------------------------------
    # Alarm overlay
    # ------------------------------------------------------------------

    def _show_alarm(self, room: str) -> None:
        if self._alarm_win and self._alarm_win.winfo_exists():
            # Already showing — just update the room name label
            try:
                self._alarm_win._room_label.config(  # type: ignore[attr-defined]
                    text=f"\u26a0  ALARM — {room.upper()}  \u26a0"
                )
                self._alarm_win._time_label.config(  # type: ignore[attr-defined]
                    text=f"Triggered at {datetime.now().strftime('%H:%M:%S')}"
                )
            except Exception:
                pass
            return

        win = tk.Toplevel(self._root)
        win.title("ALARM")

        # Full-screen, always on top, no window decorations
        win.attributes("-fullscreen", True)
        win.attributes("-topmost", True)
        win.overrideredirect(True)
        win.configure(bg=_RED_BRIGHT)

        # Prevent the window from being closed by the OS (e.g. Alt+F4 on Win)
        win.protocol("WM_DELETE_WINDOW", lambda: None)

        timestamp = datetime.now().strftime("%H:%M:%S")

        # Main alarm label
        room_lbl = tk.Label(
            win,
            text=f"\u26a0  ALARM — {room.upper()}  \u26a0",
            font=("Arial", 72, "bold"),
            bg=_RED_BRIGHT,
            fg=_WHITE,
            wraplength=win.winfo_screenwidth() - 100,
            justify="center",
        )
        room_lbl.pack(expand=True)

        time_lbl = tk.Label(
            win,
            text=f"Triggered at {timestamp}",
            font=("Arial", 28),
            bg=_RED_BRIGHT,
            fg=_WHITE,
        )
        time_lbl.pack()

        dismiss_btn = tk.Button(
            win,
            text="DISMISS  (ESC)",
            font=("Arial", 24, "bold"),
            bg=_WHITE,
            fg=_RED_BRIGHT,
            relief="flat",
            padx=30,
            pady=10,
            command=self._hide_alarm,
        )
        dismiss_btn.pack(pady=40)

        # Attach references for later updates
        win._room_label = room_lbl  # type: ignore[attr-defined]
        win._time_label = time_lbl  # type: ignore[attr-defined]

        # ESC to dismiss
        win.bind("<Escape>", lambda _e: self._hide_alarm())
        win.bind("<Button-1>", lambda _e: None)   # absorb stray clicks

        self._alarm_win = win
        self._flash_bright = True
        self._start_flash()

        log.info("Alarm overlay shown for room %r", room)

    def _hide_alarm(self) -> None:
        if self._flash_job:
            try:
                self._root.after_cancel(self._flash_job)
            except Exception:
                pass
            self._flash_job = None

        if self._alarm_win and self._alarm_win.winfo_exists():
            self._alarm_win.destroy()
            self._alarm_win = None
            log.info("Alarm overlay dismissed")

    def _start_flash(self) -> None:
        if not self._alarm_win or not self._alarm_win.winfo_exists():
            return
        colour = _RED_BRIGHT if self._flash_bright else _RED_DARK
        self._flash_bright = not self._flash_bright
        try:
            self._alarm_win.configure(bg=colour)
            for widget in self._alarm_win.winfo_children():
                try:
                    widget.configure(bg=colour)
                except Exception:
                    pass
        except Exception:
            return
        self._flash_job = self._root.after(self.FLASH_INTERVAL_MS, self._start_flash)

    # ------------------------------------------------------------------
    # Status banner (client_down / client_up)
    # ------------------------------------------------------------------

    def _show_banner(self, room: str, up: bool) -> None:
        # Destroy existing banner for this room if any
        existing = self._banner_wins.get(room)
        if existing:
            try:
                existing.destroy()
            except Exception:
                pass

        if up:
            msg = f"Alert system RESTORED on {room}"
            bg = "#006600"
        else:
            msg = f"Alert system NOT WORKING on {room}"
            bg = _AMBER

        win = tk.Toplevel(self._root)
        win.title("")
        win.attributes("-topmost", True)
        win.overrideredirect(True)
        win.configure(bg=bg)

        # Position in bottom-right corner, stacking upward
        screen_w = win.winfo_screenwidth()
        screen_h = win.winfo_screenheight()
        banner_h = 60
        offset = len(self._banner_wins) * (banner_h + 5)
        win.geometry(f"500x{banner_h}+{screen_w - 510}+{screen_h - 80 - offset}")

        lbl = tk.Label(
            win,
            text=msg,
            font=("Arial", 14, "bold"),
            bg=bg,
            fg=_WHITE,
            padx=10,
        )
        lbl.pack(expand=True, fill="both")

        self._banner_wins[room] = win

        if up:
            # Auto-dismiss "restored" banners after 5 s
            self._root.after(5000, lambda: self._remove_banner(room))
        else:
            log.warning("Banner: %s", msg)

    def _remove_banner(self, room: str) -> None:
        win = self._banner_wins.pop(room, None)
        if win:
            try:
                win.destroy()
            except Exception:
                pass
