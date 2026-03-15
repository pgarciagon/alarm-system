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
        self._root.title("Alarm Server — Dashboard")
        set_window_icon(self._root, "alarm_server.ico")
        self._root.configure(bg=_BG)
        self._root.geometry("620x460")
        self._root.minsize(500, 350)
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

        tk.Button(
            title_row, text="Beenden", font=("Arial", 9),
            bg=_RED, fg="white", relief="flat", padx=10, pady=2,
            cursor="hand2", command=self._exit,
        ).pack(side=tk.RIGHT, padx=(4, 0))

        tk.Button(
            title_row, text="Ausblenden", font=("Arial", 9),
            bg=_BLUE, fg=_FG, relief="flat", padx=10, pady=2,
            cursor="hand2", command=self._minimize_to_tray,
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
        for col, (text, w) in enumerate([("Raum", 20), ("Status", 12), ("Letzter Heartbeat", 20)]):
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

                # Room name
                tk.Label(
                    row, text=snap.room, font=("Arial", 10),
                    fg=_FG, bg=row_bg, anchor="w", width=20,
                ).grid(row=0, column=0, sticky="w")

                # Status indicator
                if snap.is_down:
                    status_text = "OFFLINE"
                    status_color = _RED
                else:
                    status_text = "ONLINE"
                    status_color = _GREEN

                tk.Label(
                    row, text=f"\u25cf {status_text}", font=("Arial", 10, "bold"),
                    fg=status_color, bg=row_bg, anchor="w", width=12,
                ).grid(row=0, column=1, sticky="w")

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
                    bg=row_bg, anchor="w", width=20,
                ).grid(row=0, column=2, sticky="w")

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
