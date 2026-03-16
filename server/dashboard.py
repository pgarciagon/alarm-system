"""
dashboard.py — Tkinter dashboard GUI for the alarm server.

Shows server configuration and a live table of connected clients.
Minimises to the system tray on close.
"""

from __future__ import annotations

import time
import tkinter as tk
from tkinter import ttk
from typing import List, Optional

from common.config import ServerConfig
from common.tray_icon import TrayIcon, set_window_icon
from common.version import __version__

# Avoid circular import at module level — AlarmServer is only used for typing.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from server.server import AlarmServer, ClientSnapshot


# ---------------------------------------------------------------------------
# Theme (reused from installer)
# ---------------------------------------------------------------------------

_BG = "#1a1a2e"
_FG = "#e0e0e0"
_BLUE = "#0f3460"
_ACCENT = "#e94560"
_GREEN = "#00b894"
_AMBER = "#fdcb6e"
_RED = "#e94560"
_HEADER_BG = "#16213e"


def _darken(hex_color: str) -> str:
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i+2], 16) for i in (0, 2, 4))
    r, g, b = max(0, r - 25), max(0, g - 25), max(0, b - 25)
    return f"#{r:02x}{g:02x}{b:02x}"


def _make_btn(parent, text, bg, fg, command, font=("Arial", 9), padx=10, pady=2):
    """Canvas-based button — tk.Canvas always honours bg/fg on macOS Aqua theme."""
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


