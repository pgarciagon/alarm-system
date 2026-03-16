"""
discovery.py — Scan the local /24 subnet for reachable alarm servers.

Uses asyncio TCP probes (no server-side changes needed).
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Callable, List, Optional


def _local_ip() -> Optional[str]:
    """Return the machine's primary LAN IP (not loopback)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def local_subnet() -> Optional[str]:
    """Return the local /24 subnet string, e.g. '192.168.1.0/24'."""
    ip = _local_ip()
    if not ip:
        return None
    return str(ipaddress.ip_network(f"{ip}/24", strict=False))


async def _probe(ip: str, port: int, timeout: float) -> Optional[str]:
    """Return *ip* if port is open, else None."""
    try:
        _r, w = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout
        )
        w.close()
        try:
            await w.wait_closed()
        except Exception:
            pass
        return ip
    except Exception:
        return None


async def scan_subnet(
    port: int = 9999,
    timeout: float = 0.4,
    progress_cb: Optional[Callable[[int, int, str, bool], None]] = None,
) -> List[str]:
    """
    Scan all 254 hosts of the local /24 subnet for *port*.

    *progress_cb(done, total, ip, found)* is called from the asyncio thread
    after each probe completes:
      - done:  number of probes finished so far
      - total: total number of hosts to scan
      - ip:    the IP that was just probed
      - found: True if that IP had the port open

    Returns a sorted list of IP strings where the port was reachable.
    """
    local_ip = _local_ip()
    if not local_ip:
        return []

    network = ipaddress.ip_network(f"{local_ip}/24", strict=False)
    hosts = [str(h) for h in network.hosts()]
    total = len(hosts)
    done = 0
    found: List[str] = []

    sem = asyncio.Semaphore(64)  # max 64 concurrent probes

    async def _bounded(ip: str) -> None:
        nonlocal done
        async with sem:
            result = await _probe(ip, port, timeout)
        done += 1
        is_found = result is not None
        if is_found:
            found.append(ip)
        if progress_cb:
            progress_cb(done, total, ip, is_found)

    await asyncio.gather(*[_bounded(h) for h in hosts])
    return sorted(found)
