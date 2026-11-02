"""Offline tests for session stale cleanup."""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _write_manifest(path: Path, workers: list[dict]) -> None:
    payload = {
        "schemaVersion": 1,
        "sessionId": "sess_test",
        "workers": workers,
        "transitionServerPid": 12345,
        "transitionServerLauncherPid": 12346,
        "learnerPid": 99999,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def test_worker_manifest_running_dead_pids_effective_stale():
    from ac_workers.session_cleanup import worker_effective_state

    entry = {"workerId": 0, "state": "running", "acPid": 1, "collectorPid": 2}
    eff = worker_effective_state(0, entry, {"collector_pid": 2, "heartbeat_age_ms": 0.0})
    assert eff["manifest_state"] == "running"
    assert eff["effective_state"] in ("stale", "offline")


def test_targeted_stop_removes_worker_from_manifest():
    import ac_workers.session_cleanup as sc
    from AssettoCorsaEnv import ac_worker_runtime as rt

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        manifest_path = root / "ac_workers" / "manifest.json"
        status_dir = root / "ac_workers" / "status"
        status_dir.mkdir(parents=True)
        (status_dir / "worker_0.json").write_text('{"worker_id":0,"phase":"running"}', encoding="utf-8")
        (status_dir / "worker_2.json").write_text('{"worker_id":2,"phase":"running"}', encoding="utf-8")
        _write_manifest(
            manifest_path,
            [
                {"workerId": 0, "state": "running", "acPid": 0, "collectorPid": 0},
                {"workerId": 2, "state": "running", "acPid": 0, "collectorPid": 0},
            ],
        )
        old_manifest = rt.MANIFEST_PATH
        old_status = sc.STATUS_DIR
        rt.MANIFEST_PATH = manifest_path
        sc.STATUS_DIR = status_dir
        sc.STATUS_ARCHIVE = status_dir / "archive"
        try:
            report = sc.cleanup_stale_state(root, worker_ids=[0, 1, 3])
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            ids = [int(w["workerId"]) for w in data["workers"]]
            assert 0 not in ids
            assert 2 in ids
            assert 0 in report["removed_workers"]
            assert not (status_dir / "worker_0.json").is_file()
            assert (status_dir / "worker_2.json").is_file()
        finally:
            rt.MANIFEST_PATH = old_manifest
            sc.STATUS_DIR = old_status
            sc.STATUS_ARCHIVE = old_status / "archive"


def test_learner_json_active_pid_dead_inactive():
    from ac_workers.session_cleanup import learner_effective_state

    learner = {
        "learner_running": True,
        "learner_pid": 999999,
        "global_steps": 302,
        "replay_size": 86,
        "session_id": "s1",
        "ts": "2000-01-01T00:00:00+00:00",
    }
    eff = learner_effective_state(learner, "s1")
    assert eff["active"] is False
    assert eff["status"] == "stale"
    assert eff["saved_steps"] == 302


def test_transition_offline_clears_manifest_pids(monkeypatch=None):
    import ac_workers.session_cleanup as sc
    from AssettoCorsaEnv import ac_worker_runtime as rt

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        manifest_path = root / "ac_workers" / "manifest.json"
        _write_manifest(manifest_path, [])
        old_manifest = rt.MANIFEST_PATH
        rt.MANIFEST_PATH = manifest_path
        original = sc.transition_server_online
        sc.transition_server_online = lambda _port=24050: False
        try:
            report = sc.cleanup_stale_state(root, audit_all=True)
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            assert data["transitionServerPid"] is None
            assert report["transition_cleared"] is True
        finally:
            sc.transition_server_online = original
            rt.MANIFEST_PATH = old_manifest


def test_manifest_atomic_write():
    from AssettoCorsaEnv.ac_worker_runtime import read_manifest, write_manifest

    with tempfile.TemporaryDirectory() as tmp:
        import AssettoCorsaEnv.ac_worker_runtime as rt

        path = Path(tmp) / "manifest.json"
        old = rt.MANIFEST_PATH
        rt.MANIFEST_PATH = path
        try:
            write_manifest({"schemaVersion": 1, "workers": [], "sessionId": "a"})
            assert path.is_file()
            assert not path.with_suffix(".json.tmp").exists()
            assert read_manifest()["sessionId"] == "a"
        finally:
            rt.MANIFEST_PATH = old


def test_cleanup_import():
    from ac_workers.session_cleanup import cleanup_stale_state, effective_report

    assert callable(cleanup_stale_state)
    assert callable(effective_report)


def main() -> int:
    tests = [
        test_worker_manifest_running_dead_pids_effective_stale,
        test_targeted_stop_removes_worker_from_manifest,
        test_learner_json_active_pid_dead_inactive,
        test_transition_offline_clears_manifest_pids,
        test_manifest_atomic_write,
        test_cleanup_import,
    ]
    for fn in tests:
        fn()
        print("OK", fn.__name__)
    print("Test-SessionCleanup-Offline: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
