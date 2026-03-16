"""
install_autostart_mac.py — Register a macOS launchd plist (for development).

Run once:
    python scripts/install_autostart_mac.py --target /path/to/server.py --role server
    python scripts/install_autostart_mac.py --target /path/to/client.py --role client

The plist is written to ~/Library/LaunchAgents/ and loaded immediately.
The process will be kept alive (launchd restarts it on exit).
"""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path

LABEL_SERVER = "com.alarm-system.server"
LABEL_CLIENT = "com.alarm-system.client"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"


def main() -> None:
    parser = argparse.ArgumentParser(description="Register macOS launchd auto-start")
    parser.add_argument("--target", required=True, help="Absolute path to the Python script or executable")
    parser.add_argument("--role", choices=["server", "client"], required=True)
    parser.add_argument("--config", default=None, help="Optional path to config file")
    parser.add_argument("--uninstall", action="store_true", help="Remove the agent instead")
    args = parser.parse_args()

    label     = LABEL_SERVER if args.role == "server" else LABEL_CLIENT
    plist_path = LAUNCH_AGENTS / f"{label}.plist"
    target    = Path(args.target).resolve()

    if args.uninstall:
        _unload(plist_path, label)
        return

    if not target.exists():
        print(f"ERROR: Target not found: {target}", file=sys.stderr)
        sys.exit(1)

    _install(label, plist_path, target, args.role, args.config)


def _install(
    label: str,
    plist_path: Path,
    target: Path,
    role: str,
    config: str | None,
) -> None:
    python = sys.executable

    # Build program arguments
    if target.suffix == ".py":
        program_args = [python, str(target)]
    else:
        program_args = [str(target)]

    if config:
        program_args += ["--config", config]

    if role == "client":
        # On macOS dev we typically use fallback hotkey unless Accessibility is granted
        pass  # user can add --fallback-hotkey manually or edit the plist

    plist_data = {
        "Label": label,
        "ProgramArguments": program_args,
        "RunAtLoad": True,
        "KeepAlive": False,  # start at login but do NOT restart if user quits
        "WorkingDirectory": str(target.parent),
        "StandardOutPath": str(Path.home() / f"Library/Logs/{label}.log"),
        "StandardErrorPath": str(Path.home() / f"Library/Logs/{label}.err"),
    }

    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    with open(plist_path, "wb") as fh:
        plistlib.dump(plist_data, fh)

    print(f"Plist written to {plist_path}")

    # Unload first in case it already exists
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)

    result = subprocess.run(
        ["launchctl", "load", str(plist_path)],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"launchd agent '{label}' loaded and will start on next login.")
        # Also start it now
        subprocess.run(["launchctl", "start", label], capture_output=True)
        print(f"Agent started: {label}")
    else:
        print(f"ERROR loading agent: {result.stderr}", file=sys.stderr)
        sys.exit(1)


def _unload(plist_path: Path, label: str) -> None:
    if not plist_path.exists():
        print(f"Plist not found: {plist_path}")
        return
    subprocess.run(["launchctl", "stop",   label],         capture_output=True)
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    plist_path.unlink()
    print(f"Agent '{label}' removed.")


if __name__ == "__main__":
    main()
