"""Persistent TCP client helpers with Windows ephemeral-port (10048) retry."""
from __future__ import annotations

import socket
import threading
import time
from typing import Callable

# Linux EADDRINUSE, Windows WSAEADDRINUSE (ephemeral port / local bind exhaustion).
_ADDRINUSE_ERRNOS = frozenset({98, 10048})
# Transient connect / transport errors worth retrying.
_TRANSIENT_CONNECT_ERRNOS = frozenset({10054, 10060, 10061, 104, 110, 111})


def os_error_code(exc: BaseException) -> int | None:
    if not isinstance(exc, OSError):
        return None
    return getattr(exc, "winerror", None) or getattr(exc, "errno", None)


def is_addr_in_use_error(exc: BaseException) -> bool:
    code = os_error_code(exc)
    return code in _ADDRINUSE_ERRNOS


def is_transient_tcp_error(exc: BaseException) -> bool:
    if is_addr_in_use_error(exc):
        return True
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return True
    if isinstance(exc, TimeoutError):
        return True
    code = os_error_code(exc)
    return code in _TRANSIENT_CONNECT_ERRNOS


def configure_client_socket(sock: socket.socket, *, timeout: float) -> None:
    sock.settimeout(timeout)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except OSError:
        pass
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 0)
        except OSError:
            pass


class PersistentTcpConnection:
    """One reusable TCP stream per remote host:port (thread-safe)."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout: float = 5.0,
        max_attempts: int = 5,
    ):
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self.max_attempts = max(1, int(max_attempts))
        self._sock: socket.socket | None = None
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._drop()

    def _drop(self) -> None:
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _connect(self) -> socket.socket:
        last_exc: BaseException | None = None
        for attempt in range(self.max_attempts):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            configure_client_socket(sock, timeout=self.timeout)
            try:
                sock.connect((self.host, self.port))
                return sock
            except OSError as exc:
                last_exc = exc
                try:
                    sock.close()
                except OSError:
                    pass
                if is_transient_tcp_error(exc) and attempt + 1 < self.max_attempts:
                    time.sleep(0.15 * (attempt + 1))
                    continue
                raise
        if last_exc is not None:
            raise last_exc
        raise OSError("TCP connect failed for {0}:{1}".format(self.host, self.port))

    def _ensure(self) -> socket.socket:
        if self._sock is not None:
            return self._sock
        self._sock = self._connect()
        return self._sock

    def transact(
        self,
        send_fn: Callable[[socket.socket], None],
        recv_fn: Callable[[socket.socket], bytes | None] | None = None,
    ) -> bytes | None:
        """Run one request on the persistent socket; reconnect + retry on transient errors."""
        with self._lock:
            last_exc: BaseException | None = None
            for attempt in range(self.max_attempts):
                try:
                    sock = self._ensure()
                    send_fn(sock)
                    if recv_fn is None:
                        return None
                    return recv_fn(sock)
                except OSError as exc:
                    last_exc = exc
                    self._drop()
                    if is_transient_tcp_error(exc) and attempt + 1 < self.max_attempts:
                        time.sleep(0.15 * (attempt + 1))
                        continue
                    raise
            if last_exc is not None:
                raise last_exc
        return None
