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

    def __init__(self) -> None:
        super().__init__()
        self.title("Alarmsystem — Installation")
        self.resizable(False, False)
        self.configure(bg=self._BG)
        self._center(620, 520)

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

        tk.Label(self, text="Alarmsystem Installation",
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

    def _on_role(self, role: str) -> None:
        self._role = role
        self._build_probe_page(role)

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

        self._make_install_btn("Server installieren", self._do_install_server).pack(pady=25)

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

        self._make_install_btn("Client installieren", self._do_install_client).pack(pady=25)

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

        self._show_progress("Server wird installiert…")

        def _worker():
            try:
                exe = _copy_exe("server", INSTALL_DIR)
                cfg = INSTALL_DIR / "server_config.toml"
                write_server_config(cfg, port, self._silent_var.get())
                task_ok = register_task(TASK_SERVER, exe, "server", cfg)
                desk_ok, start_ok = create_shortcuts(exe, "server", cfg)
                self.after(0, lambda: self._finish(task_ok, "server", exe,
                                                    desk_ok, start_ok))
            except Exception as exc:
                self.after(0, lambda: self._error(str(exc)))

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

        self._show_progress("Client wird installiert…")

        def _worker():
            try:
                slug = _sanitize_name(room)
                exe = _copy_exe("client", INSTALL_DIR)
                cfg = INSTALL_DIR / f"client_config_{slug}.toml"
                write_client_config(cfg, room, sip, port, hotkey)
                task_name = f"{TASK_CLIENT}_{slug}"
                task_ok = register_task(task_name, exe, "client", cfg)
                desk_ok, start_ok = create_shortcuts(
                    exe, "client", cfg, room_name=room)
                self.after(0, lambda: self._finish(task_ok, "client", exe,
                                                    desk_ok, start_ok,
                                                    task_name=task_name))
            except Exception as exc:
                self.after(0, lambda: self._error(str(exc)))

        threading.Thread(target=_worker, daemon=True).start()

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

    # Otherwise we're running as the installer — request UAC elevation.
    if sys.platform == "win32":
        try:
            import ctypes
            if not ctypes.windll.shell32.IsUserAnAdmin():
                # Re-launch with elevation
                ctypes.windll.shell32.ShellExecuteW(
                    None, "runas", sys.executable,
                    " ".join(f'"{a}"' for a in sys.argv), None, 1
                )
                sys.exit(0)
        except Exception:
            pass  # If elevation fails, continue anyway

    app = InstallerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
