"""
protocol.py — Message types and JSON serialisation/deserialisation.

All WebSocket messages are JSON objects with at least a "type" field.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, asdict
from typing import Any, Dict, Union

# ---------------------------------------------------------------------------
# Message type constants
# ---------------------------------------------------------------------------

MSG_REGISTER    = "register"
MSG_ALARM       = "alarm"
MSG_HEARTBEAT   = "heartbeat"
MSG_DISMISS     = "dismiss"
MSG_CLIENT_DOWN = "client_down"
MSG_CLIENT_UP   = "client_up"
MSG_CLIENT_LIST = "client_list"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class RegisterMsg:
    room: str
    type: str = MSG_REGISTER


@dataclass
class AlarmMsg:
    room: str
    type: str = MSG_ALARM


@dataclass
class HeartbeatMsg:
    room: str
    type: str = MSG_HEARTBEAT


@dataclass
class DismissMsg:
    room: str
    type: str = MSG_DISMISS


@dataclass
class ClientDownMsg:
    room: str
    type: str = MSG_CLIENT_DOWN


@dataclass
class ClientUpMsg:
    room: str
    type: str = MSG_CLIENT_UP


@dataclass
class ClientListMsg:
    clients: list  # [{"room": str, "is_down": bool}, ...]
    type: str = MSG_CLIENT_LIST


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

AnyMsg = Union[RegisterMsg, AlarmMsg, HeartbeatMsg, DismissMsg, ClientDownMsg, ClientUpMsg, ClientListMsg]

_TYPE_MAP: Dict[str, type] = {
    MSG_REGISTER:    RegisterMsg,
    MSG_ALARM:       AlarmMsg,
    MSG_HEARTBEAT:   HeartbeatMsg,
    MSG_DISMISS:     DismissMsg,
    MSG_CLIENT_DOWN: ClientDownMsg,
    MSG_CLIENT_UP:   ClientUpMsg,
    MSG_CLIENT_LIST: ClientListMsg,
}


def encode(msg: AnyMsg) -> str:
    """Serialise a message dataclass to a JSON string."""
    return json.dumps(asdict(msg))


def decode(raw: str) -> AnyMsg:
    """
    Deserialise a JSON string into the appropriate message dataclass.
    Raises ValueError for unknown or malformed messages.
    """
    try:
        data: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc

    msg_type = data.get("type")
    cls = _TYPE_MAP.get(msg_type)  # type: ignore[arg-type]
    if cls is None:
        raise ValueError(f"Unknown message type: {msg_type!r}")

    try:
        valid_fields = {f.name for f in dataclasses.fields(cls)}
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered)
    except TypeError as exc:
        raise ValueError(f"Malformed {msg_type!r} message: {exc}") from exc
