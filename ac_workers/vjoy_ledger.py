"""Crash-safe vJoy ownership ledger.

The manifest is updated best-effort and can go stale when a collector dies hard.
The ledger is the authoritative, crash-safe record of "which process currently
OWNS which vJoy device": each collector writes its entry the instant it acquires a
device and clears it on a clean exit. If the process dies without clearing, the
entry survives on disk so the reaper can tell a leaked device (dead owner) from a
healthy one (live owner) and recover only what is actually stuck.

Pure stdlib so the collector can import it cheaply during early startup.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LEDGER_DIR = REPO / "ac_workers" / ".vjoy_ledger"


def ledger_dir() -> Path:
    return LEDGER_DIR


def _entry_path(device_id: int) -> Path:
    return LEDGER_DIR / "device_{0}.json".format(int(device_id))


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(str(tmp), str(path))


def record_owner(
    device_id: int,
    *,
    pid: int | None = None,
    session_id: str | None = None,
    worker_id: int | None = None,
) -> bool:
    """Record this process as the current owner of ``device_id``."""
    payload = {
        "device_id": int(device_id),
        "pid": int(pid if pid is not None else os.getpid()),
        "session_id": session_id or os.environ.get("AC_AI_SESSION_ID", ""),
        "worker_id": int(worker_id) if worker_id is not None else None,
        "ts": time.time(),
    }
    try:
        _atomic_write(_entry_path(device_id), payload)
        return True
    except OSError:
        return False


def read_owner(device_id: int) -> dict | None:
    path = _entry_path(device_id)
    try:
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def clear_owner(device_id: int, *, pid: int | None = None, force: bool = False) -> bool:
    """Remove the ledger entry for ``device_id``.

    By default only clears the entry if it belongs to ``pid`` (or the current
    process), so a collector cannot wipe another process's still-valid ownership.
    Pass ``force=True`` for janitor/reaper-driven cleanup.
    """
    entry = read_owner(device_id)
    if entry is not None and not force:
        owner_pid = int(entry.get("pid", -1))
        check_pid = int(pid if pid is not None else os.getpid())
        if owner_pid != check_pid:
            return False
    try:
        _entry_path(device_id).unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def clear_all_owners() -> None:
    try:
        if not LEDGER_DIR.is_dir():
            return
        for path in LEDGER_DIR.glob("device_*.json"):
            try:
                path.unlink()
            except OSError:
                pass
    except OSError:
        pass


def read_all() -> list[dict]:
    entries: list[dict] = []
    try:
        if not LEDGER_DIR.is_dir():
            return entries
        for path in sorted(LEDGER_DIR.glob("device_*.json")):
            try:
                entries.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
    except OSError:
        return entries
    return entries


def _acquire_lock_path(device_id: int) -> Path:
    return LEDGER_DIR / "device_{0}.acquire.lock".format(int(device_id))


@contextlib.contextmanager
def device_acquire_lock(device_id: int, *, timeout_sec: float = 30.0):
    """Cross-process mutex while AcquireVJD / reacquire runs for one device."""
    path = _acquire_lock_path(device_id)
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.5, float(timeout_sec))
    fd: int | None = None
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "vJoy device {0} acquire lock timeout after {1:.1f}s".format(
                        int(device_id), float(timeout_sec)
                    )
                )
            time.sleep(0.05)
    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            path.unlink()
        except OSError:
            pass
