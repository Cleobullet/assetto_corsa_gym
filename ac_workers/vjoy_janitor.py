"""Client + shared protocol for the privileged vJoy janitor.

A stuck vJoy PDO handle can only be cleared by a PnP disable/enable of the vJoy
driver, which requires admin. To recover WITHOUT a UAC prompt every time, a
long-running janitor runs as SYSTEM via a Windows Scheduled Task (installed once)
and watches an IPC directory. The Control Center (unprivileged) drops a request
file; the janitor performs a global vJoy driver bounce + acquire-verify and writes
a result file.

Because the vJoy raw PDO is shared by all virtual devices, a reset is GLOBAL (it
bounces all 6 at once) -- callers must only request it when no healthy worker
currently owns a device.

This module is the unprivileged client side. It degrades gracefully: if the
janitor is not installed/alive, ``request_vjoy_reset`` returns ok=False with
reason "janitor_unavailable" so callers can fall back to the manual reset.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
JANITOR_DIR = REPO / "ac_workers" / ".vjoy_janitor"
HEARTBEAT_PATH = JANITOR_DIR / "heartbeat.json"
REQUEST_PATH = JANITOR_DIR / "request.json"
RESULT_PATH = JANITOR_DIR / "result.json"

PROTOCOL_VERSION = 1
HEARTBEAT_STALE_SEC = 15.0
DEFAULT_RESET_TIMEOUT_SEC = 60.0


def janitor_dir() -> Path:
    return JANITOR_DIR


def _read_json(path: Path) -> dict | None:
    try:
        if not path.is_file():
            return None
        # Janitor service (PowerShell) writes UTF-8 with BOM; collectors use plain utf-8.
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(str(tmp), str(path))


def heartbeat_age_sec() -> float | None:
    hb = _read_json(HEARTBEAT_PATH)
    if not hb:
        return None
    try:
        return max(0.0, time.time() - float(hb.get("ts", 0.0)))
    except (TypeError, ValueError):
        return None


def janitor_alive(max_age_sec: float = HEARTBEAT_STALE_SEC) -> bool:
    age = heartbeat_age_sec()
    return age is not None and age <= max_age_sec


def _new_request_id() -> str:
    return "{0}-{1}".format(int(time.time() * 1000), os.getpid())


def request_vjoy_reset(
    devices: list[int] | None = None,
    *,
    timeout_sec: float = DEFAULT_RESET_TIMEOUT_SEC,
    poll_sec: float = 0.5,
    require_alive: bool = True,
) -> dict:
    """Ask the privileged janitor for a global vJoy driver reset + verify.

    Returns a dict with at least ``ok`` (bool) and ``reason`` (str). When the
    janitor is unavailable, ok=False and reason="janitor_unavailable".
    """
    if devices is None:
        devices = list(range(1, 7))
    if require_alive and not janitor_alive():
        return {"ok": False, "reason": "janitor_unavailable", "devices": devices}

    request_id = _new_request_id()
    payload = {
        "request_id": request_id,
        "version": PROTOCOL_VERSION,
        "devices": [int(d) for d in devices],
        "requested_at": time.time(),
        "requested_by_pid": os.getpid(),
    }
    try:
        _atomic_write(REQUEST_PATH, payload)
    except OSError as exc:
        return {"ok": False, "reason": "request_write_failed:{0}".format(exc), "devices": devices}

    deadline = time.monotonic() + max(1.0, timeout_sec)
    while time.monotonic() < deadline:
        result = _read_json(RESULT_PATH)
        if result and str(result.get("request_id")) == request_id:
            result.setdefault("ok", False)
            result.setdefault("reason", "")
            return result
        time.sleep(poll_sec)
    return {"ok": False, "reason": "janitor_timeout", "request_id": request_id, "devices": devices}


def write_heartbeat(pid: int | None = None) -> bool:
    """Used by the janitor service side (also handy for tests)."""
    payload = {
        "pid": int(pid if pid is not None else os.getpid()),
        "ts": time.time(),
        "version": PROTOCOL_VERSION,
    }
    try:
        _atomic_write(HEARTBEAT_PATH, payload)
        return True
    except OSError:
        return False


def read_pending_request() -> dict | None:
    """Janitor service side: read the current request file (if any)."""
    return _read_json(REQUEST_PATH)


def write_result(payload: dict) -> bool:
    """Janitor service side: publish a result for the last processed request."""
    body = dict(payload)
    body.setdefault("ts", time.time())
    body.setdefault("version", PROTOCOL_VERSION)
    try:
        _atomic_write(RESULT_PATH, body)
        return True
    except OSError:
        return False
