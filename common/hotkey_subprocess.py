"""
hotkey_subprocess.py — Runs in a child process to isolate pynput from the
main tkinter process.  Writes "TRIGGERED\n" to stdout each time the hotkey
fires so the parent process can react without being affected by any pynput /
CGEventTap crashes on macOS.

Usage (internal):
    python -m common.hotkey_subprocess <pynput-hotkey>

Hotkey format: pynput GlobalHotKeys syntax, e.g. "<cmd>+n" or "<ctrl>+<shift>+a".
"""

import sys
import os


def main() -> None:
    if len(sys.argv) < 2:
        sys.stderr.write("Usage: hotkey_subprocess.py <hotkey>\n")
        sys.exit(1)

    hotkey = sys.argv[1]

    # Suppress pygame / SDL noise
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

    try:
        from pynput import keyboard  # type: ignore

        def _on_activate():
            try:
                sys.stdout.write("TRIGGERED\n")
                sys.stdout.flush()
            except Exception:
                pass

        with keyboard.GlobalHotKeys({hotkey: _on_activate}) as h:
            h.join()

    except Exception as exc:
        sys.stderr.write(f"hotkey_subprocess error: {exc}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
