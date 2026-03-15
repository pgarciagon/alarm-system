"""
sound.py — Alarm sound playback wrapper.

Uses pygame.mixer which is cross-platform (macOS + Windows) and supports
looping.  Falls back to a terminal beep if pygame is unavailable.

macOS note: SDL (used internally by pygame) installs CoreAudio hooks that
conflict with Tcl's notifier when initialised on the main thread before
tkinter.  We work around this by:
  1. Setting SDL_VIDEODRIVER=dummy and SDL_AUDIODRIVER=coreaudio so SDL
     does NOT touch the main NSRunLoop / display server.
  2. Initialising pygame.mixer (not the full pygame) only, from a
     background thread, AFTER tkinter's mainloop has started.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path
from typing import Optional

log = logging.getLogger("alarm.client.sound")


# ---------------------------------------------------------------------------
# Asset resolution
# ---------------------------------------------------------------------------

def _find_asset(relative_path: str) -> Path:
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidate = Path(base) / relative_path
        if candidate.exists():
            return candidate
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

    pygame.mixer is initialised lazily in a background thread the first
    time play() is called, to avoid interfering with tkinter on macOS.
    """

    def __init__(self, sound_path: str = "") -> None:
        self._path: Optional[Path] = None
        self._playing = False
        self._lock = threading.Lock()
        self._pygame_ok = False
        self._init_done = False
        self._init_lock = threading.Lock()

        # Resolve sound file path (don't touch pygame yet)
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

    # ------------------------------------------------------------------
    # Lazy pygame initialisation (called from background thread)
    # ------------------------------------------------------------------

    def _ensure_init(self) -> None:
        """Initialise pygame.mixer on first use (background thread safe)."""
        with self._init_lock:
            if self._init_done:
                return
            self._init_done = True
            self._pygame_ok = False
            try:
                import pygame  # noqa: PLC0415

                # Tell SDL not to touch the video display or the main run loop.
                # These must be set BEFORE pygame.mixer.init().
                os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
                os.environ.setdefault("SDL_AUDIODRIVER", "coreaudio")

                # Suppress pygame's startup banner
                os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

                pygame.mixer.pre_init(frequency=44100, size=-16, channels=1, buffer=512)
                pygame.mixer.init()
                self._pygame_ok = True
                log.debug("pygame.mixer initialised")
            except ImportError:
                log.warning("pygame not installed — sound disabled. Run: pip install pygame")
            except Exception as exc:  # noqa: BLE001
                log.warning("pygame.mixer init failed: %s — sound disabled", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def play(self) -> None:
        """Start looped playback (non-blocking)."""
        with self._lock:
            if self._playing:
                return
            self._playing = True
        threading.Thread(target=self._play_worker, daemon=True).start()

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

    # ------------------------------------------------------------------
    # Background worker
    # ------------------------------------------------------------------

    def _play_worker(self) -> None:
        self._ensure_init()

        if not self._pygame_ok or self._path is None:
            self._fallback_beep()
            return

        try:
            import pygame

            sound = pygame.mixer.Sound(str(self._path))
            sound.play(loops=-1)
            log.debug("Sound started: %s", self._path)

            import time
            while True:
                with self._lock:
                    if not self._playing:
                        break
                time.sleep(0.1)

            pygame.mixer.stop()
            log.debug("Sound stopped")
        except Exception as exc:  # noqa: BLE001
            log.error("Sound playback error: %s", exc)

    def _fallback_beep(self) -> None:
        if sys.platform == "win32":
            try:
                import winsound
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            except Exception:
                pass
        else:
            sys.stdout.write("\a")
            sys.stdout.flush()
