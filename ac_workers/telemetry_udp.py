"""UDP telemetry helpers — diagnostic only; do NOT use while collector is active."""
from __future__ import annotations

import json
import logging
import socket
import sys
import time
from typing import Any

logger = logging.getLogger(__name__)

try:
    from ac_workers.udp_windows import configure_udp_client, recv_udp_safe
except ImportError:
    configure_udp_client = lambda _s: None  # noqa: E731
    recv_udp_safe = None


def udp_connect_and_sample(
    ego_port: int,
    host: str = "127.0.0.1",
    timeout: float = 5.0,
    *,
    allow_external: bool = False,
) -> dict[str, Any] | None:
    """Offline/tests only unless allow_external=True (collector uses AssettoCorsaEnv.Client)."""
    if not allow_external:
        logger.warning(
            "udp_connect_and_sample port=%s blocked — use collector diagnostic channel",
            ego_port,
        )
        return None
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    configure_udp_client(sock)
    try:
        sock.settimeout(1.0)
        deadline = time.time() + timeout
        sock.sendto(b"connect", (host, ego_port))
        while time.time() < deadline:
            try:
                if recv_udp_safe:
                    data, addr = recv_udp_safe(sock, 65536, "diag-udp", ego_port)
                else:
                    data, addr = sock.recvfrom(65536)
                if not data:
                    continue
                text = data.decode("utf-8", errors="replace")
                if text == "identified":
                    continue
                if text in ("disconnect", "connect"):
                    continue
                try:
                    state = json.loads(text)
                    if isinstance(state, dict):
                        return state
                except json.JSONDecodeError:
                    pass
                state = _eval_state(text)
                if state:
                    return state
            except socket.timeout:
                sock.sendto(b"connect", (host, ego_port))
                continue
            except ConnectionResetError:
                logger.warning("UDP 10054 on port=%s role=diag — retry", ego_port)
                sock.sendto(b"connect", (host, ego_port))
                continue
    finally:
        try:
            sock.sendto(b"disconnect", (host, ego_port))
        except Exception:
            pass
        sock.close()
    return None

def _eval_state(text: str) -> dict[str, Any]:
    try:
        value = eval(text, {"__builtins__": {}}, {})
        if isinstance(value, dict):
            return value
    except Exception:
        pass
    return {}
