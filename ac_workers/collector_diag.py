"""Local TCP diagnostic channel for worker collectors (C4D)."""
from __future__ import annotations

import json
import logging
import socket
import sys
import threading
import time
import traceback
import uuid
from typing import Any, Callable

logger = logging.getLogger(__name__)

DIAG_BASE_PORT = 24100
LISTENER_HOST = "127.0.0.1"
CLIENT_TIMEOUT = 3.0
ACCEPT_TIMEOUT = 0.5
CONN_TIMEOUT = 0.5
READ_CHUNK_TIMEOUT = 0.15
_WIN_EXCLUSIVE_ADDRUSE = getattr(socket, "SO_EXCLUSIVEADDRUSE", -5)
_ADDRINUSE_ERRNOS = frozenset({98, 10048})  # Linux EADDRINUSE, Windows WSAEADDRINUSE


def _send_json_line(conn: socket.socket, payload: dict[str, Any]) -> None:
    """Send one JSON line; raises on failure (use _safe_send_json_line in the server)."""
    if not _safe_send_json_line(conn, payload):
        raise OSError("diagnostic send failed")


def _safe_send_json_line(conn: socket.socket, payload: dict[str, Any]) -> bool:
    """Best-effort send; never raises."""
    try:
        data = (json.dumps(payload, default=str) + "\n").encode("utf-8")
        conn.sendall(data)
        return True
    except Exception as exc:
        _diag_log("collector diagnostic: send failed: {0}".format(exc))
        return False


def diag_port(worker_id: int) -> int:
    return DIAG_BASE_PORT + worker_id


