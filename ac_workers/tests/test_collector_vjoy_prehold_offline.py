"""Offline tests for wait_vjoy_prehold (collector before AC)."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def test_wait_vjoy_prehold_succeeds_when_device_acquired(tmp_path):
    from ac_workers import collector_ready as cr

    status_dir = tmp_path / "status"
    status_dir.mkdir()
    path = status_dir / "worker_2.json"

    def write(phase: str, acquired: bool) -> None:
        path.write_text(
            json.dumps(
                {
                    "session_id": "sess1",
                    "collector_pid": 999,
                    "phase": phase,
                    "vjoy_device_acquired": acquired,
                    "last_error": "",
                }
            ),
            encoding="utf-8",
        )

    write("vjoy_prehold_begin", False)
    repo = tmp_path

    def fake_read(wid, *, repo_root=None):
        return json.loads(path.read_text(encoding="utf-8"))

    with mock.patch("ac_workers.collector_status.read_worker_status", side_effect=fake_read):
        with mock.patch.object(cr.time, "sleep", lambda _s: write("vjoy_prehold_active", True)):
            ok, err = cr.wait_vjoy_prehold(2, "sess1", 999, timeout_sec=5, repo_root=repo)
    assert ok is True
    assert err == ""


def test_wait_vjoy_prehold_fails_on_startup_failed(tmp_path):
    from ac_workers import collector_ready as cr

    snap = {
        "session_id": "sess1",
        "collector_pid": 999,
        "phase": "vjoy_startup_failed",
        "vjoy_device_acquired": False,
        "last_error": "AcquireVJD failed status=1 (FREE)",
    }
    with mock.patch("ac_workers.collector_status.read_worker_status", return_value=snap):
        ok, err = cr.wait_vjoy_prehold(0, "sess1", 999, timeout_sec=1, repo_root=tmp_path)
    assert ok is False
    assert "AcquireVJD" in err


def test_wait_vjoy_prehold_uses_diag_when_status_file_unreadable(tmp_path):
    """Root-cause regression: status file empty but diag channel is authoritative.

    Resolve-CollectorRealPid resolves the pid via the diagnostic channel even
    when the status file is unreadable. wait_vjoy_prehold must trust the same
    live signal so it can never disagree with the pid resolution before it.
    """
    from ac_workers import collector_ready as cr

    diag_snap = {
        "session_id": "sessX",
        "collector_pid": 4242,
        "phase": "environment_construct_begin",
        "vjoy_device_acquired": True,
        "last_error": "",
    }

    class FakeDiagClient:
        def __init__(self, worker_id, timeout=2.0):
            pass

        def request(self, cmd):
            return diag_snap

    with mock.patch("ac_workers.collector_diag.CollectorDiagClient", FakeDiagClient):
        # status file deliberately empty to reproduce the original failure mode
        with mock.patch(
            "ac_workers.collector_status.read_worker_status", return_value={}
        ):
            ok, err = cr.wait_vjoy_prehold(3, "sessX", 4242, timeout_sec=2, repo_root=tmp_path)
    assert ok is True
    assert err == ""


def test_wait_vjoy_prehold_matches_on_pid_when_session_drifts(tmp_path):
    """A pid match alone is enough even if the session stamp differs."""
    from ac_workers import collector_ready as cr

    class DeadDiagClient:
        def __init__(self, worker_id, timeout=2.0):
            from ac_workers.collector_diag import DiagRequestError

            self._exc = DiagRequestError("no diag")

        def request(self, cmd):
            raise self._exc

    snap = {
        "session_id": "OTHER_SESSION",
        "collector_pid": 777,
        "phase": "vjoy_prehold_active",
        "vjoy_device_acquired": True,
        "last_error": "",
    }
    with mock.patch("ac_workers.collector_diag.CollectorDiagClient", DeadDiagClient):
        with mock.patch(
            "ac_workers.collector_status.read_worker_status", return_value=snap
        ):
            ok, err = cr.wait_vjoy_prehold(1, "WANTED_SESSION", 777, timeout_sec=2, repo_root=tmp_path)
    assert ok is True
    assert err == ""
