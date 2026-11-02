"""Offline tests for mono-worker test-start / test-health commands."""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def test_format_test_health_fields():
    from ac_workers.control_center import ControlCenter, WORKER_OP_OFFLINE

    center = ControlCenter(REPO)
    with mock.patch.object(center, "_worker_status", return_value={
        "worker_id": 0,
        "phase": "idle",
        "collector_pid": 1234,
        "boot_id": "abc123",
        "telemetry_age_ms": 50.0,
        "vjoy_device": 1,
        "vjoy_device_acquired": True,
        "initial_session_attached": True,
        "policy_ready": True,
    }), mock.patch.object(center, "_manifest_worker", return_value={"acPid": 999}), mock.patch(
        "ac_workers.control_center._diag_probe_blocking",
        return_value=(True, "ok", {"boot_id": "abc123", "collector_pid": 1234}, 0.0),
    ), mock.patch("ac_workers.controls_snapshot.load_meta", return_value={
        "joy_index_written": 2,
        "detected_joy_index": 2,
        "verdict": "OK",
    }), mock.patch("ac_workers.control_center.pid_alive", return_value=True):
        center._worker_op_state[0] = WORKER_OP_OFFLINE
        text = center.format_test_health(0)
    assert "verdict=READY" in text
    assert "ping=yes" in text
    assert "JOY written=2" in text


def test_attach_diag_probe_no_tcp_open():
    from ac_workers.control_center import ControlCenter, WorkerView

    center = ControlCenter(REPO)
    view = WorkerView(worker_id=0)
    with mock.patch(
        "ac_workers.control_center._cached_diag_probe",
        return_value=(True, "ok", {}, 10.0),
    ), mock.patch("ac_workers.worker_handshake.tcp_open") as tcp_mock:
        center._attach_diag_probe_fields(0, {"collector_pid": 1}, view)
    tcp_mock.assert_not_called()
    assert view.diag_ready is True


def test_dashboard_diag_cache_non_blocking():
    from ac_workers import control_center as cc

    with cc._DIAG_PROBE_LOCK:
        cc._DIAG_PROBE_CACHE.clear()
        cc._DIAG_PROBE_INFLIGHT.clear()
    ok, reason, snap, age = cc._diag_cache_peek(0)
    assert ok is False
    assert reason == "diag_pending"
    with mock.patch.object(cc.threading, "Thread") as thread_cls:
        thread_cls.return_value.start = lambda: None
        cc._diag_probe_schedule(0, {"collector_pid": 1})
    thread_cls.assert_called_once()


def test_test_start_is_async_and_returns_immediately():
    """test-start must spawn a background job and return at once (no 60s block)."""
    import time

    from ac_workers.control_center import ControlCenter

    center = ControlCenter(REPO)
    launched = {"n": 0}

    def _slow_launch(wid):
        launched["n"] += 1
        time.sleep(2.0)  # would block the UI if test-start ran synchronously

    health_ready = {
        "verdict": "READY", "ac_pid": 999, "ac_alive": True,
        "collector_pid": 1234, "collector_alive": True, "diag_ping_ok": True,
        "diag_port": 24100, "telemetry_age_ms": 10.0, "vjoy_acquired": True,
        "phase": "idle", "boot_id": "b", "failed_step": "", "reason": "",
        "startup_error": "", "op_state": "running", "diag_listener_ready": True,
    }
    with mock.patch.object(center, "_test_force_clean", return_value=(True, "OK")), \
         mock.patch.object(center, "_launch_worker_start", side_effect=_slow_launch), \
         mock.patch.object(center, "_reload_session_from_manifest", return_value=None), \
         mock.patch.object(center, "_test_health_dict", return_value=health_ready), \
         mock.patch.object(center, "_test_final_validation", return_value=(True, "ok")), \
         mock.patch("ac_workers.vjoy_pool.validate_pool_mapping", return_value=None), \
         mock.patch.object(center, "_manifest_session_id", return_value="sid"):
        t0 = time.time()
        msg = center.test_start_worker(0)
        elapsed = time.time() - t0
        assert elapsed < 1.0, "test-start blocked the caller for {0:.1f}s".format(elapsed)
        assert "job started" in msg
        # Status reflects a running/finished job, never "no job".
        deadline = time.time() + 5.0
        while time.time() < deadline:
            with center._test_jobs_lock:
                job = center._test_jobs.get(0)
            if job and not job.get("running"):
                break
            time.sleep(0.05)
    assert job is not None
    assert job["verdict"] == "READY"


