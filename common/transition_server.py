"""Local transition server for multi-worker SAC (C4C).

TCP newline-delimited JSON. Collectors push transitions; learner pulls batches.
"""
from __future__ import annotations

import collections
import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_TRANSITION_PORT = 24050
ENV_TRANSITION_PORT = "ACGYM_TRANSITION_PORT"
DEFAULT_HOST = "127.0.0.1"


def transition_server_port() -> int:
    return int(os.environ.get(ENV_TRANSITION_PORT, DEFAULT_TRANSITION_PORT))


def atomic_publish_checkpoint(source: Path, latest_path: Path) -> None:
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = latest_path.with_suffix(latest_path.suffix + ".tmp")
    tmp.write_bytes(source.read_bytes())
    os.replace(tmp, latest_path)


class TransitionServer:
    def __init__(self, host: str = DEFAULT_HOST, port: int | None = None, max_queue: int = 10000):
        self.host = host
        self.port = port if port is not None else transition_server_port()
        self.max_queue: collections.deque = collections.deque(maxlen=max_queue)
        self._lock = threading.Lock()
        self._heartbeats: dict[int, float] = {}
        self._stats: dict[int, dict[str, int]] = {}
        self._learner_connected = False
        self._learner_last_seen = 0.0
        # Async actor-learner RPC: workers post observations and long-poll for an
        # action; the learner pulls pending requests and posts responses. Routing
        # is per worker_id + request_id so no worker ever waits on another.
        self._action_cond = threading.Condition()
        self._action_requests: dict[int, dict[str, Any]] = {}
        self._action_responses: dict[int, dict[str, Any]] = {}
        self._actor_stats = {
            "requests_total": 0,
            "responses_total": 0,
            "timeouts_total": 0,
        }
        # async_local_actor = primary local actor mode; async_actor = RPC debug mode.
        self._control_mode = "async_local_actor"
        self._control_mode_ts = time.time()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._server_sock: socket.socket | None = None
        self.dropped = 0

    def _ensure_worker(self, worker_id: int) -> dict[str, int]:
        if worker_id not in self._stats:
            self._stats[worker_id] = {
                "transitions_received": 0,
                "heartbeats": 0,
            }
        return self._stats[worker_id]

    def push_transition(self, payload: dict[str, Any]) -> bool:
        with self._lock:
            if len(self.max_queue) >= self.max_queue.maxlen:
                self.dropped += 1
                return False
            self.max_queue.append(payload)
            wid = int(payload.get("worker_id", -1))
            st = self._ensure_worker(wid)
            st["transitions_received"] += 1
            return True

    def heartbeat(self, worker_id: int) -> None:
        with self._lock:
            self._heartbeats[worker_id] = time.time()
            self._ensure_worker(worker_id)["heartbeats"] += 1

    def learner_ping(self) -> None:
        with self._lock:
            self._learner_connected = True
            self._learner_last_seen = time.time()

    def pull_batch(self, max_n: int = 256) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        with self._lock:
            while self.max_queue and len(out) < max_n:
                out.append(self.max_queue.popleft())
        return out

    def set_control_mode(self, mode: str) -> None:
        with self._action_cond:
            self._control_mode = str(mode)
            self._control_mode_ts = time.time()

    def control_mode(self) -> str:
        with self._action_cond:
            return self._control_mode

    def request_action(
        self, worker_id: int, request_id: str, obs: Any, wait_ms: float
    ) -> dict[str, Any]:
        """Worker long-poll: register the request, then wait for the learner's
        response routed back by (worker_id, request_id). Returns ready=False on
        timeout so the caller can apply a safe action and count a timeout."""
        wait_s = max(0.0, min(float(wait_ms) / 1000.0, 2.0))
        deadline = time.monotonic() + wait_s
        with self._action_cond:
            self._action_requests[worker_id] = {
                "worker_id": int(worker_id),
                "request_id": str(request_id),
                "obs": obs,
                "ts": time.time(),
            }
            self._actor_stats["requests_total"] += 1
            self._action_cond.notify_all()
            while True:
                resp = self._action_responses.get(worker_id)
                if resp is not None and resp.get("request_id") == request_id:
                    self._action_responses.pop(worker_id, None)
                    out = {"ok": True, "ready": True}
                    out.update(resp)
                    return out
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._actor_stats["timeouts_total"] += 1
                    return {"ok": True, "ready": False}
                self._action_cond.wait(remaining)

    def pull_action_requests(self, max_n: int = 64) -> list[dict[str, Any]]:
        with self._action_cond:
            reqs = list(self._action_requests.values())[:max_n]
            for r in reqs:
                self._action_requests.pop(int(r["worker_id"]), None)
            return reqs

    def put_action_responses(self, responses: list[dict[str, Any]]) -> int:
        n = 0
        with self._action_cond:
            for r in responses:
                wid = int(r["worker_id"])
                self._action_responses[wid] = r
                self._actor_stats["responses_total"] += 1
                n += 1
            self._action_cond.notify_all()
        return n

    def actor_stats(self) -> dict[str, Any]:
        with self._action_cond:
            recent = [int(r["worker_id"]) for r in self._action_requests.values()]
            return {
                "control_mode": self._control_mode,
                "actor_requests_total": int(self._actor_stats["requests_total"]),
                "actor_responses_total": int(self._actor_stats["responses_total"]),
                "actor_timeouts_total": int(self._actor_stats["timeouts_total"]),
                "actor_pending_requests": sorted(recent),
            }

    def stats_snapshot(self) -> dict[str, Any]:
        with self._lock:
            workers = {str(k): v for k, v in self._stats.items()}
            heartbeats = {str(k): v for k, v in self._heartbeats.items()}
            snap = {
                "queue_size": len(self.max_queue),
                "dropped": self.dropped,
                "heartbeats": heartbeats,
                "workers": workers,
                "learner_connected": self._learner_connected,
                "learner_last_seen": self._learner_last_seen,
            }
        snap.update(self.actor_stats())
        return snap

    def _handle_line(self, line: str) -> dict[str, Any]:
        msg = json.loads(line)
        cmd = msg.get("cmd")
        if cmd == "transition":
            ok = self.push_transition(msg.get("data", msg))
            return {"ok": ok}
        if cmd == "heartbeat":
            self.heartbeat(int(msg["worker_id"]))
            return {"ok": True}
        if cmd == "pull_batch":
            self.learner_ping()
            batch = self.pull_batch(int(msg.get("max_n", 256)))
            return {"ok": True, "transitions": batch, "count": len(batch)}
        if cmd == "stats":
            return {"ok": True, "stats": self.stats_snapshot()}
        if cmd == "health":
            return {"ok": True, "role": "transition_server"}
        if cmd == "learner_ping":
            self.learner_ping()
            return {"ok": True}
        if cmd == "request_action":
            return self.request_action(
                int(msg["worker_id"]),
                str(msg["request_id"]),
                msg.get("obs"),
                float(msg.get("wait_ms", 250.0)),
            )
        if cmd == "pull_action_requests":
            self.learner_ping()
            return {
                "ok": True,
                "requests": self.pull_action_requests(int(msg.get("max_n", 64))),
            }
        if cmd == "put_action_responses":
            n = self.put_action_responses(list(msg.get("responses", [])))
            return {"ok": True, "count": n}
        if cmd == "set_control_mode":
            self.set_control_mode(str(msg["mode"]))
            return {"ok": True, "control_mode": str(msg["mode"])}
        if cmd == "shutdown":
            self._stop.set()
            return {"ok": True}
        return {"ok": False, "error": "unknown cmd"}

    def _client_loop(self, conn: socket.socket) -> None:
        buf = b""
        try:
            while not self._stop.is_set():
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    resp = self._handle_line(line.decode("utf-8"))
                    conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
        finally:
            conn.close()

    def _serve(self) -> None:
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind((self.host, self.port))
        self._server_sock.listen(8)
        self._server_sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _addr = self._server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                raise
            threading.Thread(target=self._client_loop, args=(conn,), daemon=True).start()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, daemon=True, name="transition-server")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        sock = self._server_sock
        self._server_sock = None
        if sock:
            try:
                sock.close()
            except OSError:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)


