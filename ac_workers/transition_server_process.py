"""Detect, ping and stop transition server processes (Windows-safe, -m module aware)."""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

TRANSITION_SERVER_MODULE_MARKERS = (
    "-m ac_workers.transition_server_main",
    "-m common.transition_server",
    "transition_server_main.py",
)

_CMDLINE_CACHE: dict[int, tuple[float, str | None]] = {}
_CMDLINE_CACHE_TTL_SEC = 15.0


def clear_process_command_line_cache() -> None:
    _CMDLINE_CACHE.clear()


def invalidate_process_command_line_cache(pid: int) -> None:
    if pid > 0:
        _CMDLINE_CACHE.pop(pid, None)


def is_transition_server_commandline(commandline: str | None) -> bool:
    if not commandline:
        return False
    return any(marker in commandline for marker in TRANSITION_SERVER_MODULE_MARKERS)


def get_process_command_line(pid: int, *, use_cache: bool = True) -> str | None:
    if pid <= 0:
        return None
    now = time.time()
    if use_cache:
        cached = _CMDLINE_CACHE.get(pid)
        if cached is not None and (now - cached[0]) < _CMDLINE_CACHE_TTL_SEC:
            return cached[1]
    result: str | None = None
    if os.name == "nt":
        try:
            out = subprocess.check_output(
                [
                    "wmic",
                    "process",
                    "where",
                    "ProcessId={0}".format(pid),
                    "get",
                    "CommandLine",
                    "/format:value",
                ],
                text=True,
                errors="replace",
                timeout=5,
            )
            for line in out.splitlines():
                if line.startswith("CommandLine="):
                    val = line.split("=", 1)[1].strip()
                    result = val if val else None
                    break
        except Exception:
            pass
        if result is None:
            try:
                ps = (
                    "(Get-CimInstance Win32_Process -Filter \"ProcessId={0}\").CommandLine"
                ).format(pid)
                out = subprocess.check_output(
                    ["powershell", "-NoProfile", "-Command", ps],
                    text=True,
                    errors="replace",
                    timeout=5,
                )
                val = out.strip()
                result = val if val else None
            except Exception:
                pass
    if use_cache:
        _CMDLINE_CACHE[pid] = (now, result)
    return result


def is_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def get_tcp_listener_pids(port: int) -> list[int]:
    pids: list[int] = []
    if os.name != "nt":
        return pids
    try:
        out = subprocess.check_output(["netstat", "-ano"], text=True, errors="replace", timeout=10)
    except Exception:
        return pids
    token = ":{0} ".format(port)
    for line in out.splitlines():
        if "LISTENING" not in line.upper():
            continue
        if token not in line and not line.rstrip().endswith(":{0}".format(port)):
            continue
        parts = line.split()
        if not parts:
            continue
        try:
            pids.append(int(parts[-1]))
        except ValueError:
            continue
    return list(dict.fromkeys(pids))


def is_port_listening(port: int) -> bool:
    return len(get_tcp_listener_pids(port)) > 0


def wait_port_released(port: int, timeout_sec: float = 5.0) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if not is_port_listening(port):
            return True
        time.sleep(0.2)
    return not is_port_listening(port)


def ping_transition_server(port: int | None = None, timeout: float = 1.0) -> bool:
    sys.path.insert(0, str(REPO))
    from common.transition_server import TransitionClient, transition_server_port

    p = port if port is not None else transition_server_port()
    try:
        resp = TransitionClient(port=p, timeout=timeout).health()
        return bool(resp.get("ok")) and resp.get("role") == "transition_server"
    except (OSError, socket.timeout, TimeoutError, ConnectionError, json.JSONDecodeError, ValueError):
        return False


def stop_process_pid(pid: int, verify_commandline: bool = True) -> bool:
    if pid <= 0:
        return False
    cmd = get_process_command_line(pid)
    if verify_commandline and not is_transition_server_commandline(cmd):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, AttributeError):
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], check=False, capture_output=True)
    return True


def detect_transition_server_pid(
    manifest: dict | None = None,
    port: int | None = None,
) -> dict:
    """Priority: manifest PID, port listener; validate CommandLine."""
    sys.path.insert(0, str(REPO))
    from common.transition_server import transition_server_port

    port = port if port is not None else transition_server_port()
    manifest = manifest or {}
    result = {
        "port": port,
        "pid": None,
        "commandline": None,
        "source": None,
        "recognized": False,
        "listener_pids": get_tcp_listener_pids(port),
    }

    manifest_pid = manifest.get("transitionServerPid")
    if manifest_pid and is_process_alive(int(manifest_pid)):
        cmd = get_process_command_line(int(manifest_pid))
        recognized = is_transition_server_commandline(cmd)
        result.update(
            {
                "pid": int(manifest_pid),
                "commandline": cmd,
                "source": "manifest",
                "recognized": recognized,
            }
        )
        if recognized:
            return result

    for lp in result["listener_pids"]:
        cmd = get_process_command_line(lp)
        if is_transition_server_commandline(cmd):
            result.update({"pid": lp, "commandline": cmd, "source": "port", "recognized": True})
            return result

    if result["listener_pids"]:
        lp = result["listener_pids"][0]
        result.update(
            {
                "pid": lp,
                "commandline": get_process_command_line(lp),
                "source": "port_unknown",
                "recognized": False,
            }
        )
    return result


def stop_transition_server(
    manifest: dict | None = None,
    port: int | None = None,
    wait_sec: float = 5.0,
) -> dict:
    sys.path.insert(0, str(REPO))
    from common.transition_server import transition_server_port

    port = port if port is not None else transition_server_port()
    report = {
        "detected_pid": None,
        "detected_source": None,
        "commandline": None,
        "stopped": False,
        "port_released": False,
        "skipped_unknown": False,
        "port": port,
    }
    deadline = time.time() + wait_sec
    announced = False
    while time.time() < deadline:
        if not is_port_listening(port):
            report["port_released"] = True
            break
        info = detect_transition_server_pid(manifest, port)
        pid = info.get("pid")
        if not pid:
            time.sleep(0.2)
            continue
        if not announced:
            report["detected_pid"] = pid
            report["detected_source"] = info.get("source")
            report["commandline"] = info.get("commandline")
            print("transition server detected pid={0}".format(pid))
            announced = True

        if info.get("recognized"):
            if stop_process_pid(int(pid), verify_commandline=False):
                report["stopped"] = True
                print("transition server stopped")
        else:
            report["skipped_unknown"] = True
        time.sleep(0.25)

    if not report["port_released"]:
        report["port_released"] = not is_port_listening(port)
    if report["port_released"]:
        print("transition server port {0} released".format(port))
    return report
