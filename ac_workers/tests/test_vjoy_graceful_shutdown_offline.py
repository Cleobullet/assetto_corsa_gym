"""Offline tests for the graceful collector shutdown sentinel.

These cover the fix for the recurring ``AcquireVJD failed status=1 (FREE)``: instead
of force-killing collectors (which skips their finally and leaks a stuck vJoy PDO
handle), the orchestrator drops a per-worker sentinel and waits for a clean exit.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "assetto_corsa_gym"))


def test_sentinel_round_trip(tmp_path):
    import ac_workers.collector_shutdown as cs

    with mock.patch.object(cs, "STATUS_DIR", tmp_path):
        assert cs.collector_shutdown_requested(3) is False
        assert cs.request_collector_shutdown(3) is True
        assert cs.stop_sentinel_path(3) == tmp_path / "worker_3.stop"
        assert cs.collector_shutdown_requested(3) is True
        cs.clear_collector_shutdown(3)
        assert cs.collector_shutdown_requested(3) is False
        # Clearing a missing sentinel must not raise.
        cs.clear_collector_shutdown(3)


def test_graceful_stop_waits_then_reports_exited(tmp_path):
    import ac_workers.collector_shutdown as cs
    import ac_workers.emergency_stop as es

    manifest = {
        "workers": [
            {"workerId": 0, "collectorPid": 111},
            {"workerId": 1, "collectorPid": 222},
        ]
    }

    # PID 111 exits gracefully (after the sentinel), 222 never does.
    alive = {111: True, 222: True}

    def fake_pid_alive(pid: int) -> bool:
        if pid == 111 and cs.collector_shutdown_requested(0):
            # Simulate the collector noticing the sentinel and exiting.
            alive[111] = False
        return alive.get(int(pid), False)

    with mock.patch.object(cs, "STATUS_DIR", tmp_path), \
            mock.patch("ac_workers.collector_pid.scan_collector_pids", return_value=[]), \
            mock.patch.object(es, "_pid_alive", side_effect=fake_pid_alive):
        exited = es._graceful_stop_collectors(manifest, grace_sec=2.0)

    assert 111 in exited
    assert 222 not in exited
    # Sentinels are always cleaned up afterwards (no stale files left behind).
    assert cs.collector_shutdown_requested(0) is False
    assert cs.collector_shutdown_requested(1) is False


def test_graceful_stop_no_targets_is_noop(tmp_path):
    import ac_workers.collector_shutdown as cs
    import ac_workers.emergency_stop as es

    with mock.patch.object(cs, "STATUS_DIR", tmp_path), \
            mock.patch("ac_workers.collector_pid.scan_collector_pids", return_value=[]), \
            mock.patch.object(es, "_pid_alive", return_value=False):
        exited = es._graceful_stop_collectors({"workers": []}, grace_sec=0.5)

    assert exited == []