class TransitionClient:
    _MAX_RETRIES = 3

    def __init__(self, host: str = DEFAULT_HOST, port: int | None = None, timeout: float = 5.0):
        self.host = host
        self.port = port if port is not None else transition_server_port()
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = b""
        self._lock = threading.Lock()

    def _connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except OSError:
            pass
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.connect((self.host, self.port))
        return sock

    def _drop(self) -> None:
        sock = self._sock
        self._sock = None
        self._buf = b""
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def close(self) -> None:
        with self._lock:
            self._drop()

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = (json.dumps(payload) + "\n").encode("utf-8")
        with self._lock:
            last_exc: BaseException | None = None
            for attempt in range(self._MAX_RETRIES):
                try:
                    if self._sock is None:
                        self._sock = self._connect()
                        self._buf = b""
                    self._sock.sendall(data)
                    while b"\n" not in self._buf:
                        chunk = self._sock.recv(65536)
                        if not chunk:
                            raise ConnectionResetError("server closed connection")
                        self._buf += chunk
                    line, self._buf = self._buf.split(b"\n", 1)
                    return json.loads(line.decode("utf-8"))
                except OSError as exc:
                    last_exc = exc
                    self._drop()
                    if attempt + 1 < self._MAX_RETRIES:
                        time.sleep(0.1 * (attempt + 1))
                        continue
                    raise
            raise last_exc  # type: ignore[misc]

    def push_transition(self, data: dict[str, Any]) -> bool:
        resp = self._request({"cmd": "transition", "data": data})
        return bool(resp.get("ok"))

    def heartbeat(self, worker_id: int) -> None:
        self._request({"cmd": "heartbeat", "worker_id": worker_id})

    def pull_batch(self, max_n: int = 256) -> list[dict[str, Any]]:
        resp = self._request({"cmd": "pull_batch", "max_n": max_n})
        return list(resp.get("transitions", []))

    def stats(self) -> dict[str, Any]:
        resp = self._request({"cmd": "stats"})
        return dict(resp.get("stats", {}))

    def health(self) -> dict[str, Any]:
        return self._request({"cmd": "health"})

    def learner_ping(self) -> None:
        self._request({"cmd": "learner_ping"})

    def request_action(
        self, worker_id: int, obs: Any, request_id: str, wait_ms: float = 250.0
    ) -> dict[str, Any]:
        """Worker side: ask the central learner for an action (long-poll).

        Returns the server response dict; ready=True with an "action" when the
        learner answered, ready=False on timeout.
        """
        return self._request(
            {
                "cmd": "request_action",
                "worker_id": int(worker_id),
                "request_id": str(request_id),
                "obs": obs,
                "wait_ms": float(wait_ms),
            }
        )

    def pull_action_requests(self, max_n: int = 64) -> list[dict[str, Any]]:
        resp = self._request({"cmd": "pull_action_requests", "max_n": max_n})
        return list(resp.get("requests", []))

    def put_action_responses(self, responses: list[dict[str, Any]]) -> int:
        resp = self._request({"cmd": "put_action_responses", "responses": responses})
        return int(resp.get("count", 0))

    def set_control_mode(self, mode: str) -> dict[str, Any]:
        return self._request({"cmd": "set_control_mode", "mode": str(mode)})

    def shutdown(self) -> None:
        try:
            self._request({"cmd": "shutdown"})
        except OSError:
            pass


class TransitionServerStub:
    """Backward-compatible alias."""

    def __init__(self, host: str = DEFAULT_HOST, port: int | None = None):
        self._server = TransitionServer(host=host, port=port)

    def describe(self) -> str:
        return "transition-server @ {0}:{1}".format(self._server.host, self._server.port)
