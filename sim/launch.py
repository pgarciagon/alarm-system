"""
launch.py — Simulation launcher for macOS and Linux.

Starts one server process + N client processes.  On macOS each process
opens in its own Terminal.app window via `open -a Terminal .command`,
which gives the process full AppKit/NSRunLoop rights for tkinter.

Usage:
    python sim/launch.py [--rooms N] [--python PATH]

    --rooms N       Number of rooms to simulate (default: 3)
    --python PATH   Python interpreter to use (default: auto-detect)

Trigger an alarm: in any client window type  a  + Enter.
Stop:            Ctrl+C in each window, or close the windows.

macOS note:
  Tcl/Tk 8.6 (bundled with Python 3.9 via Homebrew) crashes on macOS 26+
  (Tahoe) with "Tcl_WaitForEvent: Notifier not initialized".
  Tcl/Tk 9.0 (bundled with Python 3.12+ via Homebrew) works correctly.
  This script therefore prefers Python 3.12+ when auto-detecting.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import List

REPO_ROOT = Path(__file__).parent.parent
SIM_DIR   = Path("/tmp/alarm-sim")
LOG_DIR   = SIM_DIR / "logs"


# ---------------------------------------------------------------------------
# Python auto-detection
# ---------------------------------------------------------------------------

def find_python() -> str:
    """
    Return the first Python interpreter that has tkinter.
    Prefers 3.12+ (Tcl/Tk 9) over 3.9 (Tcl/Tk 8.6) for macOS 26+.
    """
    candidates: List[str] = [
        # Homebrew opt symlinks (canonical) and Cellar direct paths
        "/opt/homebrew/opt/python@3.13/bin/python3.13",
        "/opt/homebrew/opt/python@3.12/bin/python3.12",
        "/opt/homebrew/opt/python@3.11/bin/python3.11",
        "/opt/homebrew/bin/python3.13",
        "/opt/homebrew/bin/python3.12",
        "/opt/homebrew/bin/python3.11",
        "/usr/local/opt/python@3.13/bin/python3.13",
        "/usr/local/opt/python@3.12/bin/python3.12",
        "/usr/local/opt/python@3.11/bin/python3.11",
        "/usr/local/bin/python3.13",
        "/usr/local/bin/python3.12",
        "/usr/local/bin/python3.11",
        # 3.9 last — Tcl/Tk 8.6 crashes on macOS 26
        "/opt/homebrew/opt/python@3.9/bin/python3.9",
        "/opt/homebrew/bin/python3.9",
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        sys.executable,
        "python3",
    ]

    # Also scan Homebrew Cellar for any 3.12+ install not in opt/
    for version in ("3.13", "3.12", "3.11"):
        cellar = Path(f"/opt/homebrew/Cellar/python@{version}")
        if cellar.exists():
            for sub in sorted(cellar.iterdir(), reverse=True):
                p = sub / "bin" / f"python{version}"
                if p.exists():
                    candidates.insert(0, str(p))

    seen: set = set()
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        resolved = shutil.which(c) or (c if Path(c).exists() else None)
        if not resolved:
            continue
        try:
            result = subprocess.run(
                [resolved, "-c", "import tkinter"],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                return resolved
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

def launch_macos(title: str, python: str, args: List[str]) -> None:
    """
    Write a .command shell script and open it with 'open -a Terminal'.
    This gives the child process a proper AppKit / NSRunLoop context,
    which is required for tkinter on macOS.
    """
    safe_title = title.replace(" ", "_")
    script_path = SIM_DIR / f"_run_{safe_title}.command"
    cmd_parts = [python] + args
    cmd_str = " ".join(
        f"'{a}'" if (" " in a or not a) else a
        for a in cmd_parts
    )
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


def launch_linux(title: str, python: str, args: List[str], log_path: Path) -> None:
    cmd = [python] + args
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}

    for term, build_cmd in [
        ("gnome-terminal", lambda: ["gnome-terminal", f"--title={title}", "--"] + cmd),
        ("konsole",        lambda: ["konsole", "--title", title, "-e"] + cmd),
        ("xterm",          lambda: ["xterm", "-title", title, "-e"] + cmd),
    ]:
        if shutil.which(term):
            subprocess.Popen(build_cmd(), cwd=str(REPO_ROOT), env=env)
            return

    print(f"  [{title}] no terminal found — logging to {log_path}")
    with open(log_path, "w") as lf:
        subprocess.Popen(cmd, cwd=str(REPO_ROOT), stdout=lf, stderr=lf, env=env)


def launch(title: str, python: str, module_args: List[str]) -> None:
    if sys.platform == "darwin":
        launch_macos(title, python, module_args)
    else:
        log = LOG_DIR / f"{title.replace(' ', '_')}.log"
        launch_linux(title, python, module_args, log)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Alarm system simulation launcher")
    parser.add_argument("--rooms",  type=int, default=3,  help="Number of rooms (default: 3)")
    parser.add_argument("--python", default=None,         help="Python interpreter path")
    args = parser.parse_args()

    python = args.python or find_python()
    print(f"Using Python: {python}")

    # Show Tcl/Tk version so user can verify it's 9.0 on macOS 26+
    try:
        result = subprocess.run(
            [python, "-c",
             "import tkinter; print(f'Tcl/Tk {tkinter.TclVersion}/{tkinter.TkVersion}')"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            print(f"Tcl/Tk: {result.stdout.strip()}")
        if result.returncode == 0 and "8.6" in result.stdout:
            import platform
            mac_ver = platform.mac_ver()[0]
            major = int(mac_ver.split(".")[0]) if mac_ver else 0
            if major >= 26:
                print(
                    "\nWARNING: Tcl/Tk 8.6 is known to crash on macOS 26+ (Tahoe).\n"
                    "Install Python 3.12+ for Tcl/Tk 9.0:\n"
                    "  brew install python@3.12 python-tk@3.12\n"
                    "Then re-run with:\n"
                    "  PYTHON=/opt/homebrew/opt/python@3.12/bin/python3.12 "
                    "./sim/run_simulation.sh\n"
                )
    except Exception:
        pass

    write_configs(args.rooms)

    print("Starting server…")
    launch(
        "Alarm Server",
        python,
        ["-m", "server.server", "--config", str(SIM_DIR / "server_config.toml")],
    )
    time.sleep(1.2)

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
