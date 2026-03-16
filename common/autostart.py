"""
autostart.py — Detect and toggle Windows Task Scheduler auto-start entries.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Optional


# Task name prefixes used by the installer
_SERVER_TASK = "AlarmSystem_Server"
_CLIENT_TASK = "AlarmSystem_Client"


def _run_schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["schtasks", *args],
        capture_output=True, text=True,
    )


def _find_task_name(role: str, room_slug: str = "") -> Optional[str]:
    """Return the exact task name if it exists in Task Scheduler, else None."""
    if role == "server":
        candidates = [_SERVER_TASK]
    else:
        # Client tasks may have a slug suffix: AlarmSystem_Client_zimmer_1
        candidates = []
        if room_slug:
            candidates.append(f"{_CLIENT_TASK}_{room_slug}")
        candidates.append(_CLIENT_TASK)

    for name in candidates:
        r = _run_schtasks("/Query", "/TN", name)
        if r.returncode == 0:
            return name
    return None


def _sanitize_slug(room_name: str) -> str:
    """Convert 'Zimmer 1' to 'zimmer_1'."""
    import re
    return re.sub(r"[^a-z0-9]+", "_", room_name.lower()).strip("_")


def is_autostart_enabled(role: str, room_name: str = "") -> Optional[bool]:
    """
    Check if auto-start is enabled for the given role.
    Returns True/False, or None if no scheduled task was found.
    """
    if sys.platform != "win32":
        return None
    slug = _sanitize_slug(room_name) if room_name else ""
    task = _find_task_name(role, slug)
    if not task:
        return None
    r = _run_schtasks("/Query", "/TN", task, "/V", "/FO", "LIST")
    if r.returncode != 0:
        return None
    # Look for "Scheduled Task State:" or "Status:" line
    for line in r.stdout.splitlines():
        low = line.lower().strip()
        if "status des geplanten tasks" in low or "scheduled task state" in low:
            return "deaktiviert" not in low and "disabled" not in low
    return True  # assume enabled if we can't tell


def set_autostart(role: str, room_name: str, enabled: bool) -> bool:
    """Enable or disable the scheduled task. Returns True on success."""
    if sys.platform != "win32":
        return False
    slug = _sanitize_slug(room_name) if room_name else ""
    task = _find_task_name(role, slug)
    if not task:
        return False
    flag = "/ENABLE" if enabled else "/DISABLE"
    r = _run_schtasks("/Change", "/TN", task, flag)
    return r.returncode == 0