def _diag_log(message: str) -> None:
    try:
        print(message, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        # Logging failure must not disconnect a healthy diagnostic client.
        pass


def configure_diag_listener_socket(sock: socket.socket) -> str:
    """Single-owner diagnostic listener: exclusive on Windows, no SO_REUSEADDR elsewhere."""
    if sys.platform == "win32":
        sock.setsockopt(socket.SOL_SOCKET, _WIN_EXCLUSIVE_ADDRUSE, 1)
        return "SO_EXCLUSIVEADDRUSE"
    return "exclusive(no SO_REUSEADDR)"


def diag_port_in_use_message(worker_id: int, port: int, exc: OSError) -> str:
    winerror = getattr(exc, "winerror", None)
    errno = exc.errno
    detail = exc.strerror or str(exc)
    code = winerror if winerror is not None else errno
    if winerror in _ADDRINUSE_ERRNOS or errno in _ADDRINUSE_ERRNOS:
        return (
            "collector worker {0} already running: diagnostic port {1} is owned by "
            "another process [WinError {2}: {3}]".format(worker_id, port, code, detail)
        )
    return "diagnostic bind failed on port {0} [WinError {1}: {2}]".format(port, code, detail)


class DiagRequestError(Exception):
    """Expected diagnostic client failure (no traceback)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class CollectorDiagClient:
    def __init__(self, worker_id: int, host: str = LISTENER_HOST, timeout: float = CLIENT_TIMEOUT):
        self.worker_id = worker_id
        self.host = host
        self.port = diag_port(worker_id)
        self.timeout = timeout

    def request(
        self,
        cmd: str,
        payload: dict[str, Any] | None = None,
        *,
        trace: list[str] | None = None,
    ) -> dict[str, Any]:
        request_id = uuid.uuid4().hex[:8]
        msg = {"cmd": cmd, "worker_id": self.worker_id, "request_id": request_id}
        if payload:
            msg.update(payload)
        data = (json.dumps(msg) + "\n").encode("utf-8")

        def _trace(line: str) -> None:
            if trace is not None:
                trace.append("[client rid={0}] {1}".format(request_id, line))

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            _trace("connect {0}:{1}".format(self.host, self.port))
            sock.connect((self.host, self.port))
            _trace("send {0} bytes cmd={1}".format(len(data), cmd))
            sock.sendall(data)
            _trace("recv started")
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
            _trace("recv {0} raw bytes".format(len(buf)))
            if not buf.strip():
                _trace("empty response (peer closed before newline)")
                raise DiagRequestError(
                    "collector worker {0} diagnostic empty response on port {1} (rid={2})".format(
                        self.worker_id, self.port, request_id
                    )
                )
            line = buf.split(b"\n", 1)[0]
            try:
                resp = json.loads(line.decode("utf-8"))
                resp_rid = resp.get("request_id")
                if resp_rid and resp_rid != request_id:
                    _trace("WARNING response rid={0} != request rid={1} (stale connection)".format(
                        resp_rid, request_id
                    ))
                _trace("json parsed ok response_rid={0}".format(resp_rid or "-"))
                return resp
            except (ValueError, json.JSONDecodeError) as exc:
                _trace("json parse FAILED: {0}".format(exc))
                raise DiagRequestError(
                    "collector worker {0} diagnostic invalid JSON on port {1}: {2}".format(
                        self.worker_id, self.port, exc
                    )
                ) from exc
        except socket.timeout as exc:
            _trace("timeout: {0}".format(exc))
            raise DiagRequestError(
                "collector worker {0} diagnostic timeout on port {1}".format(
                    self.worker_id, self.port
                )
            ) from exc
        except ConnectionRefusedError as exc:
            _trace("connection refused: {0}".format(exc))
            raise DiagRequestError(
                "collector worker {0} diagnostic refused on port {1}".format(
                    self.worker_id, self.port
                )
            ) from exc
        except OSError as exc:
            _trace("OSError errno={0} winerror={1}: {2}".format(
                exc.errno, getattr(exc, "winerror", None), exc
            ))
            raise DiagRequestError(
                "collector worker {0} diagnostic error on port {1}: {2}".format(
                    self.worker_id, self.port, exc
                )
            ) from exc
        finally:
            try:
                sock.close()
            except OSError:
                pass


class CollectorDiagServer:
    """One JSON request per TCP connection; returns cached snapshot only."""

    def __init__(
        self,
        worker_id: int,
        get_snapshot: Callable[[], dict[str, Any]],
        on_safe_action_test: Callable[[], dict[str, Any]],
        on_neutralize: Callable[[], dict[str, Any]],
        lock: threading.RLock,
        on_thread_fatal: Callable[[Exception], None] | None = None,
        on_worker_control: Callable[[str], dict[str, Any]] | None = None,
        on_vjoy_pulse: Callable[[dict[str, Any] | None], dict[str, Any]] | None = None,
        on_vjoy_selftest: Callable[[], dict[str, Any]] | None = None,
        on_ping: Callable[[], dict[str, Any]] | None = None,
        get_identity: Callable[[], dict[str, Any]] | None = None,
        on_request_outcome: Callable[[str, bool, str, str], None] | None = None,
    ):
        self.worker_id = worker_id
        self.port = diag_port(worker_id)
        self.host = LISTENER_HOST
        self._get_snapshot = get_snapshot
        self._on_safe_action_test = on_safe_action_test
        self._on_neutralize = on_neutralize
        self._on_worker_control = on_worker_control
        self._on_vjoy_pulse = on_vjoy_pulse
        self._on_vjoy_selftest = on_vjoy_selftest
        self._on_ping = on_ping
        self._get_identity = get_identity
        self._on_request_outcome = on_request_outcome
        self._on_thread_fatal = on_thread_fatal
        self._lock = lock
        self._stop = threading.Event()
        self._listener_ready = threading.Event()
        self._listener_failed = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._thread_error: Exception | None = None
        self._accept_loop_alive = False
        self._last_accept_at = 0.0
        self._last_handler_error = ""
        self._socket_closed = False
        self._last_successful_response_at = 0.0
        self._last_request_error = ""
        self._last_request_traceback = ""

    def _identity_fields(self) -> dict[str, Any]:
        if self._get_identity is not None:
            try:
                ident = dict(self._get_identity())
                if ident:
                    return ident
            except Exception:
                pass
        return {"worker_id": self.worker_id}

    def _error_response(self, cmd: str, error: str, exc: BaseException | None = None) -> dict[str, Any]:
        ident = self._identity_fields()
        tb = ""
        if exc is not None:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        elif error:
            tb = traceback.format_exc()
            if tb.strip() == "NoneType: None\n":
                tb = ""
        out = {
            "ok": False,
            "cmd": cmd,
            "error": error,
            "traceback": tb,
            "worker_id": ident.get("worker_id", self.worker_id),
            "collector_pid": ident.get("collector_pid"),
            "boot_id": ident.get("boot_id", ""),
            "diag_port": self.port,
        }
        return out

    def _record_outcome(self, cmd: str, resp: dict[str, Any]) -> None:
        self._last_successful_response_at = time.time()
        handler_ok = resp.get("ok") is not False and not resp.get("error")
        err = str(resp.get("error") or "")
        tb = str(resp.get("traceback") or "")
        if handler_ok:
            self._last_request_error = ""
            self._last_request_traceback = ""
        else:
            self._last_request_error = err or "diag handler returned ok=false"
            self._last_request_traceback = tb
        if self._on_request_outcome is not None:
            try:
                self._on_request_outcome(cmd, handler_ok, self._last_request_error, self._last_request_traceback)
            except Exception:
                logger.exception("diag on_request_outcome callback failed")

    def _copy_snapshot(self) -> dict[str, Any]:
        return dict(self._get_snapshot())

    def _enrich_readonly(self, snap: dict[str, Any]) -> dict[str, Any]:
        telem_ok = float(snap.get("telemetry_age_ms", 99999.0)) < 2000.0
        obs_ok = bool(snap.get("observation_complete"))
        if not obs_ok:
            obs = snap.get("last_observation") or []
            ts = snap.get("telemetry_snapshot") or {}
            obs_ok = len(obs) > 0 and bool(ts)
        snap["telemetry_recent"] = telem_ok
        snap["observation_complete"] = obs_ok
        snap["transition_server_connected"] = bool(
            snap.get("transition_server_connected") or snap.get("transition_server_ok")
        )
        return snap

    def _dispatch(self, msg: dict[str, Any]) -> dict[str, Any]:
        cmd = str(msg.get("cmd", "status"))
        if cmd == "ping":
            if self._on_ping is not None:
                resp = dict(self._on_ping())
            else:
                ident = self._identity_fields()
                resp = {
                    "ok": True,
                    "cmd": "ping",
                    "worker_id": ident.get("worker_id", self.worker_id),
                    "collector_pid": ident.get("collector_pid"),
                    "boot_id": ident.get("boot_id", ""),
                    "diag_port": self.port,
                    "ts": time.time(),
                }
            resp.setdefault("ok", True)
            resp.setdefault("cmd", "ping")
            return resp
        if cmd in ("status", "readonly_check"):
            snap = self._copy_snapshot()
            if cmd == "readonly_check":
                snap = self._enrich_readonly(snap)
            out = dict(snap)
            out["ok"] = True
            out["cmd"] = cmd
            return out
        if cmd == "safe_action_test":
            with self._lock:
                resp = self._on_safe_action_test()
            if isinstance(resp, dict) and "cmd" not in resp:
                resp = dict(resp)
                resp["cmd"] = cmd
            return resp
        if cmd == "diagnostic_vjoy_pulse":
            if self._on_vjoy_pulse is None:
                return {"ok": False, "cmd": cmd, "error": "vjoy pulse not supported by this collector"}
            pulse_payload = {k: v for k, v in msg.items() if k not in ("cmd", "worker_id")}
            with self._lock:
                resp = self._on_vjoy_pulse(pulse_payload or None)
            if isinstance(resp, dict) and "cmd" not in resp:
                resp = dict(resp)
                resp["cmd"] = cmd
            return resp
        if cmd in ("diag_selftest_vjoy_write", "diag-selftest-vjoy-write"):
            if self._on_vjoy_selftest is None:
                return {"ok": False, "cmd": cmd, "error": "vjoy selftest not supported by this collector"}
            with self._lock:
                resp = self._on_vjoy_selftest()
            if isinstance(resp, dict) and "cmd" not in resp:
                resp = dict(resp)
                resp["cmd"] = "diag_selftest_vjoy_write"
            return resp
        if cmd == "neutralize":
            with self._lock:
                resp = self._on_neutralize()
            if isinstance(resp, dict) and "cmd" not in resp:
                resp = dict(resp)
                resp["cmd"] = cmd
            return resp
        if cmd == "load_candidate" and self._on_worker_control is not None:
            # PAS de lock : le handler est une simple écriture d'attributs (atomique
            # sous le GIL). Le lock est parfois tenu par le main loop (refresh_snapshot
            # / write_minimal_status), ce qui faisait expirer le load_candidate
            # (timeout intermittent "diagnostic timeout on port 2410X").
            resp = self._on_worker_control(cmd, msg.get("candidate"))
            if isinstance(resp, dict) and "cmd" not in resp:
                resp = dict(resp)
                resp["cmd"] = cmd
            return resp
        if cmd in ("pause", "resume", "reset", "evaluate", "calibration_enter", "calibration_exit") and self._on_worker_control is not None:
            with self._lock:
                resp = self._on_worker_control(cmd, msg.get("candidate"))
            if isinstance(resp, dict) and "cmd" not in resp:
                resp = dict(resp)
                resp["cmd"] = cmd
            return resp
        return {"ok": False, "cmd": cmd, "error": "unknown cmd: {0}".format(cmd)}

    def _read_one_request(self, conn: socket.socket) -> dict[str, Any]:
        buf = b""
        conn.settimeout(READ_CHUNK_TIMEOUT)
        deadline = time.monotonic() + CONN_TIMEOUT
        while b"\n" not in buf and time.monotonic() < deadline:
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
        if not buf.strip():
            raise ValueError("empty request")
        line = buf.split(b"\n", 1)[0]
        try:
            parsed = json.loads(line.decode("utf-8"))
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError("invalid JSON request: {0}".format(exc)) from exc
        if not isinstance(parsed, dict):
            raise ValueError("request must be a JSON object")
        return parsed

    def _handle_connection(self, conn: socket.socket) -> None:
        cmd = "status"
        request_id = "-"
        try:
            peer = conn.getpeername()
        except OSError:
            peer = "?"
        resp: dict[str, Any] | None = None
        try:
            conn.settimeout(READ_CHUNK_TIMEOUT)
            msg = self._read_one_request(conn)
            cmd = str(msg.get("cmd", "status"))
            request_id = str(msg.get("request_id") or "-")
            _diag_log(
                "collector diagnostic: request rid={0} cmd={1} peer={2}".format(
                    request_id, cmd, peer
                )
            )
            try:
                resp = self._dispatch(msg)
            except Exception as exc:
                _diag_log("collector diagnostic: handler failed rid={0} cmd={1}: {2}".format(
                    request_id, cmd, exc
                ))
                _diag_log(traceback.format_exc())
                resp = self._error_response(cmd, str(exc), exc)
        except Exception as exc:
            _diag_log("collector diagnostic: request failed rid={0}: {1}".format(request_id, exc))
            resp = self._error_response(cmd, str(exc), exc)

        if resp is None:
            resp = self._error_response(cmd, "no response built")
        if request_id != "-":
            resp.setdefault("request_id", request_id)

        try:
            payload_len = len(json.dumps(resp, default=str)) + 1
        except Exception:
            payload_len = -1

        try:
            if not _safe_send_json_line(conn, resp):
                self._last_handler_error = "send failed after building response"
                _diag_log(
                    "collector diagnostic: sendall FAILED rid={0} cmd={1} peer={2} bytes={3}".format(
                        request_id, cmd, peer, payload_len
                    )
                )
                fallback = self._error_response(cmd, "send failed")
                fallback.setdefault("request_id", request_id)
                _safe_send_json_line(conn, fallback)
                self._record_outcome(cmd, fallback)
            else:
                self._record_outcome(cmd, resp)
                _diag_log(
                    "collector diagnostic: sendall ok rid={0} cmd={1} peer={2} bytes={3}".format(
                        request_id, cmd, peer, payload_len
                    )
                )
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _fail(self, exc: Exception) -> None:
        self._thread_error = exc
        self._listener_failed.set()
        _diag_log(traceback.format_exc())
        if self._on_thread_fatal:
            try:
                self._on_thread_fatal(exc)
            except Exception:
                logger.exception("diag on_thread_fatal callback failed")

    def _serve(self) -> None:
        sock: socket.socket | None = None
        _diag_log("collector diagnostic: thread entered")
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            _diag_log(
                "collector diagnostic: binding host={0} port={1}".format(self.host, self.port)
            )
            bind_mode = configure_diag_listener_socket(sock)
            _diag_log("collector diagnostic: bind mode={0}".format(bind_mode))
            try:
                sock.bind((self.host, self.port))
            except OSError as exc:
                winerror = getattr(exc, "winerror", None)
                errno = exc.errno
                _diag_log(
                    "collector diagnostic: bind failed host={0} port={1} errno={2} winerror={3}: {4}".format(
                        self.host, self.port, errno, winerror, exc
                    )
                )
                raise OSError(
                    errno,
                    diag_port_in_use_message(self.worker_id, self.port, exc),
                ) from exc
            sock.listen(8)
            self._sock = sock
            _diag_log(
                "collector diagnostic: listening host={0} port={1}".format(self.host, self.port)
            )
            self._listener_ready.set()
            _diag_log("collector diagnostic: accept loop started")
            self._accept_loop_alive = True
            sock.settimeout(ACCEPT_TIMEOUT)
            while not self._stop.is_set():
                conn = None
                try:
                    conn, _addr = sock.accept()
                    self._last_accept_at = time.time()
                    _diag_log("collector diagnostic: client accepted")
                    self._handle_connection(conn)
                    conn = None
                except socket.timeout:
                    continue
                except Exception as exc:
                    self._last_handler_error = str(exc)
                    if conn is not None:
                        try:
                            _safe_send_json_line(
                                conn,
                                self._error_response("status", str(exc), exc),
                            )
                        finally:
                            try:
                                conn.close()
                            except OSError:
                                pass
                        conn = None
                finally:
                    if conn is not None:
                        try:
                            conn.close()
                        except OSError:
                            pass
        except Exception as exc:
            self._fail(exc)
        finally:
            self._accept_loop_alive = False
            self._socket_closed = True
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            if self._sock is sock:
                self._sock = None
            if not self._listener_ready.is_set():
                self._listener_failed.set()

    def runtime_health(self) -> dict[str, Any]:
        return {
            "diag_accept_loop_alive": bool(self._accept_loop_alive),
            "diag_last_accept_at": float(self._last_accept_at),
            "diag_last_error": str(self._last_handler_error or ""),
            "diag_socket_closed": bool(self._socket_closed),
            "diag_last_successful_response_at": float(self._last_successful_response_at),
            "diag_last_request_error": str(self._last_request_error or ""),
            "diag_last_request_traceback": str(self._last_request_traceback or ""),
        }

    def listener_ready(self) -> bool:
        return self._listener_ready.is_set()

    def wait_until_listening(self, timeout_sec: float = 5.0, poll_sec: float = 0.05) -> bool:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if self._listener_ready.is_set():
                return True
            if self._listener_failed.is_set() or self._thread_error is not None:
                return False
            if self._thread is not None and not self._thread.is_alive() and not self._listener_ready.is_set():
                if self._thread_error is None:
                    self._thread_error = RuntimeError("diagnostic thread died before listen()")
                    self._listener_failed.set()
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            wait_for = min(poll_sec, remaining)
            if self._listener_ready.wait(timeout=wait_for):
                return True
            if self._listener_failed.wait(timeout=0):
                return False
        return self._listener_ready.is_set()

    def thread_is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def thread_error(self) -> Exception | None:
        return self._thread_error

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._listener_ready.clear()
        self._listener_failed.clear()
        self._thread_error = None
        self._thread = threading.Thread(target=self._serve, daemon=True, name="diag-{0}".format(self.worker_id))
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._listener_failed.set()
        sock = self._sock
        self._sock = None
        if sock:
            try:
                sock.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=2.0)
