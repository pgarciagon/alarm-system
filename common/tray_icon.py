"""
tray_icon.py — System tray icon shared by server and client.

Uses pystray + Pillow.  Runs pystray's blocking loop in its own daemon thread.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageDraw
import pystray


def _find_icon_file(name: str) -> Optional[Path]:
    """Locate a bundled .ico file (works frozen and unfrozen)."""
    if getattr(sys, "frozen", False):
        p = Path(sys._MEIPASS) / "assets" / name  # type: ignore[attr-defined]
    else:
        p = Path(__file__).parent.parent / "assets" / name
    return p if p.exists() else None


def set_window_icon(window, icon_name: str) -> None:
    """Set the titlebar icon of a tkinter Tk or Toplevel window."""
    path = _find_icon_file(icon_name)
    if path:
        try:
            from PIL import ImageTk
            img = Image.open(path)
            # Use multiple sizes for best rendering
            photo = ImageTk.PhotoImage(img.resize((32, 32)))
            window._icon_photo = photo  # prevent GC
            window.iconphoto(True, photo)
        except Exception:
            pass


def _make_icon_image(size: int = 64, color: str = "#e94560") -> Image.Image:
    """Generate a simple circle icon programmatically (fallback)."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = size // 8
    draw.ellipse(
        [margin, margin, size - margin, size - margin],
        fill=color,
        outline="#ffffff",
        width=2,
    )
    return img


def _load_icon(icon_file: Optional[str], color: str) -> Image.Image:
    """Load .ico from file, falling back to generated image."""
    if icon_file:
        path = _find_icon_file(icon_file)
        if path:
            try:
                return Image.open(path)
            except Exception:
                pass
    return _make_icon_image(color=color)


class TrayIcon:
    """System tray icon with Show/Exit menu."""

    def __init__(
        self,
        on_show: Callable[[], None],
        on_exit: Callable[[], None],
        *,
        name: str = "alarm_server",
        title: str = "Alarm Server",
        show_label: str = "Fenster anzeigen",
        exit_label: str = "Beenden",
        icon_color: str = "#e94560",
        icon_file: Optional[str] = None,
    ) -> None:
        self._on_show = on_show
        self._on_exit = on_exit
        self._name = name
        self._title = title
        self._show_label = show_label
        self._exit_label = exit_label
        self._icon_color = icon_color
        self._icon_file = icon_file
        self._icon: Optional[pystray.Icon] = None

    def start(self) -> None:
        """Create and start the tray icon in a daemon thread."""
        menu = pystray.Menu(
            pystray.MenuItem(self._show_label, self._on_show_clicked, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(self._exit_label, self._on_exit_clicked),
        )
        self._icon = pystray.Icon(
            name=self._name,
            icon=_load_icon(self._icon_file, self._icon_color),
            title=self._title,
            menu=menu,
        )
        self._icon.run_detached()

    def stop(self) -> None:
        """Tear down the tray icon."""
        if self._icon:
            self._icon.stop()
            self._icon = None

    def update_tooltip(self, text: str) -> None:
        """Update the hover tooltip text."""
        if self._icon:
            self._icon.title = text

    # ------------------------------------------------------------------

    def _on_show_clicked(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        self._on_show()

    def _on_exit_clicked(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        self._on_exit()