def test_test_start_double_start_blocked():
    from ac_workers.control_center import ControlCenter

    center = ControlCenter(REPO)
    with center._test_jobs_lock:
        center._test_jobs[0] = {
            "worker_id": 0, "running": True, "step": "diag_ping",
            "started_at": 0.0, "verdict": "PENDING", "log": [],
        }
    msg = center.test_start_worker(0)
    assert "already running" in msg


def test_test_infer_step_progression():
    from ac_workers.control_center import ControlCenter

    center = ControlCenter(REPO)

    def _h(**kw):
        base = {
            "verdict": "PENDING", "ac_alive": False, "collector_alive": False,
            "diag_ping_ok": False, "telemetry_age_ms": 99999.0, "vjoy_acquired": False,
            "phase": "", "diag_listener_ready": None,
        }
        base.update(kw)
        return base

    assert center._test_infer_step(0, _h()) == "launch_ac"
    assert center._test_infer_step(0, _h(ac_alive=True, phase="waiting_for_management")) == "wait_window"
    assert center._test_infer_step(0, _h(ac_alive=True, phase="waiting_for_drive")) == "click_drive"
    assert center._test_infer_step(0, _h(collector_alive=True, diag_listener_ready=False)) == "diag_bind"
    assert center._test_infer_step(0, _h(collector_alive=True)) == "diag_ping"
    assert center._test_infer_step(0, _h(collector_alive=True, diag_ping_ok=True)) == "telemetry_wait"
    assert center._test_infer_step(
        0, _h(collector_alive=True, diag_ping_ok=True, telemetry_age_ms=10.0)
    ) == "vjoy_acquire"
    assert center._test_infer_step(0, _h(verdict="READY")) == "idle_ready"


def test_test_session_scope_ignores_stale_status():
    from ac_workers.control_center import ControlCenter

    center = ControlCenter(REPO)
    job = {
        "worker_id": 0,
        "startup_run_id": "abc123",
        "expected_session_id": "current_session",
        "log": [],
    }
    with mock.patch.object(center, "_worker_status", return_value={
        "session_id": "old_session",
        "ac_pid": 7608,
        "collector_pid": 24180,
        "boot_id": "oldboot",
    }), mock.patch.object(center, "_manifest_worker", return_value={
        "sessionId": "current_session",
        "acPid": 27212,
        "collectorPid": 5124,
    }):
        st, manifest, notes = center._test_session_scope(0, job)
    assert "stale_state_ignored" in notes[0]
    assert st == {}
    assert int(manifest.get("acPid") or 0) == 27212


def test_test_bind_pids_logs_mismatch():
    from ac_workers.control_center import ControlCenter

    center = ControlCenter(REPO)
    job = {
        "worker_id": 0,
        "startup_run_id": "run1",
        "bound_ac_pid": 27212,
        "bound_collector_pid": 5124,
        "log": [],
    }
    sources = {
        "ac_pid_from_launcher": 7608,
        "ac_pid_from_manifest": 0,
        "ac_pid_from_status": 0,
        "collector_pid_from_status": 24180,
        "collector_pid_from_manifest": 0,
    }
    with mock.patch("ac_workers.control_center.pid_alive", return_value=True):
        notes = center._test_bind_pids(0, job, sources, {}, ping_trusted=False)
    assert any("PID_MISMATCH" in n for n in notes)
    assert any("COLLECTOR_PID_MISMATCH" in n for n in notes)
    assert job["bound_ac_pid"] == 27212
    assert job["bound_collector_pid"] == 5124


def test_read_launch_ac_pid_since_ts():
    from ac_workers.control_center import ControlCenter

    center = ControlCenter(REPO)
    with mock.patch.object(center, "repo", REPO):
        # Future timestamp => no match from old logs
        pid = center._read_launch_ac_pid_from_log(0, since_ts=time.time() + 3600)
    assert pid == 0


import time


def main() -> int:
    test_format_test_health_fields()
    print("OK test_format_test_health_fields")
    test_attach_diag_probe_no_tcp_open()
    print("OK test_attach_diag_probe_no_tcp_open")
    test_dashboard_diag_cache_non_blocking()
    print("OK test_dashboard_diag_cache_non_blocking")
    test_test_start_is_async_and_returns_immediately()
    print("OK test_test_start_is_async_and_returns_immediately")
    test_test_start_double_start_blocked()
    print("OK test_test_start_double_start_blocked")
    test_test_infer_step_progression()
    print("OK test_test_infer_step_progression")
    test_test_session_scope_ignores_stale_status()
    print("OK test_test_session_scope_ignores_stale_status")
    test_test_bind_pids_logs_mismatch()
    print("OK test_test_bind_pids_logs_mismatch")
    test_read_launch_ac_pid_since_ts()
    print("OK test_read_launch_ac_pid_since_ts")
    print("Test-TestWorker: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
