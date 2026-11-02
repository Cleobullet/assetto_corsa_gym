"""Graceful collector shutdown sentinel.

A hard ``TerminateProcess`` (what ``os.kill(pid, SIGTERM)`` does on Windows) skips
the collector's ``finally`` block, so the vJoy device it OWNED is never relinquished
and the driver leaks a stuck PDO handle -> the next run gets ``AcquireVJD failed
status=1 (FREE)``. To avoid that, the orchestrator drops a per-worker stop sentinel
and waits: the collector notices it, breaks out of its loop, and exits cleanly via
``finally`` (relinquishing vJoy). Only stragglers are then force-killed.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STATUS_DIR = REPO / "ac_workers" / "status"


def stop_sentinel_path(worker_id: int) -> Path:
    return STATUS_DIR / ("worker_{0}.stop".format(worker_id))


def request_collector_shutdown(worker_id: int) -> bool:
    """Ask the collector for worker_id to relinquish vJoy and exit gracefully."""
    try:
        STATUS_DIR.mkdir(parents=True, exist_ok=True)
        stop_sentinel_path(worker_id).write_text("stop", encoding="utf-8")
        return True
    except OSError:
        return False


def clear_collector_shutdown(worker_id: int) -> None:
    try:
        stop_sentinel_path(worker_id).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def collector_shutdown_requested(worker_id: int) -> bool:
    try:
        return stop_sentinel_path(worker_id).is_file()
    except OSError:
        return False
