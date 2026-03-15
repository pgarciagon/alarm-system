"""
test_protocol.py — Unit tests for common/protocol.py
"""

import pytest
import json

from common.protocol import (
    AlarmMsg,
    ClientDownMsg,
    ClientUpMsg,
    DismissMsg,
    HeartbeatMsg,
    RegisterMsg,
    decode,
    encode,
    MSG_ALARM,
    MSG_CLIENT_DOWN,
    MSG_CLIENT_UP,
    MSG_DISMISS,
    MSG_HEARTBEAT,
    MSG_REGISTER,
)


# ---------------------------------------------------------------------------
# encode
# ---------------------------------------------------------------------------

class TestEncode:
    def test_register(self):
        msg = RegisterMsg(room="Room 1")
        raw = encode(msg)
        data = json.loads(raw)
        assert data["type"] == MSG_REGISTER
        assert data["room"] == "Room 1"

    def test_alarm(self):
        msg = AlarmMsg(room="Room 3")
        raw = encode(msg)
        data = json.loads(raw)
        assert data["type"] == MSG_ALARM
        assert data["room"] == "Room 3"

    def test_heartbeat(self):
        raw = encode(HeartbeatMsg(room="Room 2"))
        data = json.loads(raw)
        assert data["type"] == MSG_HEARTBEAT

    def test_client_down(self):
        raw = encode(ClientDownMsg(room="Room 5"))
        data = json.loads(raw)
        assert data["type"] == MSG_CLIENT_DOWN

    def test_client_up(self):
        raw = encode(ClientUpMsg(room="Room 5"))
        data = json.loads(raw)
        assert data["type"] == MSG_CLIENT_UP

    def test_dismiss(self):
        raw = encode(DismissMsg(room="Room 7"))
        data = json.loads(raw)
        assert data["type"] == MSG_DISMISS


# ---------------------------------------------------------------------------
# decode
# ---------------------------------------------------------------------------

class TestDecode:
    def _raw(self, **kwargs) -> str:
        return json.dumps(kwargs)

    def test_decode_register(self):
        msg = decode(self._raw(type=MSG_REGISTER, room="Room 1"))
        assert isinstance(msg, RegisterMsg)
        assert msg.room == "Room 1"

    def test_decode_alarm(self):
        msg = decode(self._raw(type=MSG_ALARM, room="Room 3"))
        assert isinstance(msg, AlarmMsg)
        assert msg.room == "Room 3"

    def test_decode_heartbeat(self):
        msg = decode(self._raw(type=MSG_HEARTBEAT, room="Room 2"))
        assert isinstance(msg, HeartbeatMsg)

    def test_decode_client_down(self):
        msg = decode(self._raw(type=MSG_CLIENT_DOWN, room="Room 4"))
        assert isinstance(msg, ClientDownMsg)

    def test_decode_client_up(self):
        msg = decode(self._raw(type=MSG_CLIENT_UP, room="Room 4"))
        assert isinstance(msg, ClientUpMsg)

    def test_decode_dismiss(self):
        msg = decode(self._raw(type=MSG_DISMISS, room="Room 9"))
        assert isinstance(msg, DismissMsg)

    def test_decode_extra_fields_are_ignored(self):
        # Extra fields should not cause an error
        msg = decode(self._raw(type=MSG_ALARM, room="Room X", extra="ignored"))
        assert isinstance(msg, AlarmMsg)
        assert msg.room == "Room X"

    def test_decode_invalid_json(self):
        with pytest.raises(ValueError, match="Invalid JSON"):
            decode("not-json")

    def test_decode_unknown_type(self):
        with pytest.raises(ValueError, match="Unknown message type"):
            decode(self._raw(type="unknown_type", room="X"))

    def test_decode_missing_required_field(self):
        with pytest.raises(ValueError):
            decode(json.dumps({"type": MSG_ALARM}))   # missing "room"

    def test_roundtrip(self):
        original = AlarmMsg(room="Room 11")
        decoded = decode(encode(original))
        assert decoded.type == original.type
        assert decoded.room == original.room
