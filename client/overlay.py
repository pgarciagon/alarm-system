"""
overlay.py — Full-screen alarm overlay using tkinter.

macOS AppKit rule: NSWindow (and therefore tkinter) MUST be driven on the
main OS thread.  This module therefore does NOT own a background thread.
Instead the caller is responsible for running tkinter on the main thread
(see client.py for how asyncio is moved to a background thread instead).

Thread-safe API: any thread can call show_alarm(), hide_alarm(),
show_banner() and stop() — they post commands through a queue; the main
thread drains the queue via a tkinter `after` callback.
"""

from __future__ import annotations

import logging
import queue
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional

log = logging.getLogger("alarm.client.overlay")

# ---------------------------------------------------------------------------
# Colour constants
# ---------------------------------------------------------------------------

_RED_BRIGHT = "#CC0000"
_RED_DARK   = "#7A0000"
_AMBER      = "#CC7700"
_WHITE      = "#FFFFFF"
_GREEN      = "#006600"

# ---------------------------------------------------------------------------
# Internal command objects
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
    up: bool

@dataclass
class _Stop:
    pass


# ---------------------------------------------------------------------------
# Overlay manager  (main-thread only)
# ---------------------------------------------------------------------------

class OverlayManager:
    """
    Drives the tkinter alarm overlay.

    Must be created and used via ``run_mainloop()`` on the main thread.
    Other threads call the public methods (show_alarm, hide_alarm, …)
    which are thread-safe — they push commands into a queue that the
    main-thread poll loop drains every 50 ms.
    """

    POLL_MS  = 50
    FLASH_MS = 500

    def __init__(self, stop_sound_cb=None) -> None:
        self._q: queue.Queue = queue.Queue()
        self._root: Optional[tk.Tk] = None
        self._alarm_win: Optional[tk.Toplevel] = None
        self._banner_wins: Dict[str, tk.Toplevel] = {}
        self._flash_bright = True
        self._flash_job: Optional[str] = None
        self._stop_sound_cb = stop_sound_cb  # called when alarm is dismissed

    # ------------------------------------------------------------------
    # Thread-safe public API  (callable from any thread)
    # ------------------------------------------------------------------

    def show_alarm(self, room: str) -> None:
        self._q.put(_ShowAlarm(room=room))

    def hide_alarm(self) -> None:
        self._q.put(_HideAlarm())

    def show_banner(self, room: str, up: bool) -> None:
        self._q.put(_ShowBanner(room=room, up=up))

    def stop(self) -> None:
        self._q.put(_Stop())

    # ------------------------------------------------------------------
    # Main-thread entry point
    # ------------------------------------------------------------------

    def run_mainloop(self) -> None:
        """
        Initialise tkinter and run the event loop.
        Blocks until stop() is called.
        Must be called from the main thread.
        """
        self._root = tk.Tk()
        # On macOS 26+ (Tahoe) withdraw() before mainloop() triggers
        # 'Tcl_WaitForEvent: Notifier not initialized' because the
        # NSRunLoop hasn't started yet. Instead we move the window
        # far off-screen and make it transparent/tiny, then hide it
        # properly once the loop is running.
        self._root.geometry("1x1+-10000+-10000")
        self._root.attributes("-alpha", 0.0)
        self._root.after(100, self._root.withdraw)  # safe to withdraw after loop starts
        self._root.after(self.POLL_MS, self._poll)
        self._root.mainloop()

    # ------------------------------------------------------------------
    # Internal poll (main thread only)
    # ------------------------------------------------------------------

    def _poll(self) -> None:
        try:
            while True:
                cmd = self._q.get_nowait()
                self._dispatch(cmd)
                if isinstance(cmd, _Stop):
                    return   # do not reschedule after quit
        except queue.Empty:
            pass
        if self._root:
            self._root.after(self.POLL_MS, self._poll)

    def _dispatch(self, cmd) -> None:
        if isinstance(cmd, _ShowAlarm):
            self._show_alarm(cmd.room)
        elif isinstance(cmd, _HideAlarm):
            self._hide_alarm()
        elif isinstance(cmd, _ShowBanner):
            self._show_banner(cmd.room, cmd.up)
        elif isinstance(cmd, _Stop):
            if self._root:
                self._root.quit()

    # ------------------------------------------------------------------
    # Alarm overlay
    # ------------------------------------------------------------------

    def _show_alarm(self, room: str) -> None:
        if self._alarm_win and self._alarm_win.winfo_exists():
            try:
                self._alarm_win._room_label.config(  # type: ignore[attr-defined]
                    text=f"\u26a0  ALARM \u2014 {room.upper()}  \u26a0"
                )
                self._alarm_win._time_label.config(  # type: ignore[attr-defined]
                    text=f"Triggered at {datetime.now().strftime('%H:%M:%S')}"
                )
            except Exception:
                pass
            return

        root = self._root
        assert root is not None

        # Use root for screen dimensions — it's already mapped and reliable.
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()

        win = tk.Toplevel(root)
        win.title("ALARM")
        # On macOS, -fullscreen True conflicts with overrideredirect; use
        # explicit geometry to cover the full screen instead.
        win.geometry(f"{sw}x{sh}+0+0")
        win.attributes("-topmost", True)
        win.configure(bg=_RED_BRIGHT)
        win.protocol("WM_DELETE_WINDOW", lambda: None)
        win.lift()
        win.focus_force()

        timestamp = datetime.now().strftime("%H:%M:%S")

        room_lbl = tk.Label(
            win,
            text=f"\u26a0  ALARM \u2014 {room.upper()}  \u26a0",
            font=("Arial", 72, "bold"),
            bg=_RED_BRIGHT,
            fg=_WHITE,
            wraplength=sw - 100,
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

        win._room_label = room_lbl  # type: ignore[attr-defined]
        win._time_label = time_lbl  # type: ignore[attr-defined]

        win.bind("<Escape>", lambda _e: self._hide_alarm())

        self._alarm_win = win
        self._flash_bright = True
        self._start_flash()
        log.info("Alarm overlay shown for room %r", room)

    def _hide_alarm(self) -> None:
        if self._flash_job and self._root:
            try:
                self._root.after_cancel(self._flash_job)
            except Exception:
                pass
            self._flash_job = None

        if self._alarm_win and self._alarm_win.winfo_exists():
            self._alarm_win.destroy()
            self._alarm_win = None
            log.info("Alarm overlay dismissed")
            if self._stop_sound_cb:
                try:
                    self._stop_sound_cb()
                except Exception:
                    pass

    def _start_flash(self) -> None:
        if not self._alarm_win or not self._alarm_win.winfo_exists() or not self._root:
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
        self._flash_job = self._root.after(self.FLASH_MS, self._start_flash)

    # ------------------------------------------------------------------
    # Status banner
    # ------------------------------------------------------------------

    def _show_banner(self, room: str, up: bool) -> None:
        existing = self._banner_wins.get(room)
        if existing:
            try:
                existing.destroy()
            except Exception:
                pass
            self._banner_wins.pop(room, None)

        bg  = _GREEN if up else _AMBER
        msg = (
            f"Alert system RESTORED on {room}"
            if up else
            f"Alert system NOT WORKING on {room}"
        )

        root = self._root
        assert root is not None

        # Query screen dimensions from root (already mapped) — querying from
        # a freshly-created unmapped Toplevel can return 0 on macOS.
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        banner_h = 60
        # Calculate offset *before* adding to dict so stacking is correct.
        offset   = len(self._banner_wins) * (banner_h + 5)

        win = tk.Toplevel(root)
        win.title("")
        win.attributes("-topmost", True)
        win.overrideredirect(True)
        win.configure(bg=bg)
        win.geometry(f"500x{banner_h}+{screen_w - 510}+{screen_h - 80 - offset}")

        tk.Label(
            win,
            text=msg,
            font=("Arial", 14, "bold"),
            bg=bg,
            fg=_WHITE,
            padx=10,
        ).pack(expand=True, fill="both")

        win.lift()
        win.update_idletasks()

        self._banner_wins[room] = win
        log.log(logging.INFO if up else logging.WARNING, "Banner: %s", msg)

        # Always auto-dismiss after a few seconds so banners don't pile up.
        dismiss_ms = 5000 if up else 8000
        root.after(dismiss_ms, lambda: self._remove_banner(room))

    def _remove_banner(self, room: str) -> None:
        win = self._banner_wins.pop(room, None)
        if win:
            try:
                win.destroy()
            except Exception:
                pass
