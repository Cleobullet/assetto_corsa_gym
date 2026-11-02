"""Local TCP IPC for C4D Control Center (dashboard + command console)."""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable

C4D_HOST = "127.0.0.1"
C4D_DEFAULT_PORT = 24700
FRAME_INTERVAL_SEC = 0.5
PROMPT = "C4D command> "


def _send_msg(sock: socket.socket, payload: dict[str, Any]) -> None:
    data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    sock.sendall(data)


def _recv_line(sock: socket.socket, buf: bytearray) -> dict[str, Any] | None:
    while True:
        nl = buf.find(b"\n")
        if nl >= 0:
            line = bytes(buf[:nl])
            del buf[: nl + 1]
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            return json.loads(text)
        chunk = sock.recv(4096)
        if not chunk:
            return None
        buf.extend(chunk)


class C4DControlServer:
    """Central server: runs ControlCenter logic, serves dashboard + command clients."""

    def __init__(
        self,
        center: Any,
        host: str = C4D_HOST,
        port: int = C4D_DEFAULT_PORT,
        frame_builder: Callable[[], str] | None = None,
        *,
        session_token: str = "",
        repo_root: Path | None = None,
        session_file: Path | None = None,
        runtime_lock: Any | None = None,
    ) -> None:
        self.center = center
        self.host = host
        self.port = port
        self.session_token = session_token
        self.repo_root = repo_root
        self.session_file = session_file
        self._runtime_lock = runtime_lock
        self._frame_builder = frame_builder or (lambda: center.build_frame(message=center._message, include_prompt=False))
        self._lock = threading.Lock()
        self._dashboards: list[socket.socket] = []
        self._console: socket.socket | None = None
        self._shutdown = threading.Event()
        self._clients_lock = threading.Lock()

    def _reject_hello(self, sock: socket.socket, reason: str) -> None:
        try:
            _send_msg(sock, {"t": "hello_reject", "error": reason})
        except OSError:
            pass

    def _validate_hello(self, hello: dict[str, Any]) -> str | None:
        if not self.session_token:
            return None
        offered = str(hello.get("token") or "")
        if not offered or offered != self.session_token:
            return "session token mismatch"
        repo = str(hello.get("repo_root") or "")
        if self.repo_root is not None and repo:
            if Path(repo).resolve() != Path(self.repo_root).resolve():
                return "repo root mismatch"
        return None

    def _broadcast_shutdown(self) -> None:
        msg = {"t": "shutdown"}
        with self._clients_lock:
            targets = list(self._dashboards)
            if self._console is not None:
                targets.append(self._console)
        for sock in targets:
            try:
                _send_msg(sock, msg)
            except OSError:
                pass

    def _remove_dashboard(self, sock: socket.socket) -> None:
        with self._clients_lock:
            if sock in self._dashboards:
                self._dashboards.remove(sock)

    def _handle_console_cmd(self, sock: socket.socket, line: str) -> None:
        with self._lock:
            result = self.center.handle_command(line)
            if self.center._quit:
                _send_msg(sock, {"t": "reply", "text": result or "", "quit": True})
                self._broadcast_shutdown()
                self._shutdown.set()
                return
        if result == "__CLEAR__":
            with self._lock:
                self.center._message = ""
            _send_msg(sock, {"t": "reply", "text": "", "quit": False})
            return
        _send_msg(sock, {"t": "reply", "text": result or "", "quit": False})

    def _client_loop(self, sock: socket.socket) -> None:
        buf = bytearray()
        role = ""
        try:
            hello = _recv_line(sock, buf)
            if not hello or hello.get("t") != "hello":
                return
            reject = self._validate_hello(hello)
            if reject:
                self._reject_hello(sock, reject)
                return
            role = str(hello.get("role") or "")
            _send_msg(sock, {"t": "hello_ok", "token": self.session_token})
            client_pid = int(hello.get("client_pid") or 0)
            if role in ("dashboard", "console") and self.repo_root is not None and client_pid > 0:
                from ac_workers.c4d_server_lock import update_session_fields

                key = "{0}_pid".format(role)
                update_session_fields(self.repo_root, **{key: client_pid})
            if role == "dashboard":
                with self._clients_lock:
                    self._dashboards.append(sock)
                frame = self._frame_builder()
                _send_msg(sock, {"t": "frame", "text": frame})
                while not self._shutdown.is_set():
                    msg = _recv_line(sock, buf)
                    if msg is None:
                        break
                    if msg.get("t") == "shutdown":
                        break
            elif role == "console":
                with self._clients_lock:
                    self._console = sock
                while not self._shutdown.is_set():
                    msg = _recv_line(sock, buf)
                    if msg is None:
                        break
                    if msg.get("t") == "cmd":
                        self._handle_console_cmd(sock, str(msg.get("line") or ""))
                        if self._shutdown.is_set():
                            break
        except (OSError, json.JSONDecodeError):
            pass
        finally:
            if role == "dashboard":
                self._remove_dashboard(sock)
            elif role == "console":
                with self._clients_lock:
                    if self._console is sock:
                        self._console = None
            try:
                sock.close()
            except OSError:
                pass

    def _frame_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                changed = False
                with self._lock:
                    changed = self.center._poll_background_ops()
                    if changed:
                        self.center.refresh_metrics_cache(force=True)
                frame = self._frame_builder()
                with self._clients_lock:
                    dashboards = list(self._dashboards)
                dead: list[socket.socket] = []
                for sock in dashboards:
                    try:
                        _send_msg(sock, {"t": "frame", "text": frame})
                    except OSError:
                        dead.append(sock)
                for sock in dead:
                    self._remove_dashboard(sock)
            except Exception:
                pass
            self._shutdown.wait(FRAME_INTERVAL_SEC)

    def run(self, ready_file: Path | None = None) -> int:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self.port))
        listener.listen(8)
        listener.settimeout(1.0)
        if ready_file is not None:
            from ac_workers.c4d_server_lock import write_ready_file

            write_ready_file(ready_file, self.host, self.port, self.session_token, os.getpid())
        frame_thread = threading.Thread(target=self._frame_loop, name="c4d-frames", daemon=True)
        frame_thread.start()
        try:
            while not self._shutdown.is_set():
                try:
                    client, _addr = listener.accept()
                except socket.timeout:
                    continue
                threading.Thread(
                    target=self._client_loop,
                    args=(client,),
                    name="c4d-client",
                    daemon=True,
                ).start()
        finally:
            self._shutdown.set()
            listener.close()
            self._broadcast_shutdown()
            if self._runtime_lock is not None:
                self._runtime_lock.release_cleanup()
        return 0


