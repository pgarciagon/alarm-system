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
from common.version import __version__

log = logging.getLogger("alarm.client.overlay")


def _make_btn(parent, text, bg, fg, command, font=("Arial", 9), padx=10, pady=2, **kw):
    """Canvas-based button — tk.Canvas always honours bg/fg on macOS Aqua theme."""
    # Measure the text to size the canvas correctly
    _probe = tk.Label(parent, text=text, font=font)
    _probe.update_idletasks()
    tw, th = _probe.winfo_reqwidth(), _probe.winfo_reqheight()
    _probe.destroy()
    w, h = tw + padx * 2, th + pady * 2
    canvas = tk.Canvas(parent, bg=bg, width=w, height=h,
                       highlightthickness=0, bd=0, cursor="hand2")
    canvas.create_text(w // 2, h // 2, text=text, font=font, fill=fg, tags="txt")
    canvas.bind("<Button-1>", lambda _e: command())
    canvas.bind("<Enter>",    lambda _e: canvas.config(bg=_darken(bg)))
    canvas.bind("<Leave>",    lambda _e: canvas.config(bg=bg))
    return canvas


def _darken(hex_color: str) -> str:
    """Return a slightly darker shade of a hex colour for hover effect."""
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i+2], 16) for i in (0, 2, 4))
    r, g, b = max(0, r - 25), max(0, g - 25), max(0, b - 25)
    return f"#{r:02x}{g:02x}{b:02x}"

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

@dataclass
class _UpdateRoomName:
    name: str


