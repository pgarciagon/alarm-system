"""
sound.py — Alarm sound playback wrapper.

Uses pygame.mixer which is cross-platform (macOS + Windows) and supports
looping.  Falls back to a simple beep via the `winsound` module on Windows
if pygame is unavailable.

The bundled asset is looked up relative to this file and also relative to
the PyInstaller _MEIPASS bundle directory when packaged as an executable.
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from typing import Optional

log = logging.getLogger("alarm.client.sound")

# ---------------------------------------------------------------------------
# Asset resolution
# ---------------------------------------------------------------------------

def _find_asset(relative_path: str) -> Path:
    """
    Locate *relative_path* inside the bundled executable (PyInstaller) or
    relative to the repository root.
    """
    # PyInstaller sets sys._MEIPASS to the temp extraction directory
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidate = Path(base) / relative_path
        if candidate.exists():
            return candidate

    # Development: relative to repo root
    repo_root = Path(__file__).parent.parent
    candidate = repo_root / relative_path
    if candidate.exists():
        return candidate

    raise FileNotFoundError(f"Asset not found: {relative_path}")


# ---------------------------------------------------------------------------
# Sound player
# ---------------------------------------------------------------------------

class SoundPlayer:
    """
    Plays (and loops) an alarm sound file.

    Parameters
    ----------
    sound_path : str
        Path to a .wav file.  Empty string → use the bundled asset.
    """

    def __init__(self, sound_path: str = "") -> None:
        self._path: Optional[Path] = None
        self._playing = False
        self._lock = threading.Lock()
        self._pygame_ok = False

        # Resolve path
        if sound_path:
            p = Path(sound_path)
            if p.exists():
                self._path = p
            else:
                log.warning("Custom sound path not found: %s — using bundled asset", sound_path)

        if self._path is None:
            try:
                self._path = _find_asset("assets/alarm.wav")
            except FileNotFoundError:
                log.warning("Bundled alarm.wav not found — sound will be silent")

        self._init_pygame()

    def _init_pygame(self) -> None:
        try:
            import pygame  # local import so tests can mock

            pygame.mixer.init()
            self._pygame_ok = True
            log.debug("pygame.mixer initialised")
        except ImportError:
            log.warning("pygame not installed — sound disabled. Run: pip install pygame")
        except Exception as exc:  # noqa: BLE001
            log.warning("pygame.mixer init failed: %s — sound disabled", exc)

    def play(self) -> None:
        """Start looped playback (non-blocking)."""
        with self._lock:
            if self._playing:
                return
            self._playing = True

        if not self._pygame_ok or self._path is None:
            self._fallback_beep()
            return

        threading.Thread(target=self._pygame_play, daemon=True).start()

    def stop(self) -> None:
        """Stop playback."""
        with self._lock:
            self._playing = False

        if self._pygame_ok:
            try:
                import pygame

                pygame.mixer.stop()
            except Exception as exc:  # noqa: BLE001
                log.warning("Error stopping sound: %s", exc)

    def _pygame_play(self) -> None:
        try:
            import pygame

            sound = pygame.mixer.Sound(str(self._path))
            sound.play(loops=-1)   # -1 = loop indefinitely
            log.debug("Sound started: %s", self._path)

            # Block until stop() is called
            while True:
                with self._lock:
                    if not self._playing:
                        break
                import time
                time.sleep(0.1)

            pygame.mixer.stop()
            log.debug("Sound stopped")
        except Exception as exc:  # noqa: BLE001
            log.error("Sound playback error: %s", exc)

    def _fallback_beep(self) -> None:
        """Emit a system beep as a last resort."""
        if sys.platform == "win32":
            try:
                import winsound
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            except Exception:
                pass
        else:
            # Terminal bell
            sys.stdout.write("\a")
            sys.stdout.flush()
