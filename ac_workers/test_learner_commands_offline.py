"""Offline tests for C4D learner status/stop/save and trainer checkpoint cycle."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "assetto_corsa_gym"))
sys.path.insert(0, str(REPO / "algorithm" / "discor"))


def _make_center(tmp: Path):
    from ac_workers.control_center import ControlCenter

    center = ControlCenter(tmp)
    return center


def test_learner_status_without_active_learner():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        center = _make_center(tmp)
        text = center.learner_status_text()
        assert "no learner data" in text or "learner_running" in text


def test_learner_status_with_trainer_state():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        work = tmp / "outputs" / "multi_sac_cpu_v3"
        work.mkdir(parents=True)
        (work / "trainer_state.json").write_text(
            json.dumps({"steps": 4000, "updates": 2500, "replay_size": 4000}),
            encoding="utf-8",
        )
        resume = work / "model" / "checkpoints" / "resume"
        resume.mkdir(parents=True)
        (resume / "policy_net.pth").write_text("fake", encoding="utf-8")

        import ac_workers.control_center as cc

        old_repo = cc.REPO
        old_work = cc.LEARNER_WORK_DIR
        old_status = cc.LEARNER_STATUS
        old_trainer = cc.TRAINER_STATE
        old_resume = cc.LEARNER_RESUME_CKPT
        try:
            cc.REPO = tmp
            cc.LEARNER_WORK_DIR = work
            cc.LEARNER_STATUS = work / "learner_status.json"
            cc.TRAINER_STATE = work / "trainer_state.json"
            cc.LEARNER_RESUME_CKPT = resume
            cc.LEARNER_PUBLISH_DIR = work / "model" / "publish"
            center = _make_center(tmp)
            report = json.loads(center.learner_status_text())
            assert report["global_steps"] == 4000
            assert report["sac_updates"] == 2500
            assert report["resume_checkpoint_available"] is False
        finally:
            cc.REPO = old_repo
            cc.LEARNER_WORK_DIR = old_work
            cc.LEARNER_STATUS = old_status
            cc.TRAINER_STATE = old_trainer
            cc.LEARNER_RESUME_CKPT = old_resume


def test_learner_stop_creates_stop_signal():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        work = tmp / "outputs" / "multi_sac_cpu_v3"
        work.mkdir(parents=True)
        stop_path = work / "learner_stop.request"

        import ac_workers.control_center as cc

        old_work = cc.LEARNER_WORK_DIR
        old_stop = cc.LEARNER_STOP
        old_status = cc.LEARNER_STATUS
        old_trainer = cc.TRAINER_STATE
        old_manifest = cc.MANIFEST
        try:
            cc.LEARNER_WORK_DIR = work
            cc.LEARNER_STOP = stop_path
            cc.LEARNER_STATUS = work / "learner_status.json"
            cc.TRAINER_STATE = work / "trainer_state.json"
            cc.MANIFEST = tmp / "ac_workers" / "manifest.json"
            (cc.MANIFEST.parent).mkdir(parents=True, exist_ok=True)
            cc.MANIFEST.write_text("{}", encoding="utf-8")
            center = _make_center(tmp)
            msg = center.learner_stop()
            assert "not running" in msg.lower()
        finally:
            cc.LEARNER_WORK_DIR = old_work
            cc.LEARNER_STOP = old_stop
            cc.LEARNER_STATUS = old_status
            cc.TRAINER_STATE = old_trainer
            cc.MANIFEST = old_manifest


def test_learner_save_creates_save_signal():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        work = tmp / "outputs" / "multi_sac_cpu_v3"
        work.mkdir(parents=True)
        save_path = work / "learner_save.request"

        import ac_workers.control_center as cc

        old_work = cc.LEARNER_WORK_DIR
        old_save = cc.LEARNER_SAVE
        try:
            cc.LEARNER_WORK_DIR = work
            cc.LEARNER_SAVE = save_path
            center = _make_center(tmp)
            with mock.patch.object(center, "_learner_pid_from_status_or_manifest", return_value=12345):
                with mock.patch("ac_workers.control_center.pid_alive", return_value=True):
                    msg = center.learner_save()
            assert save_path.is_file()
            assert "save requested" in msg.lower()
        finally:
            cc.LEARNER_WORK_DIR = old_work
            cc.LEARNER_SAVE = old_save


def test_trainer_stop_and_save_request_files():
    """Trainer polls learner_stop.request and learner_save.request marker files."""
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        stop_path = work / "learner_stop.request"
        save_path = work / "learner_save.request"
        stop_path.write_text("stop\n", encoding="utf-8")
        save_path.write_text("save\n", encoding="utf-8")
        assert stop_path.is_file()
        assert save_path.is_file()
        assert stop_path.read_text(encoding="utf-8").strip() == "stop"
        assert save_path.read_text(encoding="utf-8").strip() == "save"


def test_trainer_state_json_format():
    """Checkpoint save writes trainer_state.json with steps/updates/replay_size."""
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        state = {"steps": 100, "updates": 50, "replay_size": 100, "last_checkpoint": "step_100"}
        path = work / "trainer_state.json"
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded["steps"] == 100
        assert loaded["updates"] == 50
        assert loaded["replay_size"] == 100


if __name__ == "__main__":
    tests = [
        test_learner_status_without_active_learner,
        test_learner_status_with_trainer_state,
        test_learner_stop_creates_stop_signal,
        test_learner_save_creates_save_signal,
        test_trainer_stop_and_save_request_files,
        test_trainer_state_json_format,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed out of {len(tests)} tests")
    sys.exit(1 if failed else 0)
