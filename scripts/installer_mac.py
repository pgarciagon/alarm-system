"""
installer_mac.py — Interactive macOS installer for the Alarm System.

Presents a GUI that:
  1. Asks: Server or Client?
  2. Probes the network to detect a running server or existing local server.
  3. Collects config (room name, server IP, port, hotkey).
  4. Copies the app to /Applications/AlarmSystem/.
  5. Writes a TOML config file.
  6. Registers a launchd agent for auto-start at login.
  7. Optionally starts the service immediately.

Build into a .app + DMG with:
    pyinstaller scripts/alarm_installer_mac.spec
    bash scripts/create_dmg.sh
"""

from __future__ import annotations

import os
import plistlib
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
# Bootstrap: when frozen add bundle root to sys.path
# ---------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(sys._MEIPASS)))  # type: ignore[attr-defined]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
APP_NAME      = "AlarmSystem"
INSTALL_DIR   = Path("/Applications") / APP_NAME
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
LABEL_SERVER  = "com.alarm-system.server"
LABEL_CLIENT  = "com.alarm-system.client"
DEFAULT_PORT  = 9999
PROBE_TIMEOUT = 2.0

REQUIREMENTS = [
    "websockets>=12.0",
    "keyboard>=0.13.5",
    "pygame>=2.5.0",
    "pystray>=0.19.5",
    "Pillow>=10.0.0",
]

# Source packages to copy into the install dir (relative to repo root)
SOURCE_PACKAGES = ["server", "client", "common", "config", "assets"]


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def probe_server(host: str, port: int, timeout: float = PROBE_TIMEOUT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def get_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


# ---------------------------------------------------------------------------
# launchd helpers
# ---------------------------------------------------------------------------

def _plist_path(role: str) -> Path:
    label = LABEL_SERVER if role == "server" else LABEL_CLIENT
    return LAUNCH_AGENTS / f"{label}.plist"


def register_launchd(exe: Path, role: str, config_path: Path) -> bool:
    """Write and load a launchd plist. Returns True on success."""
    label = LABEL_SERVER if role == "server" else LABEL_CLIENT
    plist_path = _plist_path(role)
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)

    program_args = [str(exe), "--config", str(config_path), "--gui"]

    plist_data = {
        "Label": label,
        "ProgramArguments": program_args,
        "RunAtLoad": True,
        "KeepAlive": True,
        "WorkingDirectory": str(exe.parent),
        "StandardOutPath":  str(Path.home() / f"Library/Logs/{label}.log"),
        "StandardErrorPath": str(Path.home() / f"Library/Logs/{label}.err"),
    }

    with open(plist_path, "wb") as fh:
        plistlib.dump(plist_data, fh)

    # Unload existing agent silently first
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)

    r = subprocess.run(
        ["launchctl", "load", str(plist_path)],
        capture_output=True, text=True,
    )
    return r.returncode == 0


def unregister_launchd(role: str) -> None:
    label = LABEL_SERVER if role == "server" else LABEL_CLIENT
    plist_path = _plist_path(role)
    subprocess.run(["launchctl", "stop",   label],           capture_output=True)
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    if plist_path.exists():
        plist_path.unlink()


def start_launchd(role: str) -> None:
    label = LABEL_SERVER if role == "server" else LABEL_CLIENT
    subprocess.run(["launchctl", "start", label], capture_output=True)


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
# Asset / exe helpers
# ---------------------------------------------------------------------------

