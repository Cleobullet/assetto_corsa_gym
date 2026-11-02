"""Offline tests for canonical collector PID resolution."""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]


def test_launcher_dead_real_pid_alive_not_failure():
    from ac_workers.collector_pid import collector_process_alive

    entry = {"workerId": 0, "collectorPid": 11260, "collectorLauncherPid": 9860}
    status = {"collector_pid": 11260, "session_id": "sess1", "heartbeat_age_ms": 0.0}
    with mock.patch(
        "ac_workers.collector_pid.collector_evidence",
        return_value=(True, 11260, "ok", {}),
    ):
        alive, pid, reason = collector_process_alive(0, entry, status, session_id="sess1")
    assert alive is True
    assert pid == 11260
    assert reason == "ok"


def test_status_child_pid_differs_from_launcher():
    from ac_workers.collector_pid import canonical_collector_pid

    entry = {"workerId": 0, "collectorPid": 9860, "collectorLauncherPid": 9860}
    status = {"collector_pid": 11260}
    assert canonical_collector_pid(0, entry, status) == 11260


def test_evidence_transfers_to_descendant_pid():
    from ac_workers.collector_pid import collector_evidence

    status = {"collector_pid": 11260, "session_id": "sess1", "phase": "environment_loading", "heartbeat_age_ms": 0.0}
    diag = {"collector_pid": 11260, "session_id": "sess1", "heartbeat_age_ms": 0.0, "phase": "environment_loading"}
    with mock.patch("ac_workers.collector_pid.read_worker_status", return_value=status), mock.patch(
        "ac_workers.collector_pid._diag_snapshot", return_value=diag
    ), mock.patch("ac_workers.collector_pid._pid_alive", side_effect=lambda pid: pid == 11260), mock.patch(
        "ac_workers.collector_pid._is_collector_process",
        side_effect=lambda pid, wid: pid == 11260 and wid == 0,
    ), mock.patch("ac_workers.collector_pid.scan_collector_pids", return_value=[11260]), mock.patch(
        "ac_workers.collector_pid.find_descendant_collector_pids", return_value=[11260]
    ):
        alive, pid, reason, _ = collector_evidence(0, "sess1", 9860, 9860, grace_sec=3.0)
    assert alive is True
    assert pid == 11260
    assert reason in ("ok", "diag_alive", "initializing")


def test_init_phase_diag_fresh_not_dead():
    from ac_workers.collector_pid import collector_evidence

    status = {"collector_pid": 11260, "session_id": "sess1", "phase": "environment_loading", "heartbeat_age_ms": 100.0}
    diag = {"collector_pid": 11260, "session_id": "sess1", "heartbeat_age_ms": 100.0, "phase": "environment_loading"}
    with mock.patch("ac_workers.collector_pid.read_worker_status", return_value=status), mock.patch(
        "ac_workers.collector_pid._diag_snapshot", return_value=diag
    ), mock.patch("ac_workers.collector_pid._pid_alive", return_value=False), mock.patch(
        "ac_workers.collector_pid._is_collector_process", return_value=True
    ), mock.patch("ac_workers.collector_pid.scan_collector_pids", return_value=[]), mock.patch(
        "ac_workers.collector_pid.find_descendant_collector_pids", return_value=[]
    ):
        alive, pid, reason, _ = collector_evidence(0, "sess1", 9860, 9860, grace_sec=3.0)
    assert alive is True
    assert reason in ("diag_alive", "initializing")


def test_real_collector_dead_after_grace():
    from ac_workers.collector_pid import collector_evidence

    with mock.patch("ac_workers.collector_pid.read_worker_status", return_value={}), mock.patch(
        "ac_workers.collector_pid._diag_snapshot", return_value=None
    ), mock.patch("ac_workers.collector_pid.scan_collector_pids", return_value=[]), mock.patch(
        "ac_workers.collector_pid.find_descendant_collector_pids", return_value=[]
    ):
        alive, pid, reason, _ = collector_evidence(0, "sess1", 9860, 9860, grace_sec=0.0)
    assert alive is False
    assert reason == "collector_real_process_died"


