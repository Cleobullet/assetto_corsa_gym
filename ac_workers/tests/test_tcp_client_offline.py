"""Offline tests for persistent TCP client helpers."""
from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def test_is_addr_in_use_error_windows():
    from common.tcp_client import is_addr_in_use_error

    exc = OSError("address in use")
    exc.winerror = 10048  # type: ignore[attr-defined]
    assert is_addr_in_use_error(exc) is True


def test_persistent_connection_reuses_socket():
    from common.tcp_client import PersistentTcpConnection

    conn = PersistentTcpConnection("127.0.0.1", 59990, timeout=1.0, max_attempts=2)
    seen: list[int] = []

    def _send(sock: socket.socket) -> None:
        seen.append(id(sock))
        sock.sendall(b"ping\n")

    def _recv(sock: socket.socket) -> bytes:
        return b"ok\n"

    fake_sock = mock.Mock()
    fake_sock.sendall = mock.Mock()
    fake_sock.recv = mock.Mock(return_value=b"ok\n")

    with mock.patch.object(conn, "_connect", return_value=fake_sock):
        conn.transact(_send, _recv)
        conn.transact(_send, _recv)
    assert len(seen) == 2
    assert seen[0] == seen[1]
    conn.close()


def test_persistent_connection_retries_on_10048():
    from common.tcp_client import PersistentTcpConnection

    conn = PersistentTcpConnection("127.0.0.1", 59991, timeout=0.5, max_attempts=3)
    attempts = {"n": 0}

    def flaky_connect() -> socket.socket:
        attempts["n"] += 1
        if attempts["n"] < 2:
            exc = OSError("address in use")
            exc.winerror = 10048  # type: ignore[attr-defined]
            raise exc
        sock = mock.Mock()
        sock.sendall = mock.Mock()
        sock.recv = mock.Mock(return_value=b"{}\n")
        return sock

    with mock.patch.object(conn, "_connect", side_effect=flaky_connect), mock.patch(
        "common.tcp_client.time.sleep"
    ):
        payload = conn.transact(lambda s: s.sendall(b"x\n"), lambda s: s.recv(65536))
    assert payload == b"{}\n"
    assert attempts["n"] == 2


def test_simulation_management_uses_ephemeral_tcp():
    """Mgmt must not hold a persistent socket (AC plugin is single-client)."""
    from AssettoCorsaEnv.ac_client import SimulationManagement

    class _Cfg:
        simulation_management_server_host_name = "127.0.0.1"
        simulation_management_server_port = 59992

    mgmt = SimulationManagement(_Cfg())
    assert not hasattr(mgmt, "_conn") or getattr(mgmt, "_conn", None) is None
    sockets: list[int] = []

    def _fake_socket(*_a, **_k):
        sock = mock.Mock()
        sock.connect = mock.Mock()
        sock.sendall = mock.Mock()
        sock.recv = mock.Mock(side_effect=[b'{"driving":true}', b""])
        sock.__enter__ = mock.Mock(return_value=sock)
        sock.__exit__ = mock.Mock(return_value=False)
        sockets.append(id(sock))
        return sock

    with mock.patch("AssettoCorsaEnv.ac_client.socket.socket", side_effect=_fake_socket), mock.patch(
        "common.tcp_client.configure_client_socket"
    ):
        mgmt.get_driving_state()
        mgmt.get_driving_state()
    assert len(sockets) == 2
    assert sockets[0] != sockets[1]


def test_mgmt_recv_returns_without_waiting_for_close():
    """Plugin mgmt server replies then keeps the socket OPEN; recv must not block
    until EOF (that was the bug that lost every driving-state reply)."""
    from AssettoCorsaEnv.ac_client import SimulationManagement

    class _Cfg:
        simulation_management_server_host_name = "127.0.0.1"
        simulation_management_server_port = 59993

    mgmt = SimulationManagement(_Cfg())

    class _Sock:
        def __init__(self):
            self._sent_reply = False

        def settimeout(self, _t):
            pass

        def recv(self, _n):
            if not self._sent_reply:
                self._sent_reply = True
                return b'{"driving": true}'
            # Server never closes -> a real socket would raise timeout here.
            raise socket.timeout("timed out")

    state = mgmt._recv_response(_Sock())
    assert state == '{"driving": true}'


def test_plugin_drive_ready_accepts_launcher_driving_flag():
    from ac_workers.collector_live_attach import _plugin_drive_ready

    assert _plugin_drive_ready(
        {"driving": True, "sim_live": True, "physics_brake": 0.0, "menu_brake_hold": False}
    )
    assert not _plugin_drive_ready(
        {"driving": False, "sim_live": True, "physics_brake": 1.0, "menu_brake_hold": True}
    )


def test_transition_client_health_and_push():
    from common.transition_server import TransitionClient, TransitionServer

    server = TransitionServer(host="127.0.0.1", port=0)
    server.start()
    time.sleep(0.05)
    port = int(server._server_sock.getsockname()[1])  # type: ignore[union-attr]
    client = TransitionClient(port=port, timeout=2.0)
    try:
        assert client.health().get("ok") is True
        assert client.push_transition({"worker_id": 0, "reward": 0.0}) is True
        stats = client.stats()
        assert stats.get("queue_size", 0) >= 1
    finally:
        server.stop()


def main() -> int:
    tests = [
        test_is_addr_in_use_error_windows,
        test_persistent_connection_reuses_socket,
        test_persistent_connection_retries_on_10048,
        test_simulation_management_uses_ephemeral_tcp,
        test_mgmt_recv_returns_without_waiting_for_close,
        test_plugin_drive_ready_accepts_launcher_driving_flag,
        test_transition_client_health_and_push,
    ]
    for fn in tests:
        fn()
        print("OK", fn.__name__)
    print("Test-TcpClient-Offline: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
