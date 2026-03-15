"""
simulate.py — Headless simulation helper.

Runs server + N clients in the same process using asyncio subprocesses.
Useful for quick smoke-testing without needing multiple terminal windows.

Usage:
    python sim/simulate.py [--rooms N] [--alarm-from ROOM_NAME]

    --rooms N             Number of rooms to simulate (default: 3)
    --alarm-from NAME     After startup, trigger an alarm from this room name
                          (default: "Room 1")
    --duration SECS       How long to run the simulation (default: 10)

Example:
    python sim/simulate.py --rooms 5 --alarm-from "Room 3" --duration 15
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from common.config import ServerConfig, ClientConfig
from common.protocol import (
    AlarmMsg,
    HeartbeatMsg,
    RegisterMsg,
    encode,
    decode,
    MSG_ALARM,
    MSG_CLIENT_DOWN,
    MSG_CLIENT_UP,
)

import websockets


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-20s %(levelname)s %(message)s",
)
log = logging.getLogger("simulate")

SERVER_PORT = 19999   # use a high port to avoid clashing with a real server


# ---------------------------------------------------------------------------
# Inline server (same logic as server.py but without the full class overhead)
# ---------------------------------------------------------------------------

async def run_server(port: int, heartbeat_timeout: int = 15) -> None:
    from server.server import AlarmServer, ServerConfig

    cfg = ServerConfig(host="127.0.0.1", port=port, heartbeat_timeout_sec=heartbeat_timeout)
    srv = AlarmServer(cfg)
    await srv.run()


# ---------------------------------------------------------------------------
# Simulated client
# ---------------------------------------------------------------------------

class SimClient:
    def __init__(self, room: str, port: int) -> None:
        self.room = room
        self.port = port
        self.received: list[dict] = []
        self._ws = None
        self._alarm_event: asyncio.Event | None = None

    async def run(self, duration: float) -> None:
        self._alarm_event = asyncio.Event()
        uri = f"ws://127.0.0.1:{self.port}"
        try:
            async with websockets.connect(uri, open_timeout=5) as ws:
                self._ws = ws
                await ws.send(encode(RegisterMsg(room=self.room)))
                log.info("[%s] registered", self.room)

                await asyncio.gather(
                    self._recv(ws),
                    self._heartbeat(ws),
                    asyncio.sleep(duration),
                    return_exceptions=True,
                )
        except Exception as exc:
            log.warning("[%s] connection error: %s", self.room, exc)

    async def _recv(self, ws) -> None:
        async for raw in ws:
            try:
                msg = decode(str(raw))
                self.received.append({"type": msg.type, "room": msg.room})
                if msg.type == MSG_ALARM:
                    log.info("[%s] *** ALARM received from %s ***", self.room, msg.room)
                elif msg.type == MSG_CLIENT_DOWN:
                    log.warning("[%s] client_down: %s", self.room, msg.room)
                elif msg.type == MSG_CLIENT_UP:
                    log.info("[%s] client_up: %s", self.room, msg.room)
            except ValueError:
                pass

    async def _heartbeat(self, ws) -> None:
        msg = encode(HeartbeatMsg(room=self.room))
        while True:
            await asyncio.sleep(5)
            try:
                await ws.send(msg)
            except Exception:
                break

    async def send_alarm(self) -> None:
        if self._ws:
            await self._ws.send(encode(AlarmMsg(room=self.room)))
            log.info("[%s] alarm SENT", self.room)


# ---------------------------------------------------------------------------
# Main simulation
# ---------------------------------------------------------------------------

async def main(rooms: int, alarm_from: str, duration: float) -> None:
    # Start server as a background task
    server_task = asyncio.create_task(run_server(SERVER_PORT))
    await asyncio.sleep(0.5)   # let server bind

    # Create and start clients
    clients = [SimClient(f"Room {i+1}", SERVER_PORT) for i in range(rooms)]
    client_tasks = [asyncio.create_task(c.run(duration)) for c in clients]
    await asyncio.sleep(1)     # let clients register

    # Trigger alarm from requested room
    trigger = next((c for c in clients if c.room == alarm_from), clients[0])
    log.info("=== Triggering alarm from %s ===", trigger.room)
    await trigger.send_alarm()

    # Wait for simulation to complete
    await asyncio.sleep(duration)
    for t in client_tasks:
        t.cancel()
    server_task.cancel()

    # Report
    print("\n--- Simulation report ---")
    for c in clients:
        alarm_count = sum(1 for m in c.received if m["type"] == MSG_ALARM)
        down_count  = sum(1 for m in c.received if m["type"] == MSG_CLIENT_DOWN)
        print(
            f"  {c.room:10s}  alarms received: {alarm_count}  "
            f"client_down msgs: {down_count}"
        )
    print("-------------------------\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Alarm system headless simulation")
    parser.add_argument("--rooms",      type=int,   default=3,        help="Number of rooms")
    parser.add_argument("--alarm-from", type=str,   default="Room 1", help="Room that triggers alarm")
    parser.add_argument("--duration",   type=float, default=10,       help="Simulation duration in seconds")
    args = parser.parse_args()

    asyncio.run(main(args.rooms, args.alarm_from, args.duration))
