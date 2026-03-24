"""
installer.py — Interactive Windows installer for the Alarm System.

Bundles server + client into a single executable.  When run it:
  1. Asks whether this PC is the Server or a Client room.
  2. Probes the network to check if a server / clients are already running.
  3. Asks for configuration (room name, server IP, port, hotkey).
  4. Copies files to C:\\Program Files\\AlarmSystem\\
  5. Writes a config .toml file.
  6. Registers a Windows Task Scheduler job for auto-start at logon/boot.
  7. Optionally starts the service immediately.

Build with PyInstaller (see scripts/build_executables.sh):
    pyinstaller scripts/alarm_installer.spec

The resulting alarm_installer.exe is fully self-contained.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import textwrap
import threading
import tkinter as tk
from tkinter import messagebox, ttk

# Import version
try:
    from common.version import __version__
except ImportError:
    try:
        # When run as -m scripts.installer from repo root
        import sys
        _repo = Path(__file__).resolve().parent.parent
        if str(_repo) not in sys.path:
            sys.path.insert(0, str(_repo))
        from common.version import __version__
    except ImportError:
        __version__ = "unknown"
from pathlib import Path
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Embedded module bootstrap — when frozen, add the bundle root to sys.path
# so that common/, server/, client/ are importable.
# ---------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    _bundle = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    sys.path.insert(0, str(_bundle))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

APP_NAME      = "AlarmSystem"
INSTALL_DIR   = Path(os.environ.get("PROGRAMFILES", "C:\\Program Files")) / APP_NAME
TASK_SERVER   = "AlarmSystem_Server"
TASK_CLIENT   = "AlarmSystem_Client"
DEFAULT_PORT  = 9999
PROBE_TIMEOUT = 2.0   # seconds for network probe


# ---------------------------------------------------------------------------
# Network probe helpers
# ---------------------------------------------------------------------------

def probe_server(host: str, port: int, timeout: float = PROBE_TIMEOUT) -> bool:
    """Return True if a TCP connection to host:port succeeds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_websocket(host: str, port: int) -> bool:
    """
    Try a WebSocket handshake to see if the alarm server is running.
    Returns True if we get any response (even a rejection means it's up).
    """
    try:
        with socket.create_connection((host, port), timeout=PROBE_TIMEOUT) as s:
            s.sendall(
                f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\n".encode()
                + b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                  b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                  b"Sec-WebSocket-Version: 13\r\n\r\n"
            )
            data = s.recv(256)
            return bool(data)
    except Exception:
        return False


def get_local_ip() -> str:
    """Return this machine's LAN IP (best guess)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


# ---------------------------------------------------------------------------
# Task Scheduler helpers (Windows only)
# ---------------------------------------------------------------------------

def _task_xml(exe: Path, role: str, config_path: Path) -> str:
    desc = f"Alarm System {'Server' if role == 'server' else 'Client'} — auto-start"
    args = f'--config "{config_path}" --gui'
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-16"?>
        <Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
          <RegistrationInfo><Description>{desc}</Description></RegistrationInfo>
          <Triggers>
            <LogonTrigger><Enabled>true</Enabled></LogonTrigger>
            <BootTrigger><Enabled>true</Enabled><Delay>PT10S</Delay></BootTrigger>
          </Triggers>
          <Principals>
            <Principal id="Author">
              <LogonType>InteractiveToken</LogonType>
              <RunLevel>HighestAvailable</RunLevel>
            </Principal>
          </Principals>
          <Settings>
            <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
            <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
            <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
            <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
            <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
            <Enabled>true</Enabled>
          </Settings>
          <Actions Context="Author">
            <Exec>
              <Command>{exe}</Command>
              <Arguments>{args}</Arguments>
              <WorkingDirectory>{exe.parent}</WorkingDirectory>
            </Exec>
          </Actions>
        </Task>
    """)


def register_task(task_name: str, exe: Path, role: str, config_path: Path) -> bool:
    xml = _task_xml(exe, role, config_path)
    tmp = Path(os.environ.get("TEMP", "C:\\Temp")) / f"{task_name}.xml"
    tmp.write_text(xml, encoding="utf-16")
    try:
        subprocess.run(["schtasks", "/Delete", "/TN", task_name, "/F"],
                       capture_output=True)
        r = subprocess.run(
            ["schtasks", "/Create", "/TN", task_name, "/XML", str(tmp), "/F"],
            capture_output=True, text=True,
        )
        return r.returncode == 0
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass


def start_task(task_name: str) -> None:
    """Start the scheduled task, but only if the process isn't already running."""
    # Determine exe name from task name
    if "Server" in task_name:
        exe_name = "alarm_server.exe"
    else:
        exe_name = "alarm_client.exe"

    # Check if already running
    try:
        r = subprocess.run(["tasklist"], capture_output=True, text=True)
        if exe_name.lower() in (r.stdout or "").lower():
            return  # Already running — skip
    except Exception:
        pass

    subprocess.run(["schtasks", "/Run", "/TN", task_name], capture_output=True)


# ---------------------------------------------------------------------------
# Shortcut helpers (Windows — uses PowerShell + WScript.Shell COM)
# ---------------------------------------------------------------------------

def _create_shortcut(lnk_path: Path, target_exe: Path, arguments: str,
                     description: str, working_dir: Optional[Path] = None,
                     icon_path: Optional[Path] = None) -> bool:
    """Create a .lnk shortcut file via PowerShell."""
    wd = working_dir or target_exe.parent
    icon_line = ""
    if icon_path and icon_path.exists():
        icon_line = f'$s.IconLocation = "{icon_path},0"; '
    # PowerShell script using COM to create a standard Windows shortcut
    ps = (
        f'$ws = New-Object -ComObject WScript.Shell; '
        f'$s = $ws.CreateShortcut("{lnk_path}"); '
        f'$s.TargetPath = "{target_exe}"; '
        f'$s.Arguments = \'{arguments}\'; '
        f'$s.WorkingDirectory = "{wd}"; '
        f'$s.Description = "{description}"; '
        f'{icon_line}'
        f'$s.Save()'
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True,
        )
        return r.returncode == 0
    except Exception:
        return False


def create_shortcuts(exe: Path, role: str, config_path: Path,
                     room_name: Optional[str] = None) -> Tuple[bool, bool]:
    """Create Desktop and Start Menu shortcuts. Returns (desktop_ok, startmenu_ok)."""
    if role == "server":
        role_de = "Alarm Server"
    else:
        role_de = f"Alarm Client — {room_name}" if room_name else "Alarm Client"
    desc = f"Alarmsystem — {role_de}"

    # Copy icon to install dir
    ico_name = "alarm_server.ico" if role == "server" else "alarm_client.ico"
    ico_src = _bundle_file(f"assets/{ico_name}")
    ico_dest = exe.parent / ico_name
    if ico_src and ico_src.exists():
        shutil.copy2(ico_src, ico_dest)
    icon_path = ico_dest if ico_dest.exists() else None

    if getattr(sys, "frozen", False):
        # Frozen: shortcut points to the renamed exe
        target = exe
        args = f'--config "{config_path}" --gui'
        work_dir = exe.parent
    else:
        # Unfrozen (dev): shortcut points to pythonw.exe with -m module
        repo_root = Path(__file__).parent.parent.resolve()
        python_dir = Path(sys.executable).resolve().parent
        # Use pythonw.exe (no console window) if available
        pythonw = python_dir / "pythonw.exe"
        target = pythonw if pythonw.exists() else Path(sys.executable).resolve()
        module = "server.server" if role == "server" else "client.client"
        args = f'-m {module} --config "{config_path}" --gui'
        work_dir = repo_root

    # Desktop shortcut
    desktop = Path(os.environ.get("USERPROFILE", "C:\\Users\\Public")) / "Desktop"
    desktop_ok = _create_shortcut(
        desktop / f"{role_de}.lnk", target, args, desc,
        working_dir=work_dir, icon_path=icon_path,
    )

    # Start Menu shortcut (per-user Programs folder)
    start_menu = (
        Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows"
        / "Start Menu" / "Programs"
    )
    if start_menu.exists():
        startmenu_ok = _create_shortcut(
            start_menu / f"{role_de}.lnk", target, args, desc,
            working_dir=work_dir, icon_path=icon_path,
        )
    else:
        startmenu_ok = False

    # Uninstaller shortcut (only create once — check if it exists)
    uninstall_lnk = desktop / "Alarmsystem deinstallieren.lnk"
    if not uninstall_lnk.exists():
        if getattr(sys, "frozen", False):
            uninst_target = INSTALL_DIR / "alarm_installer.exe"
            # Copy installer to install dir if not there yet
            if not uninst_target.exists():
                try:
                    shutil.copy2(sys.executable, uninst_target)
                except Exception:
                    pass
            uninst_args = "--uninstall"
        else:
            uninst_target = target  # reuse python/pythonw
            uninst_args = f"-m scripts.installer --uninstall"

        ico_uninst = INSTALL_DIR / "alarm.ico"
        _create_shortcut(
            uninstall_lnk, uninst_target, uninst_args,
            "Alarmsystem deinstallieren",
            icon_path=ico_uninst if ico_uninst.exists() else None,
        )

    return desktop_ok, startmenu_ok


# ---------------------------------------------------------------------------
# Config writers
# ---------------------------------------------------------------------------

