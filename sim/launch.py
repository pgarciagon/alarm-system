"""
launch.py — Simulation launcher for macOS and Linux.

Starts one server process + N client processes.  Each process gets its
own Terminal window on macOS (via `open -a Terminal`), which gives the
process full GUI / AppKit rights — required for tkinter to work.

Usage:
    python sim/launch.py [--rooms N] [--python PATH]

    --rooms N       Number of rooms to simulate (default: 3)
    --python PATH   Python interpreter to use (default: auto-detect)

On macOS each component opens in its own Terminal.app window.
On Linux it tries gnome-terminal, xterm, or falls back to background
processes with output sent to /tmp/alarm-sim/logs/.

Trigger an alarm: in any client window type  a  + Enter.
Stop:            Ctrl+C in each window, or close the windows.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SIM_DIR   = Path("/tmp/alarm-sim")
LOG_DIR   = SIM_DIR / "logs"


# ---------------------------------------------------------------------------
# Python auto-detection
# ---------------------------------------------------------------------------

def find_python() -> str:
    """Return the first Python interpreter that has tkinter."""
    candidates = [
        "/opt/homebrew/bin/python3.9",
        "/opt/homebrew/bin/python3.11",
        "/opt/homebrew/bin/python3.12",
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3.11",
        "/usr/local/bin/python3.12",
        "/usr/local/bin/python3",
        sys.executable,
        "python3",
    ]
    for c in candidates:
        if shutil.which(c) or Path(c).exists():
            try:
                subprocess.run(
                    [c, "-c", "import tkinter"],
                    capture_output=True, timeout=5,
                )
                result = subprocess.run(
                    [c, "-c", "import tkinter"],
                    capture_output=True, timeout=5,
                )
                if result.returncode == 0:
                    return c
            except Exception:
                continue
    return sys.executable


# ---------------------------------------------------------------------------
# Config writers
# ---------------------------------------------------------------------------

def write_configs(num_rooms: int) -> None:
    SIM_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    (SIM_DIR / "server_config.toml").write_text(textwrap.dedent("""\
        [server]
        host                  = "127.0.0.1"
        port                  = 9999
        heartbeat_timeout_sec = 15
        log_file              = ""
    """))

    for i in range(1, num_rooms + 1):
        (SIM_DIR / f"client_config_room{i}.toml").write_text(textwrap.dedent(f"""\
            [client]
            room_name   = "Room {i}"
            server_ip   = "127.0.0.1"
            server_port = 9999
            hotkey      = "alt+n"
            alarm_sound = ""
            log_file    = ""
        """))


# ---------------------------------------------------------------------------
# Process launchers
# ---------------------------------------------------------------------------

def launch_macos(title: str, python: str, args: list[str]) -> None:
    """
    On macOS, write a small shell script and open it in a new Terminal
    window using `open -a Terminal`.  This gives the child process a
    proper GUI / NSRunLoop context, which is required for tkinter.
    """
    script_path = SIM_DIR / f"_run_{title.replace(' ', '_')}.command"
    cmd_str = " ".join(f"'{a}'" if " " in a else a for a in [python] + args)
    script_path.write_text(textwrap.dedent(f"""\
        #!/bin/bash
        cd '{REPO_ROOT}'
        echo '=== {title} ==='
        {cmd_str}
        echo '--- process ended ---'
        read -p 'Press Enter to close...'
    """))
    script_path.chmod(0o755)
    subprocess.Popen(["open", "-a", "Terminal", str(script_path)])


def launch_linux(title: str, python: str, args: list[str], log_path: Path) -> subprocess.Popen:
    """Launch in a new terminal emulator window, or background with log file."""
    cmd = [python] + args
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}

    for term in ["gnome-terminal", "xterm", "konsole"]:
        if shutil.which(term):
            if term == "gnome-terminal":
                full = ["gnome-terminal", f"--title={title}", "--"] + cmd
            elif term == "konsole":
                full = ["konsole", "--title", title, "-e"] + cmd
            else:
                full = ["xterm", "-title", title, "-e"] + cmd
            return subprocess.Popen(full, cwd=str(REPO_ROOT), env=env)

    # No terminal emulator — run in background and log to file
    print(f"  [{title}] no terminal found — logging to {log_path}")
    with open(log_path, "w") as lf:
        return subprocess.Popen(cmd, cwd=str(REPO_ROOT), stdout=lf, stderr=lf, env=env)


def launch(title: str, python: str, module_args: list[str]) -> None:
    if sys.platform == "darwin":
        launch_macos(title, python, module_args)
    else:
        log = LOG_DIR / f"{title.replace(' ', '_')}.log"
        launch_linux(title, python, module_args, log)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Alarm system simulation launcher")
    parser.add_argument("--rooms",  type=int, default=3,    help="Number of rooms (default: 3)")
    parser.add_argument("--python", default=None,           help="Python interpreter path")
    args = parser.parse_args()

    python = args.python or find_python()
    print(f"Using Python: {python}")

    write_configs(args.rooms)

    # ---- Server ----
    print("Starting server…")
    launch(
        "Alarm Server",
        python,
        ["-m", "server.server", "--config", str(SIM_DIR / "server_config.toml")],
    )
    time.sleep(1.2)   # give server time to bind

    # ---- Clients ----
    for i in range(1, args.rooms + 1):
        cfg = str(SIM_DIR / f"client_config_room{i}.toml")
        print(f"Starting client Room {i}…")
        launch(
            f"Alarm Client Room {i}",
            python,
            ["-m", "client.client", "--config", cfg, "--fallback-hotkey"],
        )
        time.sleep(0.4)

    print()
    print(f"Simulation running with {args.rooms} rooms.")
    print("In any client window, type  a  + Enter  to trigger an alarm.")
    if sys.platform != "darwin":
        print(f"Logs: {LOG_DIR}/")
    print("Close the Terminal windows (or Ctrl+C) to stop.")


if __name__ == "__main__":
    main()