def test_archive_failure_before_cleanup():
    from ac_workers.collector_pid import archive_collector_failure

    with __import__("tempfile").TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "ac_workers" / "status").mkdir(parents=True)
        with mock.patch("ac_workers.collector_pid.collector_evidence", return_value=(False, 9860, "collector_real_process_died", {})), mock.patch(
            "ac_workers.collector_pid.read_worker_status", return_value={"phase": "environment_loading"}
        ), mock.patch("ac_workers.collector_pid._diag_snapshot", return_value={"session_id": "sess1"}):
            path = archive_collector_failure(0, "sess1", "collector_real_process_died", repo_root=repo)
        assert path is not None
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["reason"] == "collector_real_process_died"
        assert path.name == "worker_0_sess1_failure.json"


def test_manifest_fields_distinct():
    src = (REPO / "scripts" / "Start-Multi-AI-Session.ps1").read_text(encoding="utf-8")
    assert "collectorLauncherPid" in src
    assert "$mw.collectorLauncherPid = $collectorLauncherPid" in src
    assert "$mw.collectorPid = $realCollectorPid" in src


def test_stop_targets_launcher_and_real_pid():
    src = (REPO / "scripts" / "Stop-Multi-AI-Session.ps1").read_text(encoding="utf-8")
    assert "collectorLauncherPid" in src


def test_fresh_init_phase_never_real_process_died():
    from ac_workers.collector_pid import collector_evidence

    status = {
        "collector_pid": 11260,
        "session_id": "sess1",
        "phase": "attaching_live_session",
        "heartbeat_age_ms": 100.0,
    }
    diag = {
        "collector_pid": 11260,
        "session_id": "sess1",
        "heartbeat_age_ms": 100.0,
        "phase": "attaching_live_session",
    }
    with mock.patch("ac_workers.collector_pid.read_worker_status", return_value=status), mock.patch(
        "ac_workers.collector_pid._diag_open", return_value=True
    ), mock.patch("ac_workers.collector_pid._diag_snapshot", return_value=diag), mock.patch(
        "ac_workers.collector_pid._pid_alive", return_value=False
    ), mock.patch("ac_workers.collector_pid._is_collector_process", return_value=True), mock.patch(
        "ac_workers.collector_pid.scan_collector_pids", return_value=[]
    ), mock.patch(
        "ac_workers.collector_pid.find_descendant_collector_pids", return_value=[]
    ):
        alive, _pid, reason, _ = collector_evidence(0, "sess1", 11260, 9860, grace_sec=0.0)
    assert alive is True
    assert reason in ("initializing", "diag_alive")
    assert reason != "collector_real_process_died"


def test_offline_worker_skips_heavy_scan():
    import time
    from ac_workers.collector_pid import collector_evidence

    with mock.patch("ac_workers.collector_pid.read_worker_status", return_value={}), mock.patch(
        "ac_workers.collector_pid._diag_open", return_value=False
    ), mock.patch("ac_workers.collector_pid.scan_collector_pids") as scan, mock.patch(
        "ac_workers.collector_pid.find_descendant_collector_pids"
    ) as desc:
        t0 = time.time()
        alive, _pid, reason, _ = collector_evidence(0, "sess1", 0, 0, grace_sec=0.0)
        elapsed = time.time() - t0
    assert alive is False
    assert reason == "collector_real_process_died"
    assert elapsed < 0.5
    scan.assert_not_called()
    desc.assert_not_called()


def main() -> int:
    tests = [
        test_launcher_dead_real_pid_alive_not_failure,
        test_status_child_pid_differs_from_launcher,
        test_evidence_transfers_to_descendant_pid,
        test_init_phase_diag_fresh_not_dead,
        test_real_collector_dead_after_grace,
        test_fresh_init_phase_never_real_process_died,
        test_archive_failure_before_cleanup,
        test_offline_worker_skips_heavy_scan,
        test_manifest_fields_distinct,
        test_stop_targets_launcher_and_real_pid,
    ]
    for fn in tests:
        fn()
        print("OK", fn.__name__)
    print("Test-CollectorPid-Offline: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