def write_server_config(path: Path, port: int, silent_alarm: bool) -> None:
    path.write_text(textwrap.dedent(f"""\
        [server]
        host                  = "0.0.0.0"
        port                  = {port}
        heartbeat_timeout_sec = 15
        silent_alarm          = {"true" if silent_alarm else "false"}
        log_file              = ""
    """), encoding="utf-8")


def write_client_config(path: Path, room: str, server_ip: str,
                        port: int, hotkey: str) -> None:
    path.write_text(textwrap.dedent(f"""\
        [client]
        room_name   = "{room}"
        server_ip   = "{server_ip}"
        server_port = {port}
        hotkey      = "{hotkey}"
        alarm_sound = ""
        log_file    = ""
    """), encoding="utf-8")


# ---------------------------------------------------------------------------
# Pre-install detection & cleanup helpers
# ---------------------------------------------------------------------------

def detect_existing_installation(role: str, room_slug: str = "") -> dict:
    """Check whether a previous installation exists for the given role.

    Returns a dict with keys:
      exe_exists      – True if the exe is already in INSTALL_DIR
      task_exists      – True if a matching Task Scheduler entry exists
      task_name        – the name of the found task (or None)
      process_running  – True if the exe is currently running
    """
    exe_name = "alarm_server.exe" if role == "server" else "alarm_client.exe"
    exe_path = INSTALL_DIR / exe_name

    # Check exe on disk
    exe_exists = exe_path.exists()

    # Check Task Scheduler
    if role == "server":
        task_candidates = [TASK_SERVER]
    else:
        task_candidates = []
        if room_slug:
            task_candidates.append(f"{TASK_CLIENT}_{room_slug}")
        task_candidates.append(TASK_CLIENT)

    task_name = None
    task_exists = False
    for tn in task_candidates:
        r = subprocess.run(["schtasks", "/Query", "/TN", tn],
                           capture_output=True, text=True)
        if r.returncode == 0:
            task_exists = True
            task_name = tn
            break

    # Check running process
    process_running = False
    try:
        r = subprocess.run(["tasklist"], capture_output=True, text=True)
        stdout = r.stdout or ""
        process_running = exe_name.lower() in stdout.lower()
    except Exception:
        pass

    return {
        "exe_exists": exe_exists,
        "task_exists": task_exists,
        "task_name": task_name,
        "process_running": process_running,
    }


def cleanup_existing(role: str, room_slug: str = "") -> None:
    """Stop running processes, remove old task, and delete old shortcuts."""
    exe_name = "alarm_server.exe" if role == "server" else "alarm_client.exe"

    # Kill running process — for clients, only stop via scheduled task
    # to avoid killing OTHER clients. For server, kill by image name.
    if role == "server":
        try:
            subprocess.run(["taskkill", "/IM", exe_name, "/F"],
                           capture_output=True, text=True, timeout=10)
        except Exception:
            pass
    else:
        # Stop only this client's scheduled task (does not affect others)
        task_name = f"{TASK_CLIENT}_{room_slug}" if room_slug else TASK_CLIENT
        try:
            subprocess.run(["schtasks", "/End", "/TN", task_name],
                           capture_output=True, text=True, timeout=10)
        except Exception:
            pass

    # Delete scheduled task(s)
    if role == "server":
        task_candidates = [TASK_SERVER]
    else:
        task_candidates = []
        if room_slug:
            task_candidates.append(f"{TASK_CLIENT}_{room_slug}")
        task_candidates.append(TASK_CLIENT)

    for tn in task_candidates:
        subprocess.run(["schtasks", "/Delete", "/TN", tn, "/F"],
                       capture_output=True, text=True, timeout=10)

    # Also clean orphan tasks for this role
    for orphan in find_orphan_tasks():
        subprocess.run(["schtasks", "/Delete", "/TN", orphan, "/F"],
                       capture_output=True, text=True, timeout=10)


def find_orphan_tasks() -> list:
    """Find AlarmSystem_ tasks whose target exe no longer exists."""
    orphans = []
    try:
        r = subprocess.run(
            ["schtasks", "/Query", "/FO", "CSV", "/V"],
            capture_output=True, text=True, timeout=15,
        )
        stdout = r.stdout or ""
        for line in stdout.splitlines():
            if "AlarmSystem_" not in line:
                continue
            # CSV fields: "hostname","task_name","next_run",...,"task_to_run",...
            parts = line.split('","')
            if len(parts) < 9:
                continue
            task_name = parts[1].strip('"').strip("\\")
            # Field index 8 is typically "Task To Run"
            exe_field = parts[8].strip('"') if len(parts) > 8 else ""
            if exe_field and not Path(exe_field).exists():
                orphans.append(task_name)
    except Exception:
        pass
    return orphans