def _bundle_path() -> Path:
    """Root of bundled data: _MEIPASS when frozen, repo root when dev."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).parent.parent


def _bundle_file(rel: str) -> Optional[Path]:
    p = _bundle_path() / rel
    return p if p.exists() else None


def _find_python3() -> str:
    """Return the best available python3 executable path."""
    candidates = [
        "/opt/homebrew/opt/python@3.12/bin/python3.12",
        "/opt/homebrew/opt/python@3.13/bin/python3.13",
        "/opt/homebrew/opt/python@3.11/bin/python3.11",
        "/opt/homebrew/bin/python3.12",
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3.12",
        "/usr/local/bin/python3",
        "/usr/bin/python3",
        sys.executable,
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return sys.executable


def _pip_install(python: str, log_cb) -> None:
    """pip-install all required packages into the system Python."""
    log_cb("Abhängigkeiten werden installiert…")
    cmd = [python, "-m", "pip", "install", "--break-system-packages", "--quiet"]
    cmd += REQUIREMENTS
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        # Try without --break-system-packages (older pip / venv)
        cmd2 = [python, "-m", "pip", "install", "--quiet"] + REQUIREMENTS
        r2 = subprocess.run(cmd2, capture_output=True, text=True)
        if r2.returncode != 0:
            raise RuntimeError(
                f"pip install fehlgeschlagen:\n{r.stderr or r2.stderr}"
            )


def _copy_sources(dest: Path) -> None:
    """Copy Python source packages (server/, client/, common/, etc.) to dest."""
    bundle = _bundle_path()
    for pkg in SOURCE_PACKAGES:
        src = bundle / pkg
        dst = dest / pkg
        if src.exists():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)


def _write_launcher(dest: Path, role: str, python: str, config_path: Path) -> Path:
    """Write a shell launcher script and return its path."""
    module = "server.server" if role == "server" else "client.client"
    exe_name = "alarm_server" if role == "server" else "alarm_client"
    launcher = dest / exe_name
    launcher.write_text(
        "#!/bin/bash\n"
        f'cd "{dest}"\n'
        f'"{python}" -m {module} --config "{config_path}" "$@"\n',
        encoding="utf-8",
    )
    os.chmod(launcher, 0o755)
    return launcher


def _install_app(role: str, dest: Path, log_cb, python: Optional[str] = None) -> Path:
    """
    Full install:
      1. Create dest dir
      2. pip-install dependencies into system Python
      3. Copy source packages into dest
      4. Copy assets into dest/assets/
      5. Write shell launcher
    Returns path to the launcher script.
    """
    dest.mkdir(parents=True, exist_ok=True)

    if python is None:
        python = _find_python3()

    # 1. pip dependencies
    _pip_install(python, log_cb)

    # 2. Copy source packages
    log_cb("Quelldateien werden kopiert…")
    _copy_sources(dest)

    # 3. Copy assets
    assets_dest = dest / "assets"
    assets_dest.mkdir(exist_ok=True)
    for name in ["alarm.wav", "alarm.ico", "alarm_server.ico", "alarm_client.ico"]:
        src = _bundle_file(f"assets/{name}")
        if src:
            try:
                shutil.copy2(src, assets_dest / name)
            except Exception:
                pass

    # 4. Write launcher
    config_path = dest / (
        "server_config.toml" if role == "server" else "client_config.toml"
    )
    launcher = _write_launcher(dest, role, python, config_path)
    log_cb(f"Launcher geschrieben: {launcher}")
    return launcher


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class InstallerApp(tk.Tk):
    _BG     = "#1a1a2e"
    _FG     = "#e0e0e0"
    _BLUE   = "#0f3460"
    _ACCENT = "#e94560"
    _GREEN  = "#00b894"
    _AMBER  = "#fdcb6e"

    def __init__(self) -> None:
        super().__init__()
        self.title("Alarmsystem — macOS Installation")
        self.resizable(False, False)
        self.configure(bg=self._BG)
        self._center(620, 520)
        self._role: Optional[str] = None
        self._build_role_page()

    def _center(self, w: int, h: int) -> None:
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

    # ------------------------------------------------------------------
    # Page 1 — role selection
    # ------------------------------------------------------------------

    def _build_role_page(self) -> None:
        self._clear()
        tk.Label(self, text="Alarmsystem Installation",
                 font=("Arial", 20, "bold"), bg=self._BG, fg=self._ACCENT).pack(pady=(30, 5))
        tk.Label(self, text="Wählen Sie die Rolle dieses Macs:",
                 font=("Arial", 13), bg=self._BG, fg=self._FG).pack(pady=(0, 25))

        frm = tk.Frame(self, bg=self._BG)
        frm.pack(pady=10)
        self._role_btn(frm, "🖥  SERVER",
                       "Zentrale Schaltstelle.\nNur ein Server pro Netzwerk.",
                       "server").grid(row=0, column=0, padx=20)
        self._role_btn(frm, "🔔  CLIENT",
                       "Patientenzimmer-Mac.\nMehrere Clients pro Netzwerk.",
                       "client").grid(row=0, column=1, padx=20)

        tk.Label(self, text=f"Lokale IP: {get_local_ip()}",
                 font=("Arial", 10), bg=self._BG, fg="#888").pack(pady=(30, 0))
        tk.Label(self, text=f"Installations-Pfad: {INSTALL_DIR}",
                 font=("Arial", 9), bg=self._BG, fg="#666").pack()

    def _role_btn(self, parent, title, desc, role) -> tk.Frame:
        frm = tk.Frame(parent, bg=self._BLUE, cursor="hand2")
        for widget in [frm]:
            widget.bind("<Button-1>", lambda _e, r=role: self._on_role(r))
        tk.Label(frm, text=title, font=("Arial", 16, "bold"),
                 bg=self._BLUE, fg=self._FG, padx=20, pady=15).pack()
        lbl_desc = tk.Label(frm, text=desc, font=("Arial", 10),
                 bg=self._BLUE, fg="#aaa", padx=20, pady=5,
                 justify="center", wraplength=180)
        lbl_desc.pack()
        tk.Label(frm, text="▶  Auswählen", font=("Arial", 10, "bold"),
                 bg=self._BLUE, fg=self._ACCENT, pady=10).pack()
        for child in frm.winfo_children():
            child.bind("<Button-1>", lambda _e, r=role: self._on_role(r))
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
            tk.Entry(pfrm, textvariable=self._server_ip_var, width=18,
                     font=("Arial", 11)).grid(row=1, column=1, sticky="w")

        self._next_btn = tk.Button(self, text="Weiter →",
                                   font=("Arial", 12, "bold"),
                                   bg=self._ACCENT, fg="white", relief="flat",
                                   padx=20, pady=8, state="disabled",
                                   command=self._on_probe_done)
        self._next_btn.pack(pady=20)
        tk.Button(self, text="← Zurück", font=("Arial", 10),
                  bg=self._BG, fg="#888", relief="flat",
                  command=self._build_role_page).pack()

        threading.Thread(target=self._run_probe, args=(role,), daemon=True).start()

    def _run_probe(self, role: str) -> None:
        try:
            port = int(self._port_var.get())
        except ValueError:
            port = DEFAULT_PORT

        if role == "server":
            already = probe_server("127.0.0.1", port)
            if already:
                msg = (f"⚠  Ein Server läuft bereits auf diesem Mac (Port {port}).\n"
                       "Die Installation überschreibt die Konfiguration.")
                color = self._AMBER
            else:
                msg = "✔  Kein Server gefunden — dieser Mac wird zum Server."
                color = self._GREEN
        else:
            local_ip = get_local_ip()
            prefix = ".".join(local_ip.split(".")[:3])
            found_ip: Optional[str] = None
            for ip in ["127.0.0.1", local_ip, f"{prefix}.1",
                       f"{prefix}.47", f"{prefix}.100", f"{prefix}.200"]:
                if probe_server(ip, port):
                    found_ip = ip
                    break
            if found_ip:
                self.after(0, lambda ip=found_ip: self._server_ip_var.set(ip))
                msg = f"✔  Server gefunden: {found_ip}:{port}"
                color = self._GREEN
            else:
                msg = "⚠  Kein Server gefunden.\nServer-IP bitte manuell eingeben."
                color = self._AMBER

        self.after(0, lambda: self._probe_done(msg, color))

    def _probe_done(self, msg: str, color: str) -> None:
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
    # Page 3a — server config
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
        tk.Checkbutton(frm,
                       text="Stiller Alarm (auslösendes Zimmer wird nicht benachrichtigt)",
                       variable=self._silent_var, font=("Arial", 10),
                       bg=self._BG, fg=self._FG, selectcolor=self._BLUE,
                       activebackground=self._BG, activeforeground=self._FG
                       ).grid(row=1, column=0, columnspan=2, sticky="w", padx=10, pady=8)

        tk.Label(frm, text=f"Installations-Pfad: {INSTALL_DIR}",
                 font=("Arial", 9), bg=self._BG, fg="#888",
                 justify="left").grid(row=2, column=0, columnspan=2,
                                      sticky="w", padx=10, pady=4)

        tk.Button(self, text="Server installieren",
                  font=("Arial", 13, "bold"), bg=self._ACCENT, fg="white",
                  relief="flat", padx=25, pady=10, cursor="hand2",
                  command=self._do_install_server).pack(pady=25)
        tk.Button(self, text="← Zurück", font=("Arial", 10),
                  bg=self._BG, fg="#888", relief="flat",
                  command=lambda: self._build_probe_page("server")).pack()

    # ------------------------------------------------------------------
    # Page 3b — client config
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
        self._sip_var = tk.StringVar(
            value=getattr(self, "_server_ip_var", tk.StringVar()).get())
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

        tk.Label(frm, text=f"Installations-Pfad: {INSTALL_DIR}",
                 font=("Arial", 9), bg=self._BG, fg="#888",
                 justify="left").grid(row=4, column=0, columnspan=3,
                                      sticky="w", padx=10, pady=4)

        tk.Button(self, text="Client installieren",
                  font=("Arial", 13, "bold"), bg=self._ACCENT, fg="white",
                  relief="flat", padx=25, pady=10, cursor="hand2",
                  command=self._do_install_client).pack(pady=25)
        tk.Button(self, text="← Zurück", font=("Arial", 10),
                  bg=self._BG, fg="#888", relief="flat",
                  command=lambda: self._build_probe_page("client")).pack()

    # ------------------------------------------------------------------
    # Installation
    # ------------------------------------------------------------------

    def _do_install_server(self) -> None:
        try:
            port = int(self._cfg_port.get())
        except ValueError:
            messagebox.showerror("Fehler", "Ungültiger Port.")
            return
        self._show_progress("Server wird installiert…")
        def _log(msg: str):
            self.after(0, lambda m=msg: self._update_progress(m))
        def _worker():
            try:
                exe = _install_app("server", INSTALL_DIR, _log)
                cfg = INSTALL_DIR / "server_config.toml"
                write_server_config(cfg, port, self._silent_var.get())
                ok = register_launchd(exe, "server", cfg)
                self.after(0, lambda: self._finish(ok, "server", exe))
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
        def _log(msg: str):
            self.after(0, lambda m=msg: self._update_progress(m))
        def _worker():
            try:
                exe = _install_app("client", INSTALL_DIR, _log)
                cfg = INSTALL_DIR / "client_config.toml"
                write_client_config(cfg, room, sip, port, hotkey)
                ok = register_launchd(exe, "client", cfg)
                self.after(0, lambda: self._finish(ok, "client", exe))
            except Exception as exc:
                self.after(0, lambda: self._error(str(exc)))
        threading.Thread(target=_worker, daemon=True).start()

    def _show_progress(self, msg: str) -> None:
        self._clear()
        tk.Label(self, text=msg, font=("Arial", 14),
                 bg=self._BG, fg=self._FG).pack(pady=(60, 10))
        bar = ttk.Progressbar(self, mode="indeterminate", length=300)
        bar.pack()
        bar.start(10)
        self._progress_detail = tk.Label(
            self, text="", font=("Arial", 10),
            bg=self._BG, fg=self._AMBER, wraplength=500)
        self._progress_detail.pack(pady=(12, 0))

    def _update_progress(self, msg: str) -> None:
        if hasattr(self, "_progress_detail"):
            self._progress_detail.config(text=msg)

    def _finish(self, launchd_ok: bool, role: str, exe: Path) -> None:
        self._clear()
        icon  = "✔" if launchd_ok else "⚠"
        color = self._GREEN if launchd_ok else self._AMBER
        role_de = "Server" if role == "server" else "Client"

        tk.Label(self, text=f"{icon}  Installation abgeschlossen",
                 font=("Arial", 18, "bold"), bg=self._BG, fg=color).pack(pady=(40, 15))

        info = [
            f"Rolle:      {role_de}",
            f"Programm:   {exe}",
            f"Autostart:  {'✔  launchd Agent registriert' if launchd_ok else '✘  Fehler — bitte manuell einrichten'}",
        ]
        for line in info:
            tk.Label(self, text=line, font=("Arial", 10),
                     bg=self._BG, fg=self._FG).pack(anchor="w", padx=60)

        if role == "client":
            tk.Label(self,
                     text="\n⚠  Barrierefreiheit erforderlich:\nSystemeinstellungen → Datenschutz → Bedienungshilfen\n→ Terminal (oder diese App) hinzufügen.",
                     font=("Arial", 10), bg=self._BG, fg=self._AMBER,
                     justify="left").pack(anchor="w", padx=60, pady=(8, 0))

        tk.Label(self,
                 text="\nDer Dienst startet automatisch beim nächsten Login.\nJetzt starten?",
                 font=("Arial", 11), bg=self._BG, fg=self._FG,
                 justify="center").pack(pady=15)

        btn_frm = tk.Frame(self, bg=self._BG)
        btn_frm.pack(pady=10)

        tk.Button(btn_frm, text="Jetzt starten",
                  font=("Arial", 12, "bold"), bg=self._GREEN, fg="white",
                  relief="flat", padx=20, pady=8,
                  command=lambda: [start_launchd(role), self.destroy()]
                  ).grid(row=0, column=0, padx=10)

        tk.Button(btn_frm, text="Später starten",
                  font=("Arial", 12), bg=self._BLUE, fg=self._FG,
                  relief="flat", padx=20, pady=8,
                  command=self.destroy).grid(row=0, column=1, padx=10)

    def _error(self, msg: str) -> None:
        self._clear()
        tk.Label(self, text="✘  Installationsfehler",
                 font=("Arial", 18, "bold"), bg=self._BG, fg=self._ACCENT).pack(pady=40)
        tk.Label(self, text=msg, font=("Arial", 10),
                 bg=self._BG, fg=self._FG, wraplength=500,
                 justify="left").pack(padx=30)
        tk.Button(self, text="Schließen", font=("Arial", 12),
                  bg=self._BLUE, fg=self._FG, relief="flat",
                  padx=20, pady=8, command=self.destroy).pack(pady=20)

    def _clear(self) -> None:
        for w in self.winfo_children():
            w.destroy()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _detect_role_from_exe() -> Optional[str]:
    name = Path(sys.executable).stem.lower()
    if name == "alarm_server":
        return "server"
    if name == "alarm_client":
        return "client"
    return None


def main() -> None:
    # If renamed to alarm_server / alarm_client, dispatch directly
    role = _detect_role_from_exe()
    if role == "server":
        from server.server import main as _m; _m(); return
    if role == "client":
        from client.client import main as _m; _m(); return

    app = InstallerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