@dataclass
class _UpdateServerInfo:
    info: str


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
                 change_hotkey_cb=None, change_room_name_cb=None,
                 reconnect_cb=None, toggle_mute_cb=None) -> None:
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
        self._change_room_name_cb = change_room_name_cb
        self._reconnect_cb = reconnect_cb          # called with new_ip str
        self._toggle_mute_cb = toggle_mute_cb      # called with bool
        self._muted = False
        self._tray: Optional[TrayIcon] = None
        self._status_win: Optional[tk.Toplevel] = None
        self._status_frame: Optional[tk.Frame] = None
        self._hotkey_label: Optional[tk.Label] = None
        self._room_name_label: Optional[tk.Label] = None
        self._server_info_label: Optional[tk.Label] = None
        self._mute_btn_canvas = None               # canvas widget for mute button
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

    def update_room_name(self, name: str) -> None:
        self._q.put(_UpdateRoomName(name=name))

    def update_server_info(self, info: str) -> None:
        self._q.put(_UpdateServerInfo(info=info))

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
        elif isinstance(cmd, _UpdateRoomName):
            self._room_name = cmd.name
            self._refresh_room_name_label()
        elif isinstance(cmd, _UpdateServerInfo):
            self._server_info = cmd.info
            if self._server_info_label:
                self._server_info_label.config(text=f"Server: {cmd.info}")
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
        dismiss_btn = _make_btn(
            btn_frame,
            text="BESTÄTIGEN  (ESC)",
            bg=_WHITE,
            fg=_RED_BRIGHT,
            command=self._hide_alarm,
            font=("Arial", 24, "bold"),
            padx=30,
            pady=10,
            relief="raised",
            bd=2,
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
    _ST_ACCENT = "#0f3460"

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
        win.title(f"Alarmsystem \u2014 Status  v{__version__}")
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

        self._room_name_label = tk.Label(
            title_row, text=f"Raum: {self._room_name}",
            font=("Arial", 12, "bold"), bg=self._ST_HEADER_BG, fg=self._ST_FG,
            cursor="hand2",
        )
        self._room_name_label.pack(side="left")
        self._room_name_label.bind("<Button-1>", lambda _: self._edit_room_name_dialog())

        _make_btn(
            title_row, text="Beenden", bg=self._ST_RED, fg="white",
            command=self._exit_from_tray,
        ).pack(side="right", padx=(4, 0))

        _make_btn(
            title_row, text="Ausblenden", bg="#0f3460", fg=self._ST_FG,
            command=self._minimize_status,
        ).pack(side="right")

        # Info row: server label + scan button + hotkey + mute button
        info_row = tk.Frame(header, bg=self._ST_HEADER_BG)
        info_row.pack(fill="x", padx=10, pady=(4, 0))

        if self._server_info:
            self._server_info_label = tk.Label(
                info_row, text=f"Server: {self._server_info}",
                font=("Arial", 9), bg=self._ST_HEADER_BG, fg="#888888",
            )
            self._server_info_label.pack(side="left")

        _make_btn(
            info_row, text="🔍 Suchen", bg="#1a3a5c", fg=self._ST_FG,
            command=self._scan_server_dialog,
            font=("Arial", 8), padx=6, pady=1,
        ).pack(side="left", padx=(6, 0))

        # Mute toggle button — stored so we can update its label
        self._mute_btn_canvas = _make_btn(
            info_row, text="🔔 Ton an", bg="#1a3a5c", fg=self._ST_FG,
            command=self._toggle_mute,
            font=("Arial", 8), padx=6, pady=1,
        )
        self._mute_btn_canvas.pack(side="left", padx=(4, 0))

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

        # Footer (count + version)
        footer_row = tk.Frame(win, bg=self._ST_BG)
        footer_row.pack(fill="x", side="bottom")
        self._status_footer = tk.Label(
            footer_row, text="", font=("Arial", 9), bg=self._ST_BG, fg="#888888",
            anchor="w", padx=10, pady=4,
        )
        self._status_footer.pack(side="left")
        tk.Label(
            footer_row, text=f"v{__version__}", font=("Arial", 8),
            bg=self._ST_BG, fg="#555555", anchor="e", padx=10,
        ).pack(side="right")

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

    def _refresh_room_name_label(self) -> None:
        """Update the room name label text."""
        if self._room_name_label and self._room_name_label.winfo_exists():
            self._room_name_label.config(text=f"Raum: {self._room_name}")

    # ------------------------------------------------------------------
    # Mute toggle
    # ------------------------------------------------------------------

    def _toggle_mute(self) -> None:
        self._muted = not self._muted
        if self._toggle_mute_cb:
            self._toggle_mute_cb(self._muted)
        # Update button label and colour
        if self._mute_btn_canvas and self._mute_btn_canvas.winfo_exists():
            label = "🔕 Stumm" if self._muted else "🔔 Ton an"
            bg = self._ST_RED if self._muted else "#1a3a5c"
            self._mute_btn_canvas.config(bg=bg)
            for item in self._mute_btn_canvas.find_all():
                self._mute_btn_canvas.itemconfig(item, fill="white" if self._muted else self._ST_FG)
            # update button text via the named tag
            try:
                self._mute_btn_canvas.itemconfig("txt", text=label)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Server scan dialog
    # ------------------------------------------------------------------

    def _scan_server_dialog(self) -> None:
        """Open a dialog that scans the subnet and lets the user pick a server."""
        if not self._status_win:
            return

        from common.discovery import scan_subnet, local_subnet
        import threading as _threading

        port = int(self._server_info.split(":")[-1]) if ":" in self._server_info else 9999
        current_ip = self._server_info.split(":")[0] if ":" in self._server_info else ""

        dlg = tk.Toplevel(self._status_win)
        dlg.title("Server suchen")
        dlg.configure(bg=self._ST_BG)
        dlg.geometry("360x400")
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)
        dlg.transient(self._status_win)
        dlg.grab_set()

        # ── Header ──────────────────────────────────────────────────────
        tk.Label(
            dlg, text="Alarm-Server im Netzwerk suchen",
            font=("Arial", 11, "bold"), bg=self._ST_BG, fg=self._ST_FG,
        ).pack(pady=(14, 2))

        subnet_str = local_subnet() or "unbekannt"
        tk.Label(
            dlg, text=f"Subnetz: {subnet_str}   Port: {port}",
            font=("Arial", 9), bg=self._ST_BG, fg="#666666",
        ).pack()

        # ── Progress bar (canvas) ────────────────────────────────────────
        bar_outer = tk.Frame(dlg, bg="#1a2a4a", height=12)
        bar_outer.pack(fill="x", padx=16, pady=(10, 0))
        bar_outer.pack_propagate(False)

        bar_canvas = tk.Canvas(bar_outer, bg="#1a2a4a", height=12,
                               highlightthickness=0, bd=0)
        bar_canvas.pack(fill="both", expand=True)
        bar_fill = bar_canvas.create_rectangle(0, 0, 0, 12,
                                               fill=self._ST_ACCENT, width=0)

        progress_var = tk.StringVar(value="Starte Scan…")
        tk.Label(
            dlg, textvariable=progress_var,
            font=("Arial", 9), bg=self._ST_BG, fg="#888888",
        ).pack(pady=(4, 0))

        # ── Live IP log ──────────────────────────────────────────────────
        tk.Label(
            dlg, text="Gescannte IPs:", font=("Arial", 9),
            bg=self._ST_BG, fg="#666666", anchor="w",
        ).pack(fill="x", padx=16, pady=(8, 0))

        log_frame = tk.Frame(dlg, bg="#0a1a30")
        log_frame.pack(fill="both", expand=True, padx=16, pady=(2, 0))

        log_text = tk.Text(
            log_frame, font=("Courier", 9), bg="#0a1a30", fg="#557799",
            relief="flat", bd=0, height=6, state="disabled",
            wrap="none",
        )
        log_text.pack(side="left", fill="both", expand=True)
        log_scroll = tk.Scrollbar(log_frame, orient="vertical",
                                  command=log_text.yview)
        log_scroll.pack(side="right", fill="y")
        log_text.configure(yscrollcommand=log_scroll.set)
        # Tag for found servers — shown bright
        log_text.tag_configure("found", foreground="#00e676", font=("Courier", 9, "bold"))

        # ── Found servers list ───────────────────────────────────────────
        tk.Label(
            dlg, text="Gefundene Server:", font=("Arial", 9),
            bg=self._ST_BG, fg="#666666", anchor="w",
        ).pack(fill="x", padx=16, pady=(8, 0))

        listbox = tk.Listbox(
            dlg, font=("Arial", 10), bg="#0f3460", fg=self._ST_FG,
            selectbackground=self._ST_ACCENT, selectforeground="white",
            relief="flat", bd=0, height=3,
        )
        listbox.pack(fill="x", padx=16)

        # ── Buttons ──────────────────────────────────────────────────────
        btn_row = tk.Frame(dlg, bg=self._ST_BG)
        btn_row.pack(pady=(8, 12))

        def _on_connect():
            sel = listbox.curselection()
            if not sel:
                return
            ip = listbox.get(sel[0]).split()[0]
            dlg.destroy()
            if self._reconnect_cb:
                self._reconnect_cb(ip)

        _make_btn(
            btn_row, text="Verbinden", bg=self._ST_ACCENT, fg="white",
            command=_on_connect,
        ).pack(side="left", padx=(0, 8))

        _make_btn(
            btn_row, text="Abbrechen", bg="#444", fg=self._ST_FG,
            command=dlg.destroy,
        ).pack(side="left")

        # ── Scan logic ───────────────────────────────────────────────────
        def _progress(done: int, total: int, ip: str, is_found: bool) -> None:
            if not dlg.winfo_exists():
                return

            def _update():
                if not dlg.winfo_exists():
                    return
                # Update progress bar width
                bar_canvas.update_idletasks()
                bar_w = bar_canvas.winfo_width()
                filled = int(bar_w * done / total) if total else 0
                bar_canvas.coords(bar_fill, 0, 0, filled, 12)

                # Update counter label
                progress_var.set(f"Scanning… {done}/{total}  —  {done * 100 // total}%")

                # Append to log
                log_text.configure(state="normal")
                if is_found:
                    log_text.insert("end", f"✓ {ip}\n", "found")
                else:
                    log_text.insert("end", f"  {ip}\n")
                log_text.see("end")
                log_text.configure(state="disabled")

                # Add to found list immediately
                if is_found:
                    marker = "  ← aktuell" if ip == current_ip else ""
                    listbox.insert("end", f"{ip}{marker}")
                    if listbox.size() == 1:
                        listbox.selection_set(0)

            dlg.after(0, _update)

        def _run_scan() -> None:
            import asyncio
            loop = asyncio.new_event_loop()
            found = loop.run_until_complete(scan_subnet(port=port, progress_cb=_progress))
            loop.close()

            def _done():
                if not dlg.winfo_exists():
                    return
                count = len(found)
                progress_var.set(
                    f"Scan abgeschlossen — {count} Server gefunden" if count
                    else "Scan abgeschlossen — Keine Server gefunden"
                )
                # Fill bar completely
                bar_canvas.update_idletasks()
                bar_canvas.coords(bar_fill, 0, 0, bar_canvas.winfo_width(), 12)

            dlg.after(0, _done)

        _threading.Thread(target=_run_scan, daemon=True).start()

    # ------------------------------------------------------------------
    # Room name dialog
    # ------------------------------------------------------------------

    def _edit_room_name_dialog(self) -> None:
        """Open a small dialog to edit the room/client name."""
        if not self._status_win:
            return
        dlg = tk.Toplevel(self._status_win)
        dlg.title("Raumname ändern")
        dlg.configure(bg=self._ST_BG)
        dlg.geometry("300x130")
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)
        dlg.transient(self._status_win)
        dlg.grab_set()

        tk.Label(
            dlg, text="Neuer Raumname:", font=("Arial", 10),
            bg=self._ST_BG, fg=self._ST_FG,
        ).pack(pady=(15, 5))

        var = tk.StringVar(value=self._room_name)
        entry = tk.Entry(dlg, textvariable=var, font=("Arial", 11), width=20, justify="center")
        entry.pack()
        entry.select_range(0, tk.END)
        entry.focus_set()

        def _apply():
            new_name = var.get().strip()
            if new_name and new_name != self._room_name:
                self._room_name = new_name
                self._refresh_room_name_label()
                if self._change_room_name_cb:
                    self._change_room_name_cb(new_name)
            dlg.destroy()

        entry.bind("<Return>", lambda _: _apply())
        _make_btn(
            dlg, text="Übernehmen", bg="#00b894", fg="white",
            command=_apply, font=("Arial", 10, "bold"), padx=15, pady=4,
        ).pack(pady=8)

    def _edit_hotkey_dialog(self) -> None:
        """Open a small dialog to edit the hotkey."""
        if not self._status_win:
            return
        dlg = tk.Toplevel(self._status_win)
        dlg.title("Tastenkürzel ändern")
        dlg.configure(bg=self._ST_BG)
        dlg.geometry("320x150")
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)
        dlg.transient(self._status_win)
        dlg.grab_set()

        tk.Label(
            dlg, text="Drücke die gewünschte Tastenkombination:", font=("Arial", 10),
            bg=self._ST_BG, fg=self._ST_FG,
        ).pack(pady=(15, 5))

        # Canvas display — always renders colour on macOS
        cap_canvas = tk.Canvas(dlg, bg="#1a1a2e", width=260, height=32,
                               highlightthickness=1, highlightbackground="#00cec9", bd=0)
        cap_canvas.pack(padx=20)
        cap_text = cap_canvas.create_text(130, 16, text=self._hotkey,
                                          font=("Arial", 12, "bold"), fill="#00cec9")

        captured = {"combo": self._hotkey}

        _MODIFIER_KEYSYMS = {
            "control_l", "control_r", "alt_l", "alt_r", "shift_l", "shift_r",
            "meta_l", "meta_r", "super_l", "super_r", "caps_lock",
        }

        def _on_key(event):
            parts = []
            state = event.state
            if state & 0x4:   parts.append("ctrl")
            if state & 0x8:   parts.append("alt")
            if state & 0x80:  parts.append("cmd")   # macOS Command ⌘
            if state & 0x1:   parts.append("shift")
            key = event.keysym.lower()
            if key not in _MODIFIER_KEYSYMS:
                parts.append(key)
            if len(parts) >= 2:
                combo = "+".join(parts)
                captured["combo"] = combo
                cap_canvas.itemconfig(cap_text, text=combo)
            if event.keysym == "Return":
                _apply()

        # Bind to the dialog window so it works without clicking the canvas
        dlg.bind("<KeyPress>", _on_key)
        dlg.focus_set()

        tk.Label(
            dlg, text="(Die Kombination einfach drücken — kein Klick nötig)",
            font=("Arial", 8), bg=self._ST_BG, fg="#888888",
        ).pack()

        def _apply():
            new_hk = captured["combo"].strip()
            if new_hk and new_hk != self._hotkey:
                self._hotkey = new_hk
                self._refresh_hotkey_label()
                if self._change_hotkey_cb:
                    self._change_hotkey_cb(new_hk)
            dlg.destroy()

        _make_btn(
            dlg, text="Übernehmen", bg="#00b894", fg="white",
            command=_apply, font=("Arial", 10, "bold"), padx=15, pady=4,
        ).pack(pady=8)

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