def connect_client(
    host: str,
    port: int,
    role: str,
    *,
    token: str = "",
    repo_root: Path | str | None = None,
    timeout: float = 10.0,
) -> tuple[socket.socket, bytearray]:
    sock = socket.create_connection((host, port), timeout=timeout)
    buf = bytearray()
    hello: dict[str, Any] = {"t": "hello", "role": role, "client_pid": os.getpid()}
    if token:
        hello["token"] = token
    if repo_root is not None:
        hello["repo_root"] = str(repo_root)
    _send_msg(sock, hello)
    ack = _recv_line(sock, buf)
    if ack and ack.get("t") == "hello_reject":
        raise ConnectionError(str(ack.get("error") or "C4D server rejected session"))
    if not ack or ack.get("t") != "hello_ok":
        raise ConnectionError("C4D server handshake failed")
    sock.settimeout(None)
    return sock, buf


def run_dashboard_client(
    host: str,
    port: int,
    *,
    token: str = "",
    repo_root: Path | str | None = None,
) -> int:
    import sys

    from ac_workers.control_center import TerminalRenderer, enable_ansi_stdout

    enable_ansi_stdout()
    renderer = TerminalRenderer()
    sock, buf = connect_client(host, port, "dashboard", token=token, repo_root=repo_root)
    try:
        while True:
            try:
                msg = _recv_line(sock, buf)
            except (OSError, json.JSONDecodeError):
                break
            if msg is None:
                break
            kind = msg.get("t")
            if kind == "frame":
                try:
                    renderer.paint(str(msg.get("text") or ""))
                except OSError:
                    break
            elif kind == "shutdown":
                break
    finally:
        try:
            sock.close()
        except OSError:
            pass
        try:
            sys.stdout.write("\n")
            sys.stdout.flush()
        except OSError:
            pass
    return 0


def run_console_client(
    host: str,
    port: int,
    *,
    token: str = "",
    repo_root: Path | str | None = None,
) -> int:
    import sys

    sock, buf = connect_client(host, port, "console", token=token, repo_root=repo_root)
    try:
        while True:
            try:
                line = input(PROMPT)
            except (EOFError, KeyboardInterrupt):
                print()
                line = "quit"
            _send_msg(sock, {"t": "cmd", "line": line})
            reply = _recv_line(sock, buf)
            if reply is None:
                break
            text = str(reply.get("text") or "")
            if text:
                print(text)
            if reply.get("quit"):
                break
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return 0
