"""Resume sentinel for collectors when the diagnostic TCP channel is flaky.

``learner start`` must flip collectors from idle (neutral, no transitions) to
running. The primary path is a ``resume`` command on the diagnostic port; when
that fails (Windows socket EINVAL, lock contention, etc.) the orchestrator can
drop ``status/worker_N.resume`` and the collector heartbeat thread picks it up.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STATUS_DIR = REPO / "ac_workers" / "status"


def resume_sentinel_path(worker_id: int) -> Path:
    return STATUS_DIR / ("worker_{0}.resume".format(int(worker_id)))


def request_collector_resume(worker_id: int) -> bool:
    try:
        STATUS_DIR.mkdir(parents=True, exist_ok=True)
        resume_sentinel_path(worker_id).write_text("resume", encoding="utf-8")
        return True
    except OSError:
        return False


def clear_collector_resume(worker_id: int) -> None:
    try:
        resume_sentinel_path(worker_id).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def collector_resume_requested(worker_id: int) -> bool:
    try:
        return resume_sentinel_path(worker_id).is_file()
    except OSError:
        return False
