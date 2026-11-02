"""Singleton lock and session registry for C4D Control Center (one server per repo)."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SERVER_MARKER = "ac_workers.control_center server"
DASH_MARKER = "ac_workers.control_center dashboard"
CONSOLE_MARKER = "ac_workers.control_center console"
SESSION_DIR = ".c4d_server"
SESSION_NAME = "session.json"


def repo_session_dir(repo_root: Path) -> Path:
    return Path(repo_root).resolve() / "ac_workers" / SESSION_DIR


def repo_session_file(repo_root: Path) -> Path:
    return repo_session_dir(repo_root) / SESSION_NAME


def mutex_name(repo_root: Path) -> str:
    import hashlib

    digest = hashlib.sha256(str(Path(repo_root).resolve()).encode("utf-8")).hexdigest()[:16]
    return "Global\\C4D-Server-" + digest


def generate_token() -> str:
    return uuid.uuid4().hex


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        from ac_workers.transition_server_process import is_process_alive

        return is_process_alive(pid)
    except Exception:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def process_command_line(pid: int) -> str:
    try:
        from ac_workers.transition_server_process import get_process_command_line

        return get_process_command_line(pid) or ""
    except Exception:
        return ""


def _norm(text: str) -> str:
    return text.replace("\\", "/").lower()


def _repo_in_cmd(repo_root: Path, cmd: str) -> bool:
    repo = _norm(str(Path(repo_root).resolve()))
    norm_cmd = _norm(cmd)
    return repo in norm_cmd


def _port_in_cmd(port: int, cmd: str) -> bool:
    needle = str(port)
    return ("--port {0}".format(needle) in cmd) or ("--port={0}".format(needle) in cmd)


def _token_in_cmd(token: str, cmd: str) -> bool:
    return ("--session-token {0}".format(token) in cmd) or ("--session-token={0}".format(token) in cmd)


def is_c4d_server_process(pid: int, repo_root: Path, port: int, token: str | None = None) -> bool:
    if not pid_alive(pid):
        return False
    cmd = process_command_line(pid)
    if SERVER_MARKER not in cmd:
        return False
    if not _repo_in_cmd(repo_root, cmd):
        return False
    if not _port_in_cmd(port, cmd):
        return False
    if token and not _token_in_cmd(token, cmd):
        return False
    return True


def is_c4d_client_process(pid: int, role: str, repo_root: Path) -> bool:
    if not pid_alive(pid):
        return False
    marker = "ac_workers.control_center {0}".format(role)
    cmd = process_command_line(pid)
    return marker in cmd and _repo_in_cmd(repo_root, cmd)


def iter_c4d_processes(repo_root: Path) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    if os.name != "nt":
        return found
    try:
        ps = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.CommandLine -like '*ac_workers.control_center*' } | "
            "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"
        )
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps],
            text=True,
            errors="replace",
            timeout=20,
        )
        repo_norm = _norm(str(Path(repo_root).resolve()))
        for line in out.splitlines():
            if not line.strip():
                continue
            pid_text, _, cmd = line.partition("\t")
            try:
                pid = int(pid_text.strip())
            except ValueError:
                continue
            if repo_norm in _norm(cmd):
                found.append((pid, cmd))
    except Exception:
        pass
    return found


def read_session(repo_root: Path) -> dict[str, Any] | None:
    path = repo_session_file(repo_root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def write_session(repo_root: Path, payload: dict[str, Any]) -> None:
    path = repo_session_file(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(path))


def clear_session(repo_root: Path) -> None:
    path = repo_session_file(repo_root)
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass


def clear_ready_flags() -> None:
    tmp = Path(os.environ.get("TEMP", os.environ.get("TMP", ".")))
    for path in tmp.glob("c4d_server_ready_*.flag"):
        try:
            path.unlink()
        except OSError:
            pass


def stop_pid(pid: int, *, verify_role: str | None, repo_root: Path) -> bool:
    if pid <= 0 or not pid_alive(pid):
        return False
    if verify_role == "server":
        if not is_c4d_server_process(pid, repo_root, port=0) and SERVER_MARKER not in process_command_line(pid):
            return False
    elif verify_role in ("dashboard", "console"):
        if not is_c4d_client_process(pid, verify_role, repo_root):
            return False
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                capture_output=True,
            )
        else:
            os.kill(pid, signal.SIGTERM)
    except (OSError, AttributeError):
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False, capture_output=True)
    return True


def stop_all_c4d_for_repo(repo_root: Path, port: int) -> list[int]:
    stopped: list[int] = []
    for pid, cmd in iter_c4d_processes(repo_root):
        if SERVER_MARKER in cmd or DASH_MARKER in cmd or CONSOLE_MARKER in cmd:
            if stop_pid(pid, verify_role=None, repo_root=repo_root):
                stopped.append(pid)
    session = read_session(repo_root)
    if session:
        for key in ("server_pid", "dashboard_pid", "console_pid", "launcher_pid"):
            spid = int(session.get(key) or 0)
            if spid > 0 and pid_alive(spid) and spid not in stopped:
                if stop_pid(spid, verify_role=None, repo_root=repo_root):
                    stopped.append(spid)
    clear_session(repo_root)
    clear_ready_flags()
    try:
        from ac_workers.transition_server_process import get_tcp_listener_pids, wait_port_released

        for lp in get_tcp_listener_pids(port):
            cmd = process_command_line(lp)
            if SERVER_MARKER in cmd and _repo_in_cmd(repo_root, cmd):
                stop_pid(lp, verify_role="server", repo_root=repo_root)
                stopped.append(lp)
        wait_port_released(port, timeout_sec=3.0)
    except Exception:
        pass
    return stopped


def find_valid_server(repo_root: Path, host: str, port: int) -> dict[str, Any] | None:
    session = read_session(repo_root) or {}
    token = str(session.get("token") or "")
    candidates: list[int] = []
    spid = int(session.get("server_pid") or 0)
    if spid > 0:
        candidates.append(spid)
    try:
        from ac_workers.transition_server_process import get_tcp_listener_pids

        for lp in get_tcp_listener_pids(port):
            if lp not in candidates:
                candidates.append(lp)
    except Exception:
        pass
    for pid in candidates:
        if not is_c4d_server_process(pid, repo_root, port, token or None):
            continue
        cmd = process_command_line(pid)
        stoken = token or ""
        if stoken and not _token_in_cmd(stoken, cmd):
            continue
        return {
            "server_pid": pid,
            "token": stoken or _extract_token(cmd),
            "host": host,
            "port": port,
            "session_file": str(repo_session_file(repo_root)),
        }
    return None


def _extract_token(cmd: str) -> str:
    idx = cmd.find("--session-token")
    if idx < 0:
        return ""
    tail = cmd[idx + len("--session-token") :].strip()
    if tail.startswith("="):
        return tail[1:].split()[0]
    return tail.split()[0] if tail else ""


def prepare_launch(repo_root: Path, host: str, port: int) -> dict[str, Any]:
    repo_root = Path(repo_root).resolve()
    clear_ready_flags()
    session_path = str(repo_session_file(repo_root))
    valid = find_valid_server(repo_root, host, port)
    if valid and valid.get("token"):
        payload = read_session(repo_root) or {}
        payload.update(valid)
        payload["action"] = "reuse"
        payload["session_file"] = session_path
        return payload

    stopped = stop_all_c4d_for_repo(repo_root, port)
    token = generate_token()
    payload = {
        "action": "start",
        "token": token,
        "repo_root": str(repo_root),
        "host": host,
        "port": port,
        "server_pid": 0,
        "launcher_pid": os.getpid(),
        "dashboard_pid": 0,
        "console_pid": 0,
        "started_at": _now_iso(),
        "session_file": session_path,
        "stopped_pids": stopped,
    }
    write_session(repo_root, payload)
    return payload


def update_session_fields(repo_root: Path, **fields: Any) -> None:
    payload = read_session(repo_root) or {}
    payload.update(fields)
    write_session(repo_root, payload)


class ServerRuntimeLock:
    """Named mutex held for the lifetime of the C4D server process."""

    def __init__(self, repo_root: Path, host: str, port: int, token: str) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.host = host
        self.port = port
        self.token = token
        self._handle: int | None = None

    def acquire(self) -> tuple[bool, str]:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.CreateMutexW(None, True, mutex_name(self.repo_root))
            if not handle:
                return False, "mutex create failed"
            already = ctypes.get_last_error() == 183
            if already:
                existing = find_valid_server(self.repo_root, self.host, self.port)
                if existing:
                    kernel32.CloseHandle(handle)
                    return False, "server already running pid={0}".format(existing.get("server_pid"))
                # Stale/abandoned mutex (previous server dead). Keep the handle and
                # proceed as the new owner — refusing here makes the next launch
                # fail right after an emergency stop ("connection closed by remote
                # host") because the abandoned mutex blocks the restart.
            self._handle = int(handle)
        else:
            lock_path = repo_session_dir(self.repo_root) / "server.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                self._handle = fd
            except FileExistsError:
                existing = find_valid_server(self.repo_root, self.host, self.port)
                if existing:
                    return False, "server already running pid={0}".format(existing.get("server_pid"))
                return False, "stale lock file"
        update_session_fields(
            self.repo_root,
            token=self.token,
            host=self.host,
            port=self.port,
            server_pid=os.getpid(),
            started_at=_now_iso(),
        )
        return True, "ok"

    def release_cleanup(self) -> None:
        if os.name == "nt" and self._handle is not None:
            import ctypes

            ctypes.WinDLL("kernel32").CloseHandle(self._handle)
            self._handle = None
        else:
            lock_path = repo_session_dir(self.repo_root) / "server.lock"
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass
        clear_session(self.repo_root)
        clear_ready_flags()


def write_ready_file(path: Path, host: str, port: int, token: str, server_pid: int) -> None:
    payload = {
        "host": host,
        "port": port,
        "token": token,
        "server_pid": server_pid,
        "ready_at": _now_iso(),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def validate_client_token(expected: str, offered: str) -> bool:
    return bool(expected) and expected == offered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="c4d_server_lock")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_prepare = sub.add_parser("prepare")
    p_prepare.add_argument("--repo-root", type=Path, required=True)
    p_prepare.add_argument("--host", default="127.0.0.1")
    p_prepare.add_argument("--port", type=int, default=24700)

    p_update = sub.add_parser("update-clients")
    p_update.add_argument("--repo-root", type=Path, required=True)
    p_update.add_argument("--launcher-pid", type=int, default=0)
    p_update.add_argument("--dashboard-pid", type=int, default=0)
    p_update.add_argument("--console-pid", type=int, default=0)

    p_stop = sub.add_parser("stop")
    p_stop.add_argument("--repo-root", type=Path, required=True)
    p_stop.add_argument("--port", type=int, default=24700)

    args = parser.parse_args(argv)
    repo = Path(args.repo_root).resolve()

    if args.cmd == "prepare":
        print(json.dumps(prepare_launch(repo, args.host, args.port)))
        return 0
    if args.cmd == "update-clients":
        fields = {}
        if args.launcher_pid:
            fields["launcher_pid"] = args.launcher_pid
        if args.dashboard_pid:
            fields["dashboard_pid"] = args.dashboard_pid
        if args.console_pid:
            fields["console_pid"] = args.console_pid
        if fields:
            update_session_fields(repo, **fields)
        print(json.dumps(read_session(repo) or {}))
        return 0
    if args.cmd == "stop":
        stopped = stop_all_c4d_for_repo(repo, args.port)
        print(json.dumps({"stopped_pids": stopped}))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
