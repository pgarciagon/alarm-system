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
import sys
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional

from common.tray_icon import TrayIcon, set_window_icon

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

@dataclass
class _UpdateClientList:
    clients: list  # [{"room": str, "is_down": bool}, ...]

@dataclass
class _SetConnected:
    connected: bool

@dataclass
class _UpdateHotkey:
    hotkey: str


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

    def __init__(self, stop_sound_cb=None, show_gui: bool = True,
                 room_name: str = "", server_info: str = "",
                 stop_client_cb=None, hotkey: str = "",
                 change_hotkey_cb=None) -> None:
        self._q: queue.Queue = queue.Queue()
        self._root: Optional[tk.Tk] = None
        self._alarm_win: Optional[tk.Toplevel] = None
        self._banner_wins: Dict[str, tk.Toplevel] = {}
        self._flash_bright = True
        self._flash_job: Optional[str] = None
        self._stop_sound_cb = stop_sound_cb  # called when alarm is dismissed
        self._stop_client_cb = stop_client_cb  # called on tray "Beenden"
        self._show_gui = show_gui
        self._room_name = room_name
        self._server_info = server_info
        self._hotkey = hotkey
        self._change_hotkey_cb = change_hotkey_cb
        self._tray: Optional[TrayIcon] = None
        self._status_win: Optional[tk.Toplevel] = None
        self._status_frame: Optional[tk.Frame] = None
        self._hotkey_label: Optional[tk.Label] = None
        self._connected = False

    # ------------------------------------------------------------------
    # Thread-safe public API  (callable from any thread)
    # ------------------------------------------------------------------

    def show_alarm(self, room: str) -> None:
        self._q.put(_ShowAlarm(room=room))

    def hide_alarm(self) -> None:
        self._q.put(_HideAlarm())

    def show_banner(self, room: str, up: bool) -> None:
        self._q.put(_ShowBanner(room=room, up=up))

    def update_client_list(self, clients: list) -> None:
        self._q.put(_UpdateClientList(clients=clients))

    def set_connected(self, connected: bool) -> None:
        self._q.put(_SetConnected(connected=connected))

    def update_hotkey(self, hotkey: str) -> None:
        self._q.put(_UpdateHotkey(hotkey=hotkey))

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

        # System tray icon
        self._tray = TrayIcon(
            on_show=self._restore_from_tray,
            on_exit=self._exit_from_tray,
            name="alarm_client",
            title=f"Alarm Client — {self._room_name}",
            show_label="Status anzeigen",
            icon_color="#00b894",
            icon_file="alarm_client.ico",
        )
        self._tray.start()

        # Show status window immediately (with "waiting for server" state)
        if self._show_gui:
            self._root.after(200, self._show_initial_status)

        self._root.after(self.POLL_MS, self._poll)
        self._root.mainloop()

        # Clean up tray on exit
        if self._tray:
            self._tray.stop()
            self._tray = None

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
        elif isinstance(cmd, _UpdateClientList):
            self._connected = True
            self._update_client_list(cmd.clients)
        elif isinstance(cmd, _SetConnected):
            self._connected = cmd.connected
            self._refresh_connection_status()
        elif isinstance(cmd, _UpdateHotkey):
            self._hotkey = cmd.hotkey
            self._refresh_hotkey_label()
        elif isinstance(cmd, _Stop):
            if self._tray:
                self._tray.stop()
                self._tray = None
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
                    text=f"Ausgelöst um {datetime.now().strftime('%H:%M:%S')}"
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
        win.title("NOTFALL")
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
            text=f"Ausgelöst um {timestamp}",
            font=("Arial", 28),
            bg=_RED_BRIGHT,
            fg=_WHITE,
        )
        time_lbl.pack()

        # Non-blinking dismiss button — use a frame to isolate it from flash
        btn_frame = tk.Frame(win, bg=_WHITE, padx=3, pady=3)
        btn_frame.pack(pady=40)
        dismiss_btn = tk.Button(
            btn_frame,
            text="BESTÄTIGEN  (ESC)",
            font=("Arial", 24, "bold"),
            bg=_WHITE,
            fg=_RED_BRIGHT,
            activebackground="#dddddd",
            activeforeground=_RED_BRIGHT,
            relief="raised",
            bd=2,
            padx=30,
            pady=10,
            cursor="hand2",
            command=self._hide_alarm,
        )
        dismiss_btn.pack()

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
                    # Skip the button frame — it should stay white
                    if isinstance(widget, tk.Frame):
                        continue
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
            f"Alarmsystem WIEDERHERGESTELLT in {room}"
            if up else
            f"Alarmsystem NICHT VERFÜGBAR in {room}"
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

    # ------------------------------------------------------------------
    # Client status window
    # ------------------------------------------------------------------

    _ST_BG = "#1a1a2e"
    _ST_FG = "#e0e0e0"
    _ST_HEADER_BG = "#16213e"
    _ST_GREEN = "#00b894"
    _ST_RED = "#e94560"

    def _show_initial_status(self) -> None:
        """Show the status window immediately on startup."""
        if self._status_win is None or not self._status_win.winfo_exists():
            self._build_status_window()
        self._refresh_connection_status()

    def _update_client_list(self, clients: list) -> None:
        if not self._show_gui:
            return
        if self._status_win is None or not self._status_win.winfo_exists():
            self._build_status_window()
        self._update_status_content(clients)

    def _build_status_window(self) -> None:
        root = self._root
        assert root is not None

        win = tk.Toplevel(root)
        win.title("Alarmsystem \u2014 Status")
        set_window_icon(win, "alarm_client.ico")
        win.configure(bg=self._ST_BG)
        win.geometry("350x340")
        win.resizable(True, True)
        # Hide from taskbar — Windows only (-toolwindow not supported on macOS)
        if sys.platform == "win32":
            win.attributes("-toolwindow", True)
        win.protocol("WM_DELETE_WINDOW", self._minimize_status)

        # Header
        header = tk.Frame(win, bg=self._ST_HEADER_BG, pady=8)
        header.pack(fill="x")

        # Title row with buttons
        title_row = tk.Frame(header, bg=self._ST_HEADER_BG)
        title_row.pack(fill="x", padx=10)

        tk.Label(
            title_row, text=f"Raum: {self._room_name}",
            font=("Arial", 12, "bold"), bg=self._ST_HEADER_BG, fg=self._ST_FG,
        ).pack(side="left")

        tk.Button(
            title_row, text="Beenden", font=("Arial", 9),
            bg=self._ST_RED, fg="white", relief="flat", padx=10, pady=2,
            cursor="hand2", command=self._exit_from_tray,
        ).pack(side="right", padx=(4, 0))

        tk.Button(
            title_row, text="Ausblenden", font=("Arial", 9),
            bg="#0f3460", fg=self._ST_FG, relief="flat", padx=10, pady=2,
            cursor="hand2", command=self._minimize_status,
        ).pack(side="right")

        # Info row: server + hotkey
        info_row = tk.Frame(header, bg=self._ST_HEADER_BG)
        info_row.pack(fill="x", padx=10, pady=(4, 0))

        if self._server_info:
            tk.Label(
                info_row, text=f"Server: {self._server_info}",
                font=("Arial", 9), bg=self._ST_HEADER_BG, fg="#888888",
            ).pack(side="left")

        if self._hotkey:
            self._hotkey_label = tk.Label(
                info_row, text=f"Tastenkürzel: {self._hotkey.upper()}",
                font=("Arial", 9, "bold"), bg=self._ST_HEADER_BG, fg="#fdcb6e",
                cursor="hand2",
            )
            self._hotkey_label.pack(side="right")
            self._hotkey_label.bind("<Button-1>", lambda _: self._edit_hotkey_dialog())

        # Connection status label
        self._conn_label = tk.Label(
            win, text="", font=("Arial", 10), bg=self._ST_BG, fg="#888888",
            anchor="w", padx=10, pady=4,
        )
        self._conn_label.pack(fill="x")

        # Separator
        tk.Frame(win, bg="#333333", height=1).pack(fill="x")

        # Scrollable client list area
        container = tk.Frame(win, bg=self._ST_BG)
        container.pack(fill="both", expand=True, padx=10, pady=(8, 4))

        self._status_frame = tk.Frame(container, bg=self._ST_BG)
        self._status_frame.pack(fill="both", expand=True)

        # Footer (count)
        self._status_footer = tk.Label(
            win, text="", font=("Arial", 9), bg=self._ST_BG, fg="#888888",
            anchor="w", padx=10, pady=4,
        )
        self._status_footer.pack(fill="x", side="bottom")

        self._status_win = win

    def _refresh_connection_status(self) -> None:
        """Update the connection status label in the status window."""
        if not hasattr(self, '_conn_label') or self._conn_label is None:
            return
        if self._connected:
            self._conn_label.config(
                text="\u25cf Verbunden", fg=self._ST_GREEN,
            )
        else:
            self._conn_label.config(
                text="\u25cf Warte auf Server\u2026", fg="#fdcb6e",
            )

    def _refresh_hotkey_label(self) -> None:
        """Update the hotkey label text."""
        if self._hotkey_label and self._hotkey_label.winfo_exists():
            self._hotkey_label.config(text=f"Tastenkürzel: {self._hotkey.upper()}")

    def _edit_hotkey_dialog(self) -> None:
        """Open a small dialog to edit the hotkey."""
        if not self._status_win:
            return
        dlg = tk.Toplevel(self._status_win)
        dlg.title("Tastenkürzel ändern")
        dlg.configure(bg=self._ST_BG)
        dlg.geometry("300x120")
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)
        dlg.transient(self._status_win)
        dlg.grab_set()

        tk.Label(
            dlg, text="Neues Tastenkürzel:", font=("Arial", 10),
            bg=self._ST_BG, fg=self._ST_FG,
        ).pack(pady=(15, 5))

        var = tk.StringVar(value=self._hotkey)
        entry = tk.Entry(dlg, textvariable=var, font=("Arial", 11), width=18, justify="center")
        entry.pack()
        entry.select_range(0, tk.END)
        entry.focus_set()

        def _apply():
            new_hk = var.get().strip()
            if new_hk and new_hk != self._hotkey:
                self._hotkey = new_hk
                self._refresh_hotkey_label()
                if self._change_hotkey_cb:
                    self._change_hotkey_cb(new_hk)
            dlg.destroy()

        entry.bind("<Return>", lambda _: _apply())
        tk.Button(
            dlg, text="Übernehmen", font=("Arial", 10, "bold"),
            bg="#00b894", fg="white", relief="flat", padx=15, pady=4,
            cursor="hand2", command=_apply,
        ).pack(pady=10)

    def _minimize_status(self) -> None:
        """Hide the status window (keep running in tray)."""
        if self._status_win and self._status_win.winfo_exists():
            self._status_win.withdraw()

    def _update_status_content(self, clients: list) -> None:
        frame = self._status_frame
        if frame is None:
            return

        # Clear existing content
        for widget in frame.winfo_children():
            widget.destroy()

        if not clients:
            tk.Label(
                frame, text="Keine weiteren Clients verbunden",
                font=("Arial", 10), bg=self._ST_BG, fg="#888888",
            ).pack(pady=20)
            self._status_footer.config(text="Andere Clients: 0/0")
            return

        # Filter out this client's own room — only show others
        others = [c for c in clients if c.get("room", "") != self._room_name]

        if not others:
            tk.Label(
                frame, text="Keine weiteren Clients verbunden",
                font=("Arial", 10), bg=self._ST_BG, fg="#888888",
            ).pack(pady=20)
            self._status_footer.config(text="Andere Clients: 0/0")
            return

        online_count = 0
        for c in sorted(others, key=lambda x: x.get("room", "")):
            room = c.get("room", "?")
            is_down = c.get("is_down", False)
            if not is_down:
                online_count += 1

            row = tk.Frame(frame, bg=self._ST_BG)
            row.pack(fill="x", pady=2)

            # Status indicator (colored circle via unicode)
            color = self._ST_RED if is_down else self._ST_GREEN
            status_text = "Offline" if is_down else "Online"

            tk.Label(
                row, text="\u25cf", font=("Arial", 14),
                bg=self._ST_BG, fg=color,
            ).pack(side="left", padx=(0, 6))

            tk.Label(
                row, text=room, font=("Arial", 11, "bold"),
                bg=self._ST_BG, fg=self._ST_FG,
            ).pack(side="left")

            tk.Label(
                row, text=status_text, font=("Arial", 9),
                bg=self._ST_BG, fg=color,
            ).pack(side="right", padx=(0, 4))

        total = len(others)
        self._status_footer.config(
            text=f"Andere Clients: {online_count}/{total}"
        )

    # ------------------------------------------------------------------
    # Tray integration
    # ------------------------------------------------------------------

    def _restore_from_tray(self) -> None:
        """Restore the status window from tray. Called from pystray thread."""
        if self._root and self._status_win and self._status_win.winfo_exists():
            self._root.after(0, self._status_win.deiconify)
        elif self._root and self._show_gui:
            self._root.after(0, self._show_initial_status)

    def _exit_from_tray(self) -> None:
        """Full shutdown from tray exit menu. Called from pystray thread."""
        if self._stop_client_cb:
            self._stop_client_cb()
        else:
            self.stop()
