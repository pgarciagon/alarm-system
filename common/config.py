"""
config.py — TOML configuration loading with sensible defaults.

Uses tomllib (stdlib ≥ 3.11) or the backport tomli on older Pythons.
"""

from __future__ import annotations

import sys
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError as exc:
        raise ImportError(
            "Python < 3.11 requires the 'tomli' package. "
            "Run: pip install tomli"
        ) from exc


# ---------------------------------------------------------------------------
# Config dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 9999
    heartbeat_timeout_sec: int = 15

    # When True (default), the alarm is NOT sent back to the room that
    # triggered it — silent alarm mode.  Set to False to also alert the
    # triggering room's own screen.
    silent_alarm: bool = True

    # path to write log file; empty string → log to stdout only
    log_file: str = ""


def _default_hotkey() -> str:
    return "cmd+n" if sys.platform == "darwin" else "alt+n"


@dataclass
class ClientConfig:
    room_name: str = "Room 1"
    server_ip: str = "127.0.0.1"
    server_port: int = 9999
    hotkey: str = field(default_factory=_default_hotkey)

    # path to custom alarm sound; empty string → use bundled asset
    alarm_sound: str = ""

    # path to write log file; empty string → log to stdout only
    log_file: str = ""

    # mute state
    muted: bool = False

    # internal: remembers where this config was loaded from (not serialized)
    _config_path: Optional[str] = field(default=None, repr=False)


# ---------------------------------------------------------------------------
# Default config file content (written when no file exists)
# ---------------------------------------------------------------------------

_DEFAULT_SERVER_TOML = """\
[server]
host                  = "0.0.0.0"
port                  = 9999
heartbeat_timeout_sec = 15
# silent_alarm = true  → alarm is NOT shown on the triggering room's screen (default)
# silent_alarm = false → alarm is shown on ALL screens including the sender
silent_alarm          = true
log_file              = ""
"""

_DEFAULT_CLIENT_TOML = """\
[client]
room_name   = "Room 1"
server_ip   = "127.0.0.1"
server_port = 9999
# macOS default: "cmd+n"  |  Windows/Linux default: "alt+n"
hotkey      = "{hotkey}"
alarm_sound = ""
log_file    = ""
"""


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _load_toml(path: Path) -> Dict[str, Any]:
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def load_server_config(path: Optional[Union[str, Path]] = None) -> ServerConfig:
    """
    Load server config from *path*.
    If path is None, look for ``server_config.toml`` next to this file,
    then in the current working directory.
    If no file is found, write a default one to the cwd and return defaults.
    """
    candidates = _resolve_candidates(path, "server_config.toml")
    for candidate in candidates:
        if candidate.exists():
            data = _load_toml(candidate)
            section = data.get("server", {})
            return ServerConfig(**{k: v for k, v in section.items() if k in ServerConfig.__dataclass_fields__})

    # No file found — write default to cwd so the operator can edit it
    default_path = Path.cwd() / "server_config.toml"
    default_path.write_text(_DEFAULT_SERVER_TOML, encoding="utf-8")
    print(f"[config] No server_config.toml found. Created default at {default_path}")
    return ServerConfig()


def load_client_config(path: Optional[Union[str, Path]] = None) -> ClientConfig:
    """
    Load client config from *path*.
    If path is None, look for ``client_config.toml`` next to this file,
    then in the current working directory.
    If no file is found, write a default one to the cwd and return defaults.
    """
    candidates = _resolve_candidates(path, "client_config.toml")
    for candidate in candidates:
        if candidate.exists():
            data = _load_toml(candidate)
            section = data.get("client", {})
            cfg = ClientConfig(**{k: v for k, v in section.items()
                                  if k in ClientConfig.__dataclass_fields__ and not k.startswith("_")})
            cfg._config_path = str(candidate)
            return cfg

    default_path = Path.cwd() / "client_config.toml"
    default_path.write_text(_DEFAULT_CLIENT_TOML.format(hotkey=_default_hotkey()), encoding="utf-8")
    print(f"[config] No client_config.toml found. Created default at {default_path}")
    cfg = ClientConfig()
    cfg._config_path = str(default_path)
    return cfg


def save_client_config(cfg: ClientConfig, path: Optional[Union[str, Path]] = None) -> None:
    """Persist *cfg* back to client_config.toml (overwrites the file)."""
    # Use the path the config was loaded from, if available
    if path is None and cfg._config_path:
        target = Path(cfg._config_path)
    else:
        candidates = _resolve_candidates(path, "client_config.toml")
        target = next((p for p in candidates if p.exists()), candidates[-1])
    content = (
        "[client]\n"
        f'room_name   = "{cfg.room_name}"\n'
        f'server_ip   = "{cfg.server_ip}"\n'
        f"server_port = {cfg.server_port}\n"
        f'hotkey      = "{cfg.hotkey}"\n'
        f'alarm_sound = "{cfg.alarm_sound}"\n'
        f'log_file    = "{cfg.log_file}"\n'
        f"muted       = {'true' if cfg.muted else 'false'}\n"
    )
    target.write_text(content, encoding="utf-8")


def _resolve_candidates(path: Optional[Union[str, Path]], filename: str) -> List[Path]:
    if path is not None:
        return [Path(path)]
    return [
        Path(os.environ.get("ALARM_CONFIG_DIR", ".")) / filename,
        Path.cwd() / filename,
        Path(__file__).parent.parent / "config" / filename,
    ]