def verify_autostart(task_name: str) -> bool:
    """After installation, verify the scheduled task is correctly configured.

    Checks:
      1. Task exists and is enabled
      2. The exe it points to actually exists
    Returns True if everything is OK.
    """
    r = subprocess.run(
        ["schtasks", "/Query", "/TN", task_name, "/V", "/FO", "LIST"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return False

    stdout = r.stdout or ""
    # Check enabled
    enabled = True
    for line in stdout.splitlines():
        low = line.lower().strip()
        if "status des geplanten tasks" in low or "scheduled task state" in low:
            if "deaktiviert" in low or "disabled" in low:
                enabled = False
            break

    return enabled


# ---------------------------------------------------------------------------
# Uninstaller helpers
# ---------------------------------------------------------------------------

def find_installed_clients() -> list[dict]:
    """Find all installed client configs, shortcuts and scheduled tasks.

    Returns a list of dicts: [{"slug": "zimmer_1", "room": "Zimmer 1", "config": Path|None}, ...]
    """
    seen_slugs: set[str] = set()
    clients: list[dict] = []

    # 1. Config files in INSTALL_DIR: client_config_*.toml
    if INSTALL_DIR.exists():
        for cfg_file in sorted(INSTALL_DIR.glob("client_config_*.toml")):
            slug = cfg_file.stem.replace("client_config_", "")
            room = slug
            try:
                text = cfg_file.read_text(encoding="utf-8")
                for line in text.splitlines():
                    if line.strip().startswith("room_name"):
                        room = line.split("=", 1)[1].strip().strip('"')
                        break
            except Exception:
                pass
            if slug not in seen_slugs:
                seen_slugs.add(slug)
                clients.append({"slug": slug, "room": room, "config": cfg_file})

        # Also check generic client_config.toml (no suffix)
        generic = INSTALL_DIR / "client_config.toml"
        if generic.exists():
            room = "Client"
            try:
                text = generic.read_text(encoding="utf-8")
                for line in text.splitlines():
                    if line.strip().startswith("room_name"):
                        room = line.split("=", 1)[1].strip().strip('"')
                        break
            except Exception:
                pass
            slug = room.lower().replace(" ", "_")
            if slug not in seen_slugs:
                seen_slugs.add(slug)
                clients.append({"slug": slug, "room": room, "config": generic})

    # 2. Desktop shortcuts matching "Alarm Client*"
    desktop = Path(os.environ.get("USERPROFILE", "")) / "Desktop"
    if desktop.exists():
        for lnk in desktop.glob("Alarm Client*.lnk"):
            # Extract room name from shortcut name: "Alarm Client — Room Name.lnk"
            name = lnk.stem
            for sep in (" — ", " - "):
                if sep in name:
                    room = name.split(sep, 1)[1].strip()
                    break
            else:
                room = "Client"
            slug = room.lower().replace(" ", "_")
            if slug not in seen_slugs:
                seen_slugs.add(slug)
                cfg_path = INSTALL_DIR / f"client_config_{slug}.toml"
                clients.append({
                    "slug": slug, "room": room,
                    "config": cfg_path if cfg_path.exists() else None,
                })

    # 3. Scheduled tasks matching AlarmSystem_Client_*
    try:
        r = subprocess.run(
            ["schtasks", "/Query", "/FO", "LIST"],
            capture_output=True, text=True, timeout=10,
        )
        if r.stdout:
            for line in r.stdout.splitlines():
                line = line.strip()
                if line.startswith("TaskName:") and "AlarmSystem_Client_" in line:
                    task = line.split("\\")[-1].strip()
                    slug = task.replace("AlarmSystem_Client_", "")
                    room = slug.replace("_", " ").title()
                    if slug not in seen_slugs:
                        seen_slugs.add(slug)
                        cfg_path = INSTALL_DIR / f"client_config_{slug}.toml"
                        clients.append({
                            "slug": slug, "room": room,
                            "config": cfg_path if cfg_path.exists() else None,
                        })
    except Exception:
        pass

    return clients


def is_server_installed() -> bool:
    """Return True if the server is installed."""
    return (INSTALL_DIR / "alarm_server.exe").exists() or \
           (INSTALL_DIR / "server_config.toml").exists()


def uninstall(role: str, room_slug: str = "", room_name: str = "") -> list[str]:
    """Uninstall a role completely. Returns a list of actions taken."""
    log = _get_install_logger()
    actions = []
    exe_name = "alarm_server.exe" if role == "server" else "alarm_client.exe"
    # Resolve room_name from config if not provided
    if role == "client" and not room_name and room_slug:
        cfg_path = INSTALL_DIR / f"client_config_{room_slug}.toml"
        if cfg_path.exists():
            try:
                text = cfg_path.read_text(encoding="utf-8")
                for line in text.splitlines():
                    if line.strip().startswith("room_name"):
                        room_name = line.split("=", 1)[1].strip().strip('"')
                        break
            except Exception:
                pass
        if not room_name:
            room_name = room_slug.replace("_", " ").title()

    # 1. Kill process
    try:
        r = subprocess.run(["taskkill", "/IM", exe_name, "/F"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            actions.append(f"Prozess {exe_name} beendet")
            log.info("Uninstall: killed %s", exe_name)
    except Exception:
        pass

    # 2. Delete scheduled task(s)
    if role == "server":
        task_names = [TASK_SERVER]
    else:
        task_names = []
        if room_slug:
            task_names.append(f"{TASK_CLIENT}_{room_slug}")
        task_names.append(TASK_CLIENT)

    for tn in task_names:
        r = subprocess.run(["schtasks", "/Delete", "/TN", tn, "/F"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            actions.append(f"Aufgabe {tn} entfernt")
            log.info("Uninstall: deleted task %s", tn)

    # 3. Delete shortcuts
    desktop = Path(os.environ.get("USERPROFILE", "C:\\Users\\Public")) / "Desktop"
    start_menu = (
        Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows"
        / "Start Menu" / "Programs"
    )
    if role == "server":
        shortcut_names = ["Alarm Server.lnk"]
    else:
        # Try both room_name (actual) and room_slug (legacy) variants
        shortcut_names = [
            f"Alarm Client — {room_name}.lnk",
            f"Alarm Client — {room_slug}.lnk",
            "Alarm Client.lnk",
        ]

    for folder in [desktop, start_menu]:
        # Delete exact matches
        for name in shortcut_names:
            lnk = folder / name
            if lnk.exists():
                try:
                    lnk.unlink()
                    actions.append(f"Verknüpfung {lnk.name} entfernt")
                    log.info("Uninstall: deleted shortcut %s", lnk)
                except Exception:
                    pass
        # Also glob for any remaining shortcuts with this room name
        if role == "client" and room_name:
            for lnk in folder.glob(f"Alarm Client*{room_name}*.lnk"):
                if lnk.exists():
                    try:
                        lnk.unlink()
                        actions.append(f"Verknüpfung {lnk.name} entfernt")
                        log.info("Uninstall: deleted shortcut %s", lnk)
                    except Exception:
                        pass

    # 4. Delete config file
    if role == "server":
        cfg_path = INSTALL_DIR / "server_config.toml"
    else:
        cfg_path = INSTALL_DIR / f"client_config_{room_slug}.toml"

    if cfg_path.exists():
        try:
            cfg_path.unlink()
            actions.append(f"Konfiguration {cfg_path.name} entfernt")
            log.info("Uninstall: deleted config %s", cfg_path)
        except Exception:
            pass

    # 5. Delete exe (only if the other role doesn't need it)
    exe_path = INSTALL_DIR / exe_name
    other_exe = "alarm_client.exe" if role == "server" else "alarm_server.exe"
    if exe_path.exists():
        # Check if other role still needs the install dir
        other_exists = (INSTALL_DIR / other_exe).exists()
        other_configs = list(INSTALL_DIR.glob("client_config_*.toml")) if role == "server" else []
        if role == "client":
            other_configs = [INSTALL_DIR / "server_config.toml"] if (INSTALL_DIR / "server_config.toml").exists() else []
            other_configs += [f for f in INSTALL_DIR.glob("client_config_*.toml") if f != cfg_path]

        try:
            exe_path.unlink()
            actions.append(f"Programm {exe_name} entfernt")
            log.info("Uninstall: deleted exe %s", exe_path)
        except Exception:
            pass

    # 6. Clean up if install dir is empty (no more configs or exes)
    remaining = list(INSTALL_DIR.glob("*.exe")) + list(INSTALL_DIR.glob("*.toml"))
    if not remaining and INSTALL_DIR.exists():
        try:
            shutil.rmtree(INSTALL_DIR)
            actions.append(f"Ordner {INSTALL_DIR} entfernt")
            log.info("Uninstall: removed install dir %s", INSTALL_DIR)
        except Exception:
            pass

        # Also remove uninstaller shortcut
        for folder in [desktop, start_menu]:
            lnk = folder / "Alarmsystem deinstallieren.lnk"
            if lnk.exists():
                try:
                    lnk.unlink()
                except Exception:
                    pass

    return actions


# ---------------------------------------------------------------------------
# Safe-install infrastructure (pre-flight, backup, rollback, logging)
# ---------------------------------------------------------------------------

import logging as _logging
import time as _time

_install_log: _logging.Logger | None = None


def _get_install_logger() -> _logging.Logger:
    """Return (and lazily create) a file logger for install actions."""
    global _install_log
    if _install_log is None:
        _install_log = _logging.getLogger("alarm.installer")
        _install_log.setLevel(_logging.DEBUG)
        # Try install dir first, fall back to temp dir
        log_path = INSTALL_DIR / "install.log"
        try:
            INSTALL_DIR.mkdir(parents=True, exist_ok=True)
            fh = _logging.FileHandler(str(log_path), encoding="utf-8")
        except (PermissionError, OSError):
            import tempfile
            log_path = Path(tempfile.gettempdir()) / "alarm_install.log"
            fh = _logging.FileHandler(str(log_path), encoding="utf-8")
        fh.setFormatter(_logging.Formatter(
            "%(asctime)s  %(levelname)-7s  %(message)s"))
        _install_log.addHandler(fh)
    return _install_log


def preflight_checks() -> list[str]:
    """Run pre-flight checks before touching the system.

    Returns a list of error messages.  Empty list → all OK.
    """
    errors: list[str] = []

    # 1. Verify we can write to INSTALL_DIR
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    probe = INSTALL_DIR / ".install_probe"
    try:
        probe.write_text("probe", encoding="utf-8")
        probe.unlink()
    except PermissionError:
        errors.append(
            f"Keine Schreibrechte auf {INSTALL_DIR}. "
            "Bitte als Administrator ausführen.")
    except Exception as exc:
        errors.append(f"Schreibtest fehlgeschlagen: {exc}")

    # 2. Disk space (need at least 100 MB free)
    try:
        import shutil as _shutil
        usage = _shutil.disk_usage(INSTALL_DIR)
        free_mb = usage.free / (1024 * 1024)
        if free_mb < 100:
            errors.append(
                f"Zu wenig Speicherplatz: {free_mb:.0f} MB frei "
                f"(mindestens 100 MB erforderlich).")
    except Exception:
        pass  # non-critical

    # 3. schtasks is available
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", "__install_probe__"],
                           capture_output=True, text=True)
        # returncode 1 is fine (task not found) — we just need the binary
        if r.returncode not in (0, 1):
            errors.append("schtasks ist nicht verfügbar oder blockiert.")
    except FileNotFoundError:
        errors.append("schtasks.exe wurde nicht gefunden.")
    except Exception as exc:
        errors.append(f"schtasks-Prüfung fehlgeschlagen: {exc}")

    return errors


def backup_existing(role: str, room_slug: str = "") -> dict:
    """Create backups of existing files before overwriting.

    Returns a dict describing what was backed up (used by rollback).
    """
    log = _get_install_logger()
    ts = _time.strftime("%Y%m%d_%H%M%S")
    backup_dir = INSTALL_DIR / "backups" / ts
    backed_up: dict = {"backup_dir": str(backup_dir), "files": [], "task_xml": None}

    exe_name = "alarm_server.exe" if role == "server" else "alarm_client.exe"
    exe_path = INSTALL_DIR / exe_name

    # Backup exe
    if exe_path.exists():
        backup_dir.mkdir(parents=True, exist_ok=True)
        dst = backup_dir / exe_name
        try:
            shutil.copy2(exe_path, dst)
            backed_up["files"].append({"src": str(exe_path), "bak": str(dst)})
            log.info("Backup: %s → %s", exe_path, dst)
        except Exception as exc:
            log.warning("Backup von %s fehlgeschlagen: %s", exe_path, exc)

    # Backup config(s)
    if role == "server":
        cfg_candidates = [INSTALL_DIR / "server_config.toml"]
    else:
        cfg_candidates = list(INSTALL_DIR.glob(f"client_config_{room_slug}*.toml"))
        cfg_candidates += list(INSTALL_DIR.glob("client_config.toml"))

    for cfg_path in cfg_candidates:
        if cfg_path.exists():
            backup_dir.mkdir(parents=True, exist_ok=True)
            dst = backup_dir / cfg_path.name
            try:
                shutil.copy2(cfg_path, dst)
                backed_up["files"].append({"src": str(cfg_path), "bak": str(dst)})
                log.info("Backup: %s → %s", cfg_path, dst)
            except Exception as exc:
                log.warning("Backup von %s fehlgeschlagen: %s", cfg_path, exc)

    # Export current scheduled task to XML
    if role == "server":
        task_name = TASK_SERVER
    else:
        task_name = f"{TASK_CLIENT}_{room_slug}" if room_slug else TASK_CLIENT

    try:
        r = subprocess.run(
            ["schtasks", "/Query", "/TN", task_name, "/XML"],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and r.stdout:
            backup_dir.mkdir(parents=True, exist_ok=True)
            xml_path = backup_dir / f"{task_name}.xml"
            xml_path.write_text(r.stdout, encoding="utf-16")
            backed_up["task_xml"] = str(xml_path)
            backed_up["task_name"] = task_name
            log.info("Backup: Scheduled task %s → %s", task_name, xml_path)
    except Exception as exc:
        log.warning("Task-Backup fehlgeschlagen: %s", exc)

    return backed_up


def rollback(backup_info: dict) -> None:
    """Restore files and scheduled task from a backup created by backup_existing().

    Called automatically when any installation step fails.
    """
    log = _get_install_logger()
    log.warning("ROLLBACK gestartet — stelle vorherigen Zustand wieder her")

    # Restore files
    for entry in backup_info.get("files", []):
        src = Path(entry["src"])
        bak = Path(entry["bak"])
        if bak.exists():
            try:
                shutil.copy2(bak, src)
                log.info("Wiederhergestellt: %s ← %s", src, bak)
            except Exception as exc:
                log.error("Wiederherstellung fehlgeschlagen: %s — %s", src, exc)

    # Restore scheduled task
    xml_path = backup_info.get("task_xml")
    task_name = backup_info.get("task_name")
    if xml_path and task_name and Path(xml_path).exists():
        try:
            subprocess.run(
                ["schtasks", "/Create", "/TN", task_name,
                 "/XML", xml_path, "/F"],
                capture_output=True, text=True,
            )
            log.info("Scheduled task %s wiederhergestellt", task_name)
        except Exception as exc:
            log.error("Task-Wiederherstellung fehlgeschlagen: %s", exc)

    log.warning("ROLLBACK abgeschlossen")


def safe_install(role: str, install_fn, room_slug: str = "") -> dict:
    """Orchestrate a safe, transactional installation.

    1. Pre-flight checks
    2. Backup existing installation
    3. Cleanup old processes / tasks
    4. Run the actual install function
    5. Verify autostart
    6. Rollback on ANY failure

    *install_fn* receives (backup_info) and must return a dict with
    at least {task_ok, exe, desk_ok, start_ok, task_name}.
    Raises on failure (triggering rollback).
    """
    log = _get_install_logger()
    log.info("=" * 60)
    log.info("Installation gestartet — Rolle: %s", role)

    # Step 1: pre-flight
    errors = preflight_checks()
    if errors:
        msg = "\n".join(f"• {e}" for e in errors)
        log.error("Pre-flight fehlgeschlagen:\n%s", msg)
        return {"ok": False, "error": f"Vorprüfung fehlgeschlagen:\n\n{msg}"}

    log.info("Pre-flight checks bestanden")

    # Step 2: backup
    backup_info = backup_existing(role, room_slug)
    log.info("Backup erstellt: %s", backup_info.get("backup_dir", "n/a"))

    # Step 3: cleanup
    try:
        cleanup_existing(role, room_slug)
        log.info("Alte Installation bereinigt")
    except Exception as exc:
        log.error("Bereinigung fehlgeschlagen: %s", exc)
        rollback(backup_info)
        return {"ok": False, "error": f"Bereinigung fehlgeschlagen: {exc}"}

    # Step 4: run actual install
    try:
        result = install_fn(backup_info)
        log.info("Installation ausgeführt")
    except Exception as exc:
        log.error("Installation fehlgeschlagen: %s — starte Rollback", exc)
        rollback(backup_info)
        return {"ok": False, "error": f"Installation fehlgeschlagen: {exc}\n\nAlle Änderungen wurden rückgängig gemacht."}

    # Step 5: verify
    task_name = result.get("task_name", "")
    if task_name and not verify_autostart(task_name):
        log.error("Autostart-Überprüfung fehlgeschlagen — starte Rollback")
        rollback(backup_info)
        return {"ok": False, "error": "Autostart konnte nicht verifiziert werden.\n\nAlle Änderungen wurden rückgängig gemacht."}

    log.info("Autostart verifiziert: %s", task_name)
    log.info("Installation erfolgreich abgeschlossen")
    result["ok"] = True
    return result


# ---------------------------------------------------------------------------
# Asset helpers
# ---------------------------------------------------------------------------

def _bundle_file(rel: str) -> Optional[Path]:
    """Return path to a bundled asset (works frozen and unfrozen)."""
    if getattr(sys, "frozen", False):
        p = Path(sys._MEIPASS) / rel  # type: ignore[attr-defined]
    else:
        p = Path(__file__).parent.parent / rel
    return p if p.exists() else None


def _sanitize_name(name: str) -> str:
    """Turn a room name like 'Zimmer 1' into a safe filename slug like 'zimmer_1'."""
    import re
    slug = name.lower().strip()
    slug = re.sub(r'[^a-z0-9äöüß]+', '_', slug)
    return slug.strip('_') or 'client'


def _copy_exe(role: str, dest: Path) -> Path:
    """Copy the appropriate exe to *dest* and return the launch target.

    Frozen (PyInstaller): copies the combined installer exe as
    alarm_server.exe / alarm_client.exe — the entry point detects its
    filename and dispatches to the correct module.

    Unfrozen (development): creates a .bat launcher that invokes the
    Python module directly, since copying python.exe is useless.
    """
    dest.mkdir(parents=True, exist_ok=True)
    module = "server.server" if role == "server" else "client.client"
    exe_name = "alarm_server.exe" if role == "server" else "alarm_client.exe"

    if getattr(sys, "frozen", False):
        # Frozen: copy ourselves (the combined installer binary)
        target = dest / exe_name
        src = _bundle_file(exe_name)
        if src and src != target:
            try:
                shutil.copy2(src, target)
            except PermissionError:
                pass  # exe locked by running instance — already in place
        else:
            try:
                shutil.copy2(sys.executable, target)
            except PermissionError:
                pass  # exe locked by running instance — already in place
    else:
        # Unfrozen (dev): create a .bat launcher instead
        repo_root = Path(__file__).parent.parent.resolve()
        python_exe = Path(sys.executable).resolve()
        bat_name = "alarm_server.bat" if role == "server" else "alarm_client.bat"
        target = dest / bat_name
        try:
            target.write_text(
                f'@echo off\r\n'
                f'cd /d "{repo_root}"\r\n'
                f'"{python_exe}" -m {module} %*\r\n',
                encoding="utf-8",
            )
        except PermissionError:
            pass  # bat locked — already in place

    # Also copy bundled assets (sound + icons)
    assets_dest = dest / "assets"
    assets_dest.mkdir(exist_ok=True)
    for asset_name in ["alarm.wav", "alarm.ico", "alarm_server.ico", "alarm_client.ico"]:
        src_asset = _bundle_file(f"assets/{asset_name}")
        if src_asset:
            try:
                shutil.copy2(src_asset, assets_dest / asset_name)
            except PermissionError:
                pass  # locked — already in place

    return target


# ---------------------------------------------------------------------------
# GUI — main installer window
# ---------------------------------------------------------------------------

class InstallerApp(tk.Tk):
    """Tkinter-based installer GUI."""

    _BG    = "#1a1a2e"
    _FG    = "#e0e0e0"
    _BLUE  = "#0f3460"
    _ACCENT = "#e94560"
    _GREEN = "#00b894"
    _AMBER = "#fdcb6e"

    @staticmethod
    def _add_context_menu(entry: tk.Entry) -> None:
        """Add right-click context menu (Cut/Copy/Paste/Select All) to an Entry."""
        menu = tk.Menu(entry, tearoff=0)
        menu.add_command(label="Ausschneiden", command=lambda: entry.event_generate("<<Cut>>"))
        menu.add_command(label="Kopieren", command=lambda: entry.event_generate("<<Copy>>"))
        menu.add_command(label="Einfügen", command=lambda: entry.event_generate("<<Paste>>"))
        menu.add_separator()
        menu.add_command(label="Alles auswählen",
                         command=lambda: (entry.select_range(0, tk.END), entry.icursor(tk.END)))

        def _show(event):
            menu.tk_popup(event.x_root, event.y_root)
        entry.bind("<Button-3>", _show)
        # Mac trackpad / Control+click
        entry.bind("<Button-2>", _show)

    def __init__(self, dev_mode: bool = False) -> None:
        super().__init__()
        self._dev_mode = dev_mode
        self._update_mode = False
        title = f"Alarmsystem — Installation  v{__version__}"
        if dev_mode:
            title += "  [DEV MODE]"
        self.title(title)
        self.resizable(False, False)
        self.configure(bg=self._BG)
        self._center(620, 620)

        self._role: Optional[str] = None
        self._build_role_page()

    def _center(self, w: int, h: int) -> None:
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{(sw - w)//2}+{(sh - h)//2}")

    # ------------------------------------------------------------------
    # Page 1 — choose role
    # ------------------------------------------------------------------

    def _build_role_page(self) -> None:
        self._clear()

        tk.Label(self, text=f"Alarmsystem Installation  v{__version__}",
                 font=("Arial", 20, "bold"), bg=self._BG, fg=self._ACCENT).pack(pady=(30, 5))
        tk.Label(self, text="Wählen Sie die Rolle dieses PCs:",
                 font=("Arial", 13), bg=self._BG, fg=self._FG).pack(pady=(0, 25))

        frm = tk.Frame(self, bg=self._BG)
        frm.pack(pady=10)

        self._make_role_btn(frm, "🖥  SERVER",
                            "Zentrale Schaltstelle.\nNur ein Server pro Netzwerk.",
                            "server").grid(row=0, column=0, padx=20)

        self._make_role_btn(frm, "🔔  CLIENT",
                            "Patientenzimmer-PC.\nMehrere Clients pro Netzwerk.",
                            "client").grid(row=0, column=1, padx=20)

        tk.Label(self, text=f"Lokale IP dieses PCs: {get_local_ip()}",
                 font=("Arial", 10), bg=self._BG, fg="#888").pack(pady=(30, 0))

    def _make_role_btn(self, parent, title, desc, role) -> tk.Frame:
        frm = tk.Frame(parent, bg=self._BLUE, cursor="hand2",
                       relief="flat", bd=0)
        frm.bind("<Button-1>", lambda _e: self._on_role(role))

        tk.Label(frm, text=title, font=("Arial", 16, "bold"),
                 bg=self._BLUE, fg=self._FG, padx=20, pady=15).pack()
        tk.Label(frm, text=desc, font=("Arial", 10),
                 bg=self._BLUE, fg="#aaa", padx=20, pady=5,
                 justify="center", wraplength=180).pack()
        tk.Label(frm, text="▶  Auswählen", font=("Arial", 10, "bold"),
                 bg=self._BLUE, fg=self._ACCENT, pady=10).pack()

        for child in frm.winfo_children():
            child.bind("<Button-1>", lambda _e: self._on_role(role))

        return frm

    def _build_options_checkboxes(self, parent: tk.Frame, role: str) -> None:
        """Add installation option checkboxes, adapted to the selected role."""
        self._opt_clean = tk.BooleanVar(value=False)
        self._opt_update = tk.BooleanVar(value=False)
        self._opt_dev = tk.BooleanVar(value=False)

        opt_frm = tk.LabelFrame(parent, text="Optionen", font=("Arial", 10, "bold"),
                                bg=self._BG, fg="#888", bd=1, relief="groove",
                                padx=10, pady=8)
        opt_frm.pack(fill="x", padx=20, pady=(15, 0))

        role_de = "Server" if role == "server" else "Client"

        tk.Checkbutton(
            opt_frm, text=f"Komplette Deinstallation vor Installation\n"
                          f"(Alle bestehenden {role_de}-Installationen entfernen)",
            variable=self._opt_clean, font=("Arial", 9),
            bg=self._BG, fg=self._FG, selectcolor="#2a2a4e",
            activebackground=self._BG, activeforeground=self._FG,
            anchor="w", justify="left",
            command=self._on_clean_toggled).pack(anchor="w", pady=(0, 4))

        tk.Checkbutton(
            opt_frm, text=f"Bestehende {role_de}-Installation aktualisieren\n"
                          f"(Programm aktualisieren, Konfiguration beibehalten)",
            variable=self._opt_update, font=("Arial", 9),
            bg=self._BG, fg=self._FG, selectcolor="#2a2a4e",
            activebackground=self._BG, activeforeground=self._FG,
            anchor="w", justify="left",
            command=self._on_update_toggled).pack(anchor="w", pady=(0, 4))

        if role == "client":
            tk.Checkbutton(
                opt_frm, text="Entwicklungsmodus\n"
                              "(Mehrere Clients auf diesem PC erlauben)",
                variable=self._opt_dev, font=("Arial", 9),
                bg=self._BG, fg=self._FG, selectcolor="#2a2a4e",
                activebackground=self._BG, activeforeground=self._FG,
                anchor="w", justify="left").pack(anchor="w", pady=(0, 4))

    def _on_clean_toggled(self) -> None:
        """Clean and Update are mutually exclusive."""
        if self._opt_clean.get():
            self._opt_update.set(False)

    def _on_update_toggled(self) -> None:
        """Update and Clean are mutually exclusive."""
        if self._opt_update.get():
            self._opt_clean.set(False)

    def _on_role(self, role: str) -> None:
        self._role = role
        self._build_probe_page(role)

    def _run_clean_then_install_and(self, then_fn, role: str) -> None:
        """Show confirmation of what will be removed for the given role, then uninstall and proceed."""
        has_server = False
        clients = []

        if role == "server":
            srv_info = detect_existing_installation("server")
            has_server = srv_info["exe_exists"] or srv_info["task_exists"]
        else:
            clients = find_installed_clients()

        # Find role-specific shortcuts on desktop and start menu
        desktop = Path(os.environ.get("USERPROFILE", "C:\\Users\\Public")) / "Desktop"
        start_menu = (
            Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows"
            / "Start Menu" / "Programs"
        )
        shortcuts = []
        for folder in [desktop, start_menu]:
            if folder.exists():
                if role == "server":
                    for lnk in folder.glob("Alarm Server*"):
                        shortcuts.append(lnk)
                else:
                    for lnk in folder.glob("Alarm Client*"):
                        shortcuts.append(lnk)

        if not has_server and not clients and not shortcuts:
            # Nothing to clean, proceed directly
            then_fn()
            return

        # Build confirmation dialog
        self._clear()
        role_de = "Server" if role == "server" else "Client(s)"
        tk.Label(self, text=f"{role_de} — Folgendes wird deinstalliert:",
                 font=("Arial", 16, "bold"), bg=self._BG, fg=self._AMBER).pack(pady=(30, 15))

        items_frm = tk.Frame(self, bg=self._BG)
        items_frm.pack(fill="x", padx=50, pady=5)

        if has_server:
            tk.Label(items_frm, text="• Server (Programm + Konfiguration + Aufgabe)",
                     font=("Arial", 11), bg=self._BG, fg=self._FG,
                     anchor="w").pack(fill="x", pady=2)

        for client_info in clients:
            tk.Label(items_frm, text=f"• Client: {client_info['room']}",
                     font=("Arial", 11), bg=self._BG, fg=self._FG,
                     anchor="w").pack(fill="x", pady=2)

        if shortcuts:
            tk.Label(items_frm, text=f"• {len(shortcuts)} Verknüpfung(en) auf Desktop/Startmenü",
                     font=("Arial", 11), bg=self._BG, fg=self._FG,
                     anchor="w").pack(fill="x", pady=2)

        btn_frm = tk.Frame(self, bg=self._BG)
        btn_frm.pack(pady=(25, 10))

        tk.Button(btn_frm, text="Deinstallieren & Fortfahren",
                  font=("Arial", 12, "bold"), bg=self._ACCENT, fg="white",
                  relief="flat", padx=20, pady=8,
                  command=lambda: self._do_clean_install(then_fn, has_server, clients)
                  ).grid(row=0, column=0, padx=10)

        tk.Button(btn_frm, text="Abbrechen",
                  font=("Arial", 12), bg=self._BLUE, fg=self._FG,
                  relief="flat", padx=20, pady=8,
                  command=self._build_role_page
                  ).grid(row=0, column=1, padx=10)

    def _do_clean_install(self, then_fn, has_server, clients) -> None:
        """Execute the actual clean uninstall, then call then_fn."""
        self._clear()
        tk.Label(self, text="Deinstallation läuft…",
                 font=("Arial", 16, "bold"), bg=self._BG, fg=self._AMBER).pack(pady=(40, 20))
        bar = ttk.Progressbar(self, mode="indeterminate", length=400)
        bar.pack(pady=10)
        bar.start(10)
        log_lbl = tk.Label(self, text="", font=("Arial", 10),
                           bg=self._BG, fg=self._FG, wraplength=500, justify="left")
        log_lbl.pack(pady=10)

        def _worker():
            if has_server:
                self.after(0, lambda: log_lbl.config(text="Server wird deinstalliert…"))
                uninstall("server")
            for client_info in clients:
                slug = client_info["slug"]
                room = client_info.get("room", "")
                self.after(0, lambda s=room or slug: log_lbl.config(
                    text=f"Client '{s}' wird deinstalliert…"))
                uninstall("client", slug, room_name=room)
            self.after(0, then_fn)

        threading.Thread(target=_worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Page 2 — network probe
    # ------------------------------------------------------------------

    def _build_probe_page(self, role: str) -> None:
        self._clear()

        title = "Server konfigurieren" if role == "server" else "Client konfigurieren"
        tk.Label(self, text=title, font=("Arial", 18, "bold"),
                 bg=self._BG, fg=self._ACCENT).pack(pady=(30, 20))

        self._status_lbl = tk.Label(self, text="Netzwerk wird geprüft…",
                                    font=("Arial", 11), bg=self._BG, fg=self._AMBER)
        self._status_lbl.pack()

        self._probe_bar = ttk.Progressbar(self, mode="indeterminate", length=300)
        self._probe_bar.pack(pady=10)
        self._probe_bar.start(10)

        # Port entry (shared for both roles)
        pfrm = tk.Frame(self, bg=self._BG)
        pfrm.pack(pady=(15, 0))
        tk.Label(pfrm, text="Port:", font=("Arial", 11),
                 bg=self._BG, fg=self._FG).grid(row=0, column=0, sticky="e", padx=5)
        self._port_var = tk.StringVar(value=str(DEFAULT_PORT))
        tk.Entry(pfrm, textvariable=self._port_var, width=8,
                 font=("Arial", 11)).grid(row=0, column=1, sticky="w")

        if role == "client":
            tk.Label(pfrm, text="Server-IP:", font=("Arial", 11),
                     bg=self._BG, fg=self._FG).grid(row=1, column=0, sticky="e",
                                                     padx=5, pady=5)
            self._server_ip_var = tk.StringVar(value="")
            self._server_ip_entry = tk.Entry(pfrm, textvariable=self._server_ip_var,
                                             width=18, font=("Arial", 11))
            self._server_ip_entry.grid(row=1, column=1, sticky="w")

        self._next_btn = tk.Button(self, text="Weiter →",
                                   font=("Arial", 12, "bold"),
                                   bg=self._ACCENT, fg="white",
                                   relief="flat", padx=20, pady=8,
                                   state="disabled",
                                   command=self._on_probe_done)
        self._next_btn.pack(pady=20)

        tk.Button(self, text="← Zurück", font=("Arial", 10),
                  bg=self._BG, fg="#888", relief="flat",
                  command=self._build_role_page).pack()

        # Run probe in background
        threading.Thread(target=self._run_probe, args=(role,), daemon=True).start()

    def _run_probe(self, role: str) -> None:
        try:
            port = int(self._port_var.get())
        except ValueError:
            port = DEFAULT_PORT

        if role == "server":
            # Check if a server is already running on this port
            already = probe_server("127.0.0.1", port)
            if already:
                msg = ("⚠  Ein Server läuft bereits auf diesem PC (Port %d).\n"
                       "Die Installation überschreibt die Konfiguration." % port)
                color = self._AMBER
            else:
                msg = "✔  Kein Server gefunden — dieser PC wird zum Server."
                color = self._GREEN
        else:
            # Try to auto-detect server: localhost first, then LAN
            local_ip = get_local_ip()
            prefix = ".".join(local_ip.split(".")[:3])
            found_ip: Optional[str] = None

            # Check localhost, own IP, gateway, and common addresses
            candidates = [
                "127.0.0.1", local_ip,
                f"{prefix}.1", f"{prefix}.100", f"{prefix}.200",
            ]
            for ip in candidates:
                if probe_server(ip, port):
                    found_ip = ip
                    break

            if found_ip:
                self.after(0, lambda: self._server_ip_var.set(found_ip))
                msg = f"✔  Server gefunden: {found_ip}:{port}"
                color = self._GREEN
            else:
                msg = ("⚠  Kein Server gefunden im Netzwerk.\n"
                       "Server-IP bitte manuell eingeben.")
                color = self._AMBER

        self.after(0, lambda: self._probe_finished(msg, color))

    def _probe_finished(self, msg: str, color: str) -> None:
        self._probe_bar.stop()
        self._probe_bar.pack_forget()
        self._status_lbl.config(text=msg, fg=color)
        self._next_btn.config(state="normal")

    def _on_probe_done(self) -> None:
        if self._role == "server":
            self._build_server_config_page()
        else:
            self._build_client_config_page()

    # ------------------------------------------------------------------
    # Page 3a — server configuration
    # ------------------------------------------------------------------

    def _build_server_config_page(self) -> None:
        self._clear()

        tk.Label(self, text="Server-Konfiguration",
                 font=("Arial", 18, "bold"), bg=self._BG, fg=self._ACCENT).pack(pady=(30, 20))

        frm = tk.Frame(self, bg=self._BG)
        frm.pack(pady=5)

        tk.Label(frm, text="Port:", font=("Arial", 11),
                 bg=self._BG, fg=self._FG).grid(row=0, column=0, sticky="e", padx=10, pady=6)
        self._cfg_port = tk.StringVar(value=self._port_var.get())
        tk.Entry(frm, textvariable=self._cfg_port, width=10,
                 font=("Arial", 11)).grid(row=0, column=1, sticky="w")

        self._silent_var = tk.BooleanVar(value=True)
        tk.Checkbutton(frm, text="Stiller Alarm (auslösendes Zimmer wird nicht benachrichtigt)",
                       variable=self._silent_var,
                       font=("Arial", 10), bg=self._BG, fg=self._FG,
                       selectcolor=self._BLUE, activebackground=self._BG,
                       activeforeground=self._FG).grid(
            row=1, column=0, columnspan=2, sticky="w", padx=10, pady=8)

        tk.Label(frm, text=f"Installations-Pfad:\n{INSTALL_DIR}",
                 font=("Arial", 9), bg=self._BG, fg="#888",
                 justify="left").grid(row=2, column=0, columnspan=2,
                                      sticky="w", padx=10, pady=4)

        self._build_options_checkboxes(self, "server")

        self._make_install_btn("Server installieren", self._do_install_server).pack(pady=15)

        tk.Button(self, text="← Zurück", font=("Arial", 10),
                  bg=self._BG, fg="#888", relief="flat",
                  command=lambda: self._build_probe_page("server")).pack()

    # ------------------------------------------------------------------
    # Page 3b — client configuration
    # ------------------------------------------------------------------

    def _build_client_config_page(self) -> None:
        self._clear()

        tk.Label(self, text="Client-Konfiguration",
                 font=("Arial", 18, "bold"), bg=self._BG, fg=self._ACCENT).pack(pady=(30, 20))

        frm = tk.Frame(self, bg=self._BG)
        frm.pack(pady=5)

        def lbl(row, text):
            tk.Label(frm, text=text, font=("Arial", 11),
                     bg=self._BG, fg=self._FG).grid(
                row=row, column=0, sticky="e", padx=10, pady=6)

        lbl(0, "Zimmername:")
        self._room_var = tk.StringVar(value="Zimmer 1")
        tk.Entry(frm, textvariable=self._room_var, width=22,
                 font=("Arial", 11)).grid(row=0, column=1, sticky="w")

        lbl(1, "Server-IP:")
        self._sip_var = tk.StringVar(value=getattr(self, "_server_ip_var",
                                                    tk.StringVar()).get())
        tk.Entry(frm, textvariable=self._sip_var, width=22,
                 font=("Arial", 11)).grid(row=1, column=1, sticky="w")

        lbl(2, "Port:")
        self._cli_port = tk.StringVar(value=self._port_var.get())
        tk.Entry(frm, textvariable=self._cli_port, width=10,
                 font=("Arial", 11)).grid(row=2, column=1, sticky="w")

        lbl(3, "Tastenkürzel:")
        self._hotkey_var = tk.StringVar(value="alt+n")
        tk.Entry(frm, textvariable=self._hotkey_var, width=14,
                 font=("Arial", 11)).grid(row=3, column=1, sticky="w")
        tk.Label(frm, text="(z.B. alt+n, ctrl+F12)",
                 font=("Arial", 9), bg=self._BG, fg="#888").grid(
            row=3, column=2, sticky="w", padx=4)

        tk.Label(frm, text=f"Installations-Pfad:\n{INSTALL_DIR}",
                 font=("Arial", 9), bg=self._BG, fg="#888",
                 justify="left").grid(row=4, column=0, columnspan=3,
                                      sticky="w", padx=10, pady=4)

        self._build_options_checkboxes(self, "client")

        self._make_install_btn("Client installieren", self._do_install_client).pack(pady=15)

        tk.Button(self, text="← Zurück", font=("Arial", 10),
                  bg=self._BG, fg="#888", relief="flat",
                  command=lambda: self._build_probe_page("client")).pack()

    def _make_install_btn(self, text: str, cmd) -> tk.Button:
        return tk.Button(self, text=text,
                         font=("Arial", 13, "bold"),
                         bg=self._ACCENT, fg="white",
                         relief="flat", padx=25, pady=10,
                         cursor="hand2", command=cmd)

    # ------------------------------------------------------------------
    # Installation logic
    # ------------------------------------------------------------------

    def _do_install_server(self) -> None:
        try:
            port = int(self._cfg_port.get())
        except ValueError:
            messagebox.showerror("Fehler", "Ungültiger Port.")
            return

        # Read options from checkboxes
        self._dev_mode = self._opt_dev.get() if hasattr(self, '_opt_dev') else False
        self._update_mode = self._opt_update.get() if hasattr(self, '_opt_update') else False
        do_clean = self._opt_clean.get() if hasattr(self, '_opt_clean') else False

        if do_clean:
            self._run_clean_then_install_and(
                lambda: self._run_server_install(port), role="server")
            return

        # Skip existence check in dev/update mode
        if not self._dev_mode and not self._update_mode:
            info = detect_existing_installation("server")
            if info["exe_exists"] or info["task_exists"] or info["process_running"]:
                self._show_overwrite_dialog("server", info,
                                            on_confirm=lambda: self._run_server_install(port))
                return

        self._run_server_install(port)

    def _run_server_install(self, port: int) -> None:
        self._show_progress("Server wird installiert…\n\n"
                            "Vorprüfung → Backup → Installation → Überprüfung")

        silent = self._silent_var.get()

        update = self._update_mode

        def _install_fn(_backup_info):
            exe = _copy_exe("server", INSTALL_DIR)
            cfg = INSTALL_DIR / "server_config.toml"
            if update and cfg.exists():
                pass  # Preserve existing config
            else:
                write_server_config(cfg, port, silent)
            task_ok = register_task(TASK_SERVER, exe, "server", cfg)
            desk_ok, start_ok = create_shortcuts(exe, "server", cfg)
            return {"task_ok": task_ok, "exe": exe, "desk_ok": desk_ok,
                    "start_ok": start_ok, "task_name": TASK_SERVER}

        def _worker():
            result = safe_install("server", _install_fn)
            if result["ok"]:
                self.after(0, lambda: self._finish(
                    result["task_ok"], "server", result["exe"],
                    result["desk_ok"], result["start_ok"]))
            else:
                self.after(0, lambda: self._error(result["error"]))

        threading.Thread(target=_worker, daemon=True).start()

    def _do_install_client(self) -> None:
        room   = self._room_var.get().strip()
        sip    = self._sip_var.get().strip()
        hotkey = self._hotkey_var.get().strip()
        try:
            port = int(self._cli_port.get())
        except ValueError:
            messagebox.showerror("Fehler", "Ungültiger Port.")
            return

        if not room:
            messagebox.showerror("Fehler", "Bitte Zimmername eingeben.")
            return
        if not sip:
            messagebox.showerror("Fehler", "Bitte Server-IP eingeben.")
            return

        # Read options from checkboxes
        self._dev_mode = self._opt_dev.get() if hasattr(self, '_opt_dev') else False
        self._update_mode = self._opt_update.get() if hasattr(self, '_opt_update') else False
        do_clean = self._opt_clean.get() if hasattr(self, '_opt_clean') else False

        slug = _sanitize_name(room)

        if do_clean:
            self._run_clean_then_install_and(
                lambda: self._run_client_install(room, sip, port, hotkey, slug),
                role="client")
            return

        # Check for existing installation (skip in dev/update mode)
        if not self._dev_mode and not self._update_mode:
            info = detect_existing_installation("client", slug)
            if info["exe_exists"] or info["task_exists"] or info["process_running"]:
                self._show_overwrite_dialog("client", info,
                                            on_confirm=lambda: self._run_client_install(
                                                room, sip, port, hotkey, slug))
                return

        self._run_client_install(room, sip, port, hotkey, slug)

    def _run_client_install(self, room: str, sip: str, port: int,
                            hotkey: str, slug: str) -> None:
        self._show_progress("Client wird installiert…\n\n"
                            "Vorprüfung → Backup → Installation → Überprüfung")

        update = self._update_mode

        def _install_fn(_backup_info):
            exe = _copy_exe("client", INSTALL_DIR)
            cfg = INSTALL_DIR / f"client_config_{slug}.toml"
            if update and cfg.exists():
                pass  # Preserve existing config
            else:
                write_client_config(cfg, room, sip, port, hotkey)
            task_name = f"{TASK_CLIENT}_{slug}"
            task_ok = register_task(task_name, exe, "client", cfg)
            desk_ok, start_ok = create_shortcuts(
                exe, "client", cfg, room_name=room)
            return {"task_ok": task_ok, "exe": exe, "desk_ok": desk_ok,
                    "start_ok": start_ok, "task_name": task_name}

        def _worker():
            result = safe_install("client", _install_fn, room_slug=slug)
            if result["ok"]:
                self.after(0, lambda: self._finish(
                    result["task_ok"], "client", result["exe"],
                    result["desk_ok"], result["start_ok"],
                    task_name=result["task_name"]))
            else:
                self.after(0, lambda: self._error(result["error"]))

        threading.Thread(target=_worker, daemon=True).start()

    def _show_overwrite_dialog(self, role: str, info: dict,
                               on_confirm) -> None:
        """Show a warning page when a previous installation is detected."""
        self._clear()
        role_de = "Server" if role == "server" else "Client"

        tk.Label(self, text="⚠  Vorhandene Installation erkannt",
                 font=("Arial", 16, "bold"), bg=self._BG,
                 fg=self._AMBER).pack(pady=(40, 20))

        details = []
        if info["exe_exists"]:
            details.append(f"✔  {role_de}-Programm ist bereits installiert")
        if info["task_exists"]:
            details.append(f"✔  Autostart-Aufgabe vorhanden: {info['task_name']}")
        if info["process_running"]:
            details.append(f"✔  {role_de}-Prozess läuft gerade")

        for line in details:
            tk.Label(self, text=line, font=("Arial", 11),
                     bg=self._BG, fg=self._FG).pack(anchor="w", padx=60)

        tk.Label(self, text=("\nDie bestehende Installation wird gestoppt\n"
                             "und durch die neue ersetzt.\n\n"
                             "Möchten Sie fortfahren?"),
                 font=("Arial", 11), bg=self._BG, fg=self._FG,
                 justify="center").pack(pady=20)

        btn_frm = tk.Frame(self, bg=self._BG)
        btn_frm.pack(pady=10)

        tk.Button(btn_frm, text="Ja, überschreiben",
                  font=("Arial", 12, "bold"),
                  bg=self._ACCENT, fg="white", relief="flat",
                  padx=20, pady=8,
                  command=on_confirm).grid(row=0, column=0, padx=10)

        tk.Button(btn_frm, text="Abbrechen",
                  font=("Arial", 12),
                  bg=self._BLUE, fg=self._FG, relief="flat",
                  padx=20, pady=8,
                  command=self._build_role_page).grid(row=0, column=1, padx=10)

    def _show_progress(self, msg: str) -> None:
        self._clear()
        tk.Label(self, text=msg, font=("Arial", 14),
                 bg=self._BG, fg=self._FG).pack(pady=60)
        bar = ttk.Progressbar(self, mode="indeterminate", length=300)
        bar.pack()
        bar.start(10)

    def _finish(self, task_ok: bool, role: str, exe: Path,
                desk_ok: bool = False, start_ok: bool = False,
                task_name: Optional[str] = None) -> None:
        self._clear()

        all_ok = task_ok and desk_ok and start_ok
        icon = "✔" if all_ok else "⚠"
        color = self._GREEN if all_ok else self._AMBER
        role_de = "Server" if role == "server" else "Client"

        tk.Label(self, text=f"{icon}  Installation abgeschlossen",
                 font=("Arial", 18, "bold"), bg=self._BG, fg=color).pack(pady=(40, 15))

        _ok = lambda v: "✔" if v else "✘"
        info = [
            f"Rolle:         {role_de}",
            f"Programm:      {exe}",
            f"Autostart:     {_ok(task_ok)}  {'Registriert' if task_ok else 'Fehler — bitte manuell einrichten'}",
            f"Desktop:       {_ok(desk_ok)}  {'Verknüpfung erstellt' if desk_ok else 'Fehler'}",
            f"Startmenü:     {_ok(start_ok)}  {'Verknüpfung erstellt' if start_ok else 'Fehler'}",
        ]
        for line in info:
            tk.Label(self, text=line, font=("Arial", 10),
                     bg=self._BG, fg=self._FG).pack(anchor="w", padx=60)

        tk.Label(self, text="\nDer Dienst startet automatisch beim nächsten Systemstart.\nJetzt starten?",
                 font=("Arial", 11), bg=self._BG, fg=self._FG,
                 justify="center").pack(pady=15)

        task = task_name or (TASK_SERVER if role == "server" else TASK_CLIENT)

        btn_frm = tk.Frame(self, bg=self._BG)
        btn_frm.pack(pady=10)

        tk.Button(btn_frm, text="Jetzt starten",
                  font=("Arial", 12, "bold"),
                  bg=self._GREEN, fg="white", relief="flat",
                  padx=20, pady=8,
                  command=lambda: [start_task(task), self.destroy()]).grid(
            row=0, column=0, padx=10)

        tk.Button(btn_frm, text="Später starten",
                  font=("Arial", 12),
                  bg=self._BLUE, fg=self._FG, relief="flat",
                  padx=20, pady=8,
                  command=self.destroy).grid(row=0, column=1, padx=10)

    def _error(self, msg: str) -> None:
        self._clear()
        tk.Label(self, text="✘  Installationsfehler",
                 font=("Arial", 18, "bold"), bg=self._BG, fg=self._ACCENT).pack(pady=40)
        tk.Label(self, text=msg, font=("Arial", 10),
                 bg=self._BG, fg=self._FG, wraplength=500,
                 justify="left").pack(padx=30)
        tk.Label(self, text="\nBitte als Administrator ausführen.",
                 font=("Arial", 11), bg=self._BG, fg=self._AMBER).pack(pady=10)
        tk.Button(self, text="Schließen", font=("Arial", 12),
                  bg=self._BLUE, fg=self._FG, relief="flat",
                  padx=20, pady=8, command=self.destroy).pack(pady=20)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _clear(self) -> None:
        for w in self.winfo_children():
            w.destroy()


# ---------------------------------------------------------------------------
# GUI — uninstaller window
# ---------------------------------------------------------------------------

class UninstallerApp(tk.Tk):
    """Tkinter-based uninstaller GUI."""

    _BG     = "#1a1a2e"
    _FG     = "#e0e0e0"
    _BLUE   = "#0f3460"
    _ACCENT = "#e94560"
    _GREEN  = "#00b894"

    def __init__(self) -> None:
        super().__init__()
        self.title(f"Alarmsystem — Deinstallation  v{__version__}")
        self.resizable(True, True)
        self.configure(bg=self._BG)
        self._center(500, 560)
        self.minsize(400, 400)
        self._build_main_page()

    def _center(self, w: int, h: int) -> None:
        x = (self.winfo_screenwidth() - w) // 2
        y = (self.winfo_screenheight() - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _build_main_page(self) -> None:
        self._clear()

        tk.Label(self, text="Alarmsystem deinstallieren",
                 font=("Arial", 18, "bold"), bg=self._BG,
                 fg=self._ACCENT).pack(pady=(30, 5))
        tk.Label(self, text="Wählen Sie aus, was deinstalliert werden soll:",
                 font=("Arial", 10), bg=self._BG,
                 fg=self._FG).pack(pady=(0, 15))

        # Server checkbox
        self._server_var = tk.BooleanVar(value=False)
        server_installed = is_server_installed()

        srv_frm = tk.Frame(self, bg=self._BG)
        srv_frm.pack(fill="x", padx=40, pady=4)
        cb = tk.Checkbutton(
            srv_frm, text="Server deinstallieren",
            variable=self._server_var,
            font=("Arial", 11), bg=self._BG, fg=self._FG,
            selectcolor="#333", activebackground=self._BG,
            state="normal" if server_installed else "disabled",
        )
        cb.pack(side="left")
        if not server_installed:
            tk.Label(srv_frm, text="(nicht installiert)",
                     font=("Arial", 9), bg=self._BG, fg="#888").pack(side="left", padx=6)

        # Client checkboxes (scrollable)
        self._client_vars: list[tuple[tk.BooleanVar, str, str]] = []  # (var, slug, room_name)
        clients = find_installed_clients()

        # Also find orphan shortcuts on desktop
        self._orphan_shortcuts: list[Path] = []
        desktop = Path(os.environ.get("USERPROFILE", "C:\\Users\\Public")) / "Desktop"
        for lnk in desktop.glob("Alarm Client*"):
            self._orphan_shortcuts.append(lnk)
        for lnk in desktop.glob("Alarm Server*"):
            self._orphan_shortcuts.append(lnk)
        for lnk in desktop.glob("Alarmsystem deinstallieren*"):
            self._orphan_shortcuts.append(lnk)

        has_items = clients or self._orphan_shortcuts

        if has_items:
            scroll_container = tk.Frame(self, bg=self._BG)
            scroll_container.pack(fill="both", expand=True, padx=40, pady=(5, 5))

            canvas = tk.Canvas(scroll_container, bg=self._BG, highlightthickness=0)
            scrollbar = ttk.Scrollbar(scroll_container, orient=tk.VERTICAL,
                                      command=canvas.yview)
            inner_frm = tk.Frame(canvas, bg=self._BG)

            inner_frm.bind(
                "<Configure>",
                lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
            )
            canvas.create_window((0, 0), window=inner_frm, anchor="nw")
            canvas.configure(yscrollcommand=scrollbar.set)

            canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

            canvas.bind_all("<MouseWheel>",
                            lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))

            if clients:
                tk.Label(inner_frm, text="Clients:", font=("Arial", 11, "bold"),
                         bg=self._BG, fg=self._FG).pack(anchor="w", pady=(5, 2))

                for client in clients:
                    var = tk.BooleanVar(value=False)
                    tk.Checkbutton(
                        inner_frm, text=client["room"],
                        variable=var,
                        font=("Arial", 10), bg=self._BG, fg=self._FG,
                        selectcolor="#333", activebackground=self._BG,
                    ).pack(anchor="w", pady=1, padx=15)
                    self._client_vars.append((var, client["slug"], client["room"]))

            if self._orphan_shortcuts:
                tk.Label(inner_frm, text="Verknüpfungen auf dem Desktop:",
                         font=("Arial", 11, "bold"),
                         bg=self._BG, fg=self._FG).pack(anchor="w", pady=(10, 2))

                self._shortcut_var = tk.BooleanVar(value=False)
                for lnk in self._orphan_shortcuts:
                    tk.Label(inner_frm, text=f"  • {lnk.stem}",
                             font=("Arial", 9), bg=self._BG,
                             fg="#888").pack(anchor="w", padx=15)

                tk.Checkbutton(
                    inner_frm, text="Alle Verknüpfungen entfernen",
                    variable=self._shortcut_var,
                    font=("Arial", 10), bg=self._BG, fg=self._FG,
                    selectcolor="#333", activebackground=self._BG,
                ).pack(anchor="w", pady=(4, 2), padx=15)
        else:
            self._shortcut_var = None
            tk.Label(self, text="Nichts zu deinstallieren.",
                     font=("Arial", 10), bg=self._BG,
                     fg="#888").pack(pady=(10, 0))

        # Separator
        tk.Frame(self, bg="#333333", height=1).pack(fill="x", padx=20, pady=(5, 0))

        # Buttons (always visible at bottom)
        btn_frm = tk.Frame(self, bg=self._BG)
        btn_frm.pack(pady=15)

        tk.Button(btn_frm, text="Deinstallieren",
                  font=("Arial", 13, "bold"),
                  bg=self._ACCENT, fg="white", relief="flat",
                  padx=20, pady=8, cursor="hand2",
                  command=self._do_uninstall).grid(row=0, column=0, padx=10)

        tk.Button(btn_frm, text="Abbrechen",
                  font=("Arial", 12),
                  bg=self._BLUE, fg=self._FG, relief="flat",
                  padx=20, pady=8,
                  command=self.destroy).grid(row=0, column=1, padx=10)

    def _do_uninstall(self) -> None:
        # Collect selections
        do_server = self._server_var.get()
        do_clients = [(slug, room) for var, slug, room in self._client_vars if var.get()]
        do_shortcuts = getattr(self, "_shortcut_var", None) and self._shortcut_var.get()

        if not do_server and not do_clients and not do_shortcuts:
            messagebox.showwarning("Hinweis",
                                   "Bitte wählen Sie mindestens eine Komponente aus.")
            return

        # Confirm
        parts = []
        if do_server:
            parts.append("Server")
        for slug, room in do_clients:
            parts.append(f"Client ({room})")
        if do_shortcuts:
            parts.append(f"{len(self._orphan_shortcuts)} Verknüpfung(en)")
        msg = "Folgende Komponenten werden deinstalliert:\n\n"
        msg += "\n".join(f"  • {p}" for p in parts)
        msg += "\n\nFortfahren?"

        if not messagebox.askyesno("Bestätigung", msg):
            return

        self._show_progress()

        def _worker():
            all_actions = []
            if do_server:
                all_actions += uninstall("server")
            for slug, room in do_clients:
                all_actions += uninstall("client", slug, room_name=room)
            if do_shortcuts:
                for lnk in self._orphan_shortcuts:
                    try:
                        lnk.unlink()
                        all_actions.append(f"Verknüpfung {lnk.stem} entfernt")
                    except Exception:
                        pass
            self.after(0, lambda: self._show_result(all_actions))

        threading.Thread(target=_worker, daemon=True).start()

    def _show_progress(self) -> None:
        self._clear()
        tk.Label(self, text="Deinstallation läuft…",
                 font=("Arial", 14), bg=self._BG,
                 fg=self._FG).pack(pady=60)
        bar = ttk.Progressbar(self, mode="indeterminate", length=300)
        bar.pack()
        bar.start(10)

    def _show_result(self, actions: list[str]) -> None:
        self._clear()

        if actions:
            tk.Label(self, text="✔  Deinstallation abgeschlossen",
                     font=("Arial", 16, "bold"), bg=self._BG,
                     fg=self._GREEN).pack(pady=(20, 10))

            # Scrollable action list
            scroll_frm = tk.Frame(self, bg=self._BG)
            scroll_frm.pack(fill="both", expand=True, padx=20, pady=(0, 5))

            canvas = tk.Canvas(scroll_frm, bg=self._BG, highlightthickness=0)
            scrollbar = ttk.Scrollbar(scroll_frm, orient=tk.VERTICAL,
                                      command=canvas.yview)
            inner = tk.Frame(canvas, bg=self._BG)
            inner.bind("<Configure>",
                       lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
            canvas.create_window((0, 0), window=inner, anchor="nw")
            canvas.configure(yscrollcommand=scrollbar.set)
            canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
            canvas.bind_all("<MouseWheel>",
                            lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))

            for action in actions:
                tk.Label(inner, text=f"  • {action}",
                         font=("Arial", 10), bg=self._BG,
                         fg=self._FG).pack(anchor="w", padx=10)
        else:
            tk.Label(self, text="Keine Änderungen vorgenommen.",
                     font=("Arial", 14), bg=self._BG,
                     fg="#888").pack(pady=60)

        # Button always visible at bottom
        tk.Frame(self, bg="#333333", height=1).pack(fill="x", padx=20)
        tk.Button(self, text="Schließen", font=("Arial", 12),
                  bg=self._BLUE, fg=self._FG, relief="flat",
                  padx=20, pady=8,
                  command=self.destroy).pack(pady=15)

    def _clear(self) -> None:
        for w in self.winfo_children():
            w.destroy()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _detect_role_from_exe() -> Optional[str]:
    """If we were copied/renamed to alarm_server.exe or alarm_client.exe,
    return the role so we can dispatch to the real module."""
    exe_name = Path(sys.executable).stem.lower()
    if exe_name == "alarm_server":
        return "server"
    elif exe_name == "alarm_client":
        return "client"
    return None


def main() -> None:
    # When the combined installer is copied as alarm_server.exe or
    # alarm_client.exe, dispatch directly to the correct module's main().
    role = _detect_role_from_exe()
    if role == "server":
        from server.server import main as server_main
        server_main()
        return
    elif role == "client":
        from client.client import main as client_main
        client_main()
        return

    # Parse installer flags
    dev_mode = "--dev" in sys.argv

    # Otherwise we're running as the installer — require admin rights.
    if sys.platform == "win32":
        import ctypes
        if not ctypes.windll.shell32.IsUserAnAdmin():
            # Try UAC elevation first
            try:
                script = sys.argv[0]
                extra = " ".join(f'"{a}"' for a in sys.argv[1:])
                args = f'"{script}" {extra}'.strip()
                ret = ctypes.windll.shell32.ShellExecuteW(
                    None, "runas", sys.executable, args, None, 1
                )
                if ret > 32:  # Success — elevated process launched
                    sys.exit(0)
            except Exception:
                pass
            # Elevation failed or was denied — show error and exit
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "Alarmsystem — Installer",
                "Dieses Programm benötigt Administratorrechte.\n\n"
                "Bitte starten Sie es mit 'Als Administrator ausführen'."
            )
            root.destroy()
            sys.exit(1)

    if "--uninstall" in sys.argv:
        app = UninstallerApp()
    else:
        app = InstallerApp(dev_mode=dev_mode)
    app.mainloop()


if __name__ == "__main__":
    main()
