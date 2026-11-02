"""Windows UDP client hardening (WinError 10054)."""
from __future__ import annotations

import logging
import socket
import struct
import sys

logger = logging.getLogger(__name__)

SIO_UDP_CONNRESET = 0x9800000C


def configure_udp_client(sock: socket.socket) -> None:
    """Disable ICMP port-unreachable -> ConnectionResetError on Windows UDP recv."""
    if not sys.platform.startswith("win"):
        return
    try:
        sock.ioctl(SIO_UDP_CONNRESET, struct.pack("I", 0))
    except (OSError, AttributeError, ValueError) as exc:
        logger.debug("SIO_UDP_CONNRESET not applied: %s", exc)


def recv_udp_safe(sock: socket.socket, bufsize: int, role: str, port: int) -> tuple[bytes, tuple]:
    """recvfrom with ConnectionResetError tolerance on Windows."""
    try:
        data, addr = sock.recvfrom(bufsize)
        return data, addr
    except ConnectionResetError as exc:
        logger.warning(
            "UDP ConnectionResetError role=%s local_port=%s err=%s — continuing poll",
            role,
            port,
            exc,
        )
        return b"", ("", 0)


def describe_udp_bind_conflict(port: int, host: str = "127.0.0.1") -> str:
    """Best-effort hint when UDP bind fails (port already owned)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind((host, port))
        probe.close()
        return ""
    except OSError as exc:
        msg = "UDP bind failed port={0} host={1}: {2}".format(port, host, exc)
        if sys.platform.startswith("win"):
            try:
                import subprocess

                out = subprocess.check_output(
                    ["netstat", "-ano", "-p", "udp"],
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                for line in out.splitlines():
                    if ":{0} ".format(port) in line or ":{0}\r".format(port) in line:
                        parts = line.split()
                        if parts:
                            msg += " ; netstat: " + line.strip()
            except Exception:
                pass
        return msg