class ServerDashboard:
    """Main-thread tkinter dashboard for the alarm server."""

    REFRESH_MS = 1500  # how often to poll client state

    def __init__(self, server: "AlarmServer", cfg: ServerConfig) -> None:
        self._server = server
        self._cfg = cfg
        self._root: Optional[tk.Tk] = None
        self._tray: Optional[TrayIcon] = None
        self._client_frame: Optional[tk.Frame] = None
        self._client_widgets: List[tk.Frame] = []
        self._status_label: Optional[tk.Label] = None

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def run_mainloop(self) -> None:
        """Build the UI and enter the tkinter main loop (blocks)."""
        self._root = tk.Tk()
        self._root.title(f"Alarm Server — Dashboard  v{__version__}")
        set_window_icon(self._root, "alarm_server.ico")
        self._root.configure(bg=_BG)
        self._root.geometry("700x460")
        self._root.minsize(600, 350)
        # Hide from taskbar — only show in system tray
        self._root.attributes("-toolwindow", True)
        self._root.protocol("WM_DELETE_WINDOW", self._minimize_to_tray)

        self._build_config_section()
        self._build_client_section()

        # System tray
        self._tray = TrayIcon(
            on_show=self._show_window,
            on_exit=self._exit,
            name="alarm_server",
            title="Alarm Server",
            show_label="Dashboard anzeigen",
            icon_file="alarm_server.ico",
        )
        self._tray.start()

        # Start polling
        self._root.after(self.REFRESH_MS, self._refresh_clients)
        self._root.mainloop()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_config_section(self) -> None:
        """Top panel showing current server configuration."""
        frame = tk.Frame(self._root, bg=_HEADER_BG, padx=15, pady=10)
        frame.pack(fill=tk.X, padx=10, pady=(10, 5))

        # Title row with shutdown button
        title_row = tk.Frame(frame, bg=_HEADER_BG)
        title_row.pack(fill=tk.X)

        tk.Label(
            title_row, text="Server-Konfiguration", font=("Arial", 14, "bold"),
            fg=_ACCENT, bg=_HEADER_BG, anchor="w",
        ).pack(side=tk.LEFT)
        tk.Label(
            title_row, text=f"v{__version__}", font=("Arial", 9),
            fg="#555555", bg=_HEADER_BG, anchor="w",
        ).pack(side=tk.LEFT, padx=(8, 0))

        _make_btn(
            title_row, text="Beenden", bg=_RED, fg="white",
            command=self._exit,
        ).pack(side=tk.RIGHT, padx=(4, 0))

        _make_btn(
            title_row, text="Ausblenden", bg=_BLUE, fg=_FG,
            command=self._minimize_to_tray,
        ).pack(side=tk.RIGHT)

        info_frame = tk.Frame(frame, bg=_HEADER_BG)
        info_frame.pack(fill=tk.X, pady=(5, 0))

        pairs = [
            ("Host:", self._cfg.host),
            ("Port:", str(self._cfg.port)),
            ("Heartbeat-Timeout:", f"{self._cfg.heartbeat_timeout_sec}s"),
            ("Silent Alarm:", "Ja" if self._cfg.silent_alarm else "Nein"),
        ]
        for col, (label, value) in enumerate(pairs):
            tk.Label(
                info_frame, text=label, font=("Arial", 10),
                fg="#aaaaaa", bg=_HEADER_BG,
            ).grid(row=0, column=col * 2, sticky="w", padx=(0, 3))
            tk.Label(
                info_frame, text=value, font=("Arial", 10, "bold"),
                fg=_FG, bg=_HEADER_BG,
            ).grid(row=0, column=col * 2 + 1, sticky="w", padx=(0, 15))

    def _build_client_section(self) -> None:
        """Main area with a header row and scrollable client list."""
        wrapper = tk.Frame(self._root, bg=_BG)
        wrapper.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # Section title + status count
        header_bar = tk.Frame(wrapper, bg=_BG)
        header_bar.pack(fill=tk.X, pady=(0, 5))

        tk.Label(
            header_bar, text="Verbundene Clients", font=("Arial", 14, "bold"),
            fg=_ACCENT, bg=_BG, anchor="w",
        ).pack(side=tk.LEFT)

        self._status_label = tk.Label(
            header_bar, text="0 Clients", font=("Arial", 10),
            fg="#aaaaaa", bg=_BG, anchor="e",
        )
        self._status_label.pack(side=tk.RIGHT)

        # Table header
        hdr = tk.Frame(wrapper, bg=_BLUE, padx=10, pady=6)
        hdr.pack(fill=tk.X)
        for col, (text, w) in enumerate([("Raum", 14), ("Status", 10), ("Hotkey", 10), ("Heartbeat", 10), ("", 6)]):
            tk.Label(
                hdr, text=text, font=("Arial", 10, "bold"),
                fg=_FG, bg=_BLUE, anchor="w", width=w,
            ).grid(row=0, column=col, sticky="w")

        # Scrollable client list
        canvas = tk.Canvas(wrapper, bg=_BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(wrapper, orient=tk.VERTICAL, command=canvas.yview)
        self._client_frame = tk.Frame(canvas, bg=_BG)

        self._client_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=self._client_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Placeholder
        self._empty_label = tk.Label(
            self._client_frame,
            text="Keine Clients verbunden.",
            font=("Arial", 11), fg="#666666", bg=_BG, pady=20,
        )
        self._empty_label.pack()

    # ------------------------------------------------------------------
    # Refresh loop
    # ------------------------------------------------------------------

    def _refresh_clients(self) -> None:
        """Poll the server for client state and update the table."""
        snapshots: List["ClientSnapshot"] = self._server.get_client_snapshot()
        now = time.monotonic()

        # Clear old rows
        for w in self._client_widgets:
            w.destroy()
        self._client_widgets.clear()

        if self._empty_label:
            self._empty_label.destroy()
            self._empty_label = None

        if not snapshots:
            self._empty_label = tk.Label(
                self._client_frame,
                text="Keine Clients verbunden.",
                font=("Arial", 11), fg="#666666", bg=_BG, pady=20,
            )
            self._empty_label.pack()
        else:
            for i, snap in enumerate(sorted(snapshots, key=lambda s: s.room)):
                row_bg = _BG if i % 2 == 0 else _HEADER_BG
                row = tk.Frame(self._client_frame, bg=row_bg, padx=10, pady=4)
                row.pack(fill=tk.X)

                # Room name (clickable to rename)
                room_lbl = tk.Label(
                    row, text=snap.room, font=("Arial", 10),
                    fg=_FG, bg=row_bg, anchor="w", width=14, cursor="hand2",
                )
                room_lbl.grid(row=0, column=0, sticky="w")
                room_lbl.bind("<Button-1>", lambda _e, r=snap.room: self._edit_room_name(r))

                # Status indicator
                if snap.is_down:
                    status_text = "OFFLINE"
                    status_color = _RED
                else:
                    status_text = "ONLINE"
                    status_color = _GREEN

                tk.Label(
                    row, text=f"\u25cf {status_text}", font=("Arial", 10, "bold"),
                    fg=status_color, bg=row_bg, anchor="w", width=10,
                ).grid(row=0, column=1, sticky="w")

                # Hotkey (clickable to edit)
                hk_text = snap.hotkey.upper() if snap.hotkey else "—"
                hk_lbl = tk.Label(
                    row, text=hk_text, font=("Arial", 10),
                    fg=_AMBER, bg=row_bg, anchor="w", width=10,
                    cursor="hand2",
                )
                hk_lbl.grid(row=0, column=2, sticky="w")
                hk_lbl.bind("<Button-1>", lambda _e, r=snap.room, h=snap.hotkey: self._edit_hotkey(r, h))

                # Last heartbeat age
                age = now - snap.last_heartbeat
                if age < 60:
                    age_text = f"vor {int(age)}s"
                elif age < 3600:
                    age_text = f"vor {int(age // 60)}m"
                else:
                    age_text = f"vor {int(age // 3600)}h"

                tk.Label(
                    row, text=age_text, font=("Arial", 10),
                    fg="#aaaaaa" if not snap.is_down else _RED,
                    bg=row_bg, anchor="w", width=10,
                ).grid(row=0, column=3, sticky="w")

                # Remove button
                _make_btn(
                    row, text="\u2716", bg=row_bg, fg=_RED,
                    command=lambda r=snap.room: self._remove_client(r),
                    padx=6, pady=2,
                ).grid(row=0, column=4, sticky="e")

                self._client_widgets.append(row)

        # Update counts
        total = len(snapshots)
        online = sum(1 for s in snapshots if not s.is_down)
        if self._status_label:
            self._status_label.config(text=f"{online}/{total} online")

        # Update tray tooltip
        if self._tray:
            self._tray.update_tooltip(f"Alarm Server — {online}/{total} Clients online")

        # Reschedule
        if self._root:
            self._root.after(self.REFRESH_MS, self._refresh_clients)

    # ------------------------------------------------------------------
    # Client actions
    # ------------------------------------------------------------------

    def _remove_client(self, room: str) -> None:
        """Remove a client from the server registry."""
        self._server.remove_client(room)

    def _edit_hotkey(self, room: str, current_hotkey: str) -> None:
        """Open a dialog to change a client's hotkey."""
        if not self._root:
            return
        dlg = tk.Toplevel(self._root)
        dlg.title(f"Hotkey — {room}")
        dlg.configure(bg=_BG)
        dlg.geometry("320x160")
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)
        dlg.transient(self._root)
        dlg.grab_set()

        tk.Label(
            dlg, text=f"Drücke Kombination für {room}:", font=("Arial", 10),
            bg=_BG, fg=_FG,
        ).pack(pady=(15, 5))

        captured = {"combo": current_hotkey}

        cap_canvas = tk.Canvas(dlg, bg="#1a1a2e", width=260, height=32,
                               highlightthickness=1, highlightbackground="#00cec9", bd=0)
        cap_canvas.pack(padx=20)
        cap_text = cap_canvas.create_text(130, 16, text=current_hotkey,
                                          font=("Arial", 12, "bold"), fill="#00cec9")

        _MODIFIER_KEYSYMS = {
            "control_l", "control_r", "alt_l", "alt_r", "shift_l", "shift_r",
            "meta_l", "meta_r", "super_l", "super_r", "caps_lock",
        }

        def _on_key(event):
            parts = []
            state = event.state
            if state & 0x4:  parts.append("ctrl")
            if state & 0x8:  parts.append("alt")
            if state & 0x80: parts.append("cmd")
            if state & 0x1:  parts.append("shift")
            key = event.keysym.lower()
            if key not in _MODIFIER_KEYSYMS:
                parts.append(key)
            if len(parts) >= 2:
                combo = "+".join(parts)
                captured["combo"] = combo
                cap_canvas.itemconfig(cap_text, text=combo)
            if event.keysym == "Return":
                _apply()

        dlg.bind("<KeyPress>", _on_key)
        dlg.focus_set()

        tk.Label(
            dlg, text="(Die Kombination einfach drücken — kein Klick nötig)",
            font=("Arial", 8), bg=_BG, fg="#888888",
        ).pack()

        def _apply():
            new_hk = captured["combo"].strip()
            if new_hk:
                self._server.set_client_hotkey(room, new_hk)
            dlg.destroy()

        _make_btn(
            dlg, text="Übernehmen", bg=_GREEN, fg="white",
            command=_apply, font=("Arial", 10, "bold"), padx=15, pady=4,
        ).pack(pady=8)

    def _edit_room_name(self, room: str) -> None:
        """Open a dialog to rename a client's room."""
        if not self._root:
            return
        dlg = tk.Toplevel(self._root)
        dlg.title(f"Umbenennen — {room}")
        dlg.configure(bg=_BG)
        dlg.geometry("300x130")
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)
        dlg.transient(self._root)
        dlg.grab_set()

        tk.Label(
            dlg, text=f"Neuer Name für {room}:", font=("Arial", 10),
            bg=_BG, fg=_FG,
        ).pack(pady=(15, 5))

        var = tk.StringVar(value=room)
        entry = tk.Entry(dlg, textvariable=var, font=("Arial", 11), width=20, justify="center")
        entry.pack(padx=20)
        entry.select_range(0, tk.END)
        entry.focus_set()

        def _apply():
            new_name = var.get().strip()
            if new_name and new_name != room:
                self._server.set_client_room_name(room, new_name)
            dlg.destroy()

        entry.bind("<Return>", lambda _: _apply())
        _make_btn(
            dlg, text="Übernehmen", bg=_GREEN, fg="white",
            command=_apply, font=("Arial", 10, "bold"), padx=15, pady=4,
        ).pack(pady=8)

    # ------------------------------------------------------------------
    # Tray integration
    # ------------------------------------------------------------------

    def _minimize_to_tray(self) -> None:
        """Hide window on close button (keep running in tray)."""
        if self._root:
            self._root.withdraw()

    def _show_window(self) -> None:
        """Restore the window from tray.  Called from pystray thread."""
        if self._root:
            self._root.after(0, self._root.deiconify)

    def _exit(self) -> None:
        """Full shutdown: server + tray + tkinter."""
        self._server.request_shutdown()
        if self._tray:
            self._tray.stop()
        if self._root:
            self._root.after(0, self._root.quit)
