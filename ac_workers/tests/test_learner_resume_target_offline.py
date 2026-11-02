"""Offline tests for learner resume / continuous training targets."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "assetto_corsa_gym"))
sys.path.insert(0, str(REPO / "algorithm" / "discor"))

CONFIG = REPO / "config_first_sac_cpu_v3.yml"


def _trainer(work: Path, **kwargs):
    from ac_workers.multi_sac_trainer import MultiSacTrainer

    if not CONFIG.is_file():
        raise FileNotFoundError("missing config_first_sac_cpu_v3.yml")
    return MultiSacTrainer(CONFIG, work, **kwargs)


def test_resume_continuous_does_not_stop_at_config_num_steps():
    with tempfile.TemporaryDirectory() as td:
        trainer = _trainer(Path(td), resume=True, continuous=True)
        trainer.steps = 10146
        trainer._resume = True
        trainer._finalize_training_target()
        assert trainer.continuous_mode is True
        assert trainer._target_steps is None
        assert trainer._checkpoint_loaded_step == 10146
        assert trainer._should_continue_training() is True
        assert trainer._remaining_steps() is None


def test_resume_bounded_extends_target_by_max_steps():
    with tempfile.TemporaryDirectory() as td:
        trainer = _trainer(Path(td), max_steps=10000, resume=True, continuous=False)
        trainer.steps = 10146
        trainer._resume = True
        trainer._finalize_training_target()
        assert trainer._target_steps == 20146
        assert trainer._should_continue_training() is True
        assert trainer._remaining_steps() == 10000
        trainer.steps = 20146
        assert trainer._should_continue_training() is False
        assert trainer._remaining_steps() == 0


def test_fresh_bounded_uses_absolute_target():
    with tempfile.TemporaryDirectory() as td:
        trainer = _trainer(Path(td), max_steps=5000, continuous=False)
        trainer.steps = 0
        trainer._finalize_training_target()
        assert trainer._target_steps == 5000
        assert trainer._remaining_steps() == 5000


def test_learner_status_includes_training_target_fields():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        work = tmp / "outputs" / "multi_sac_cpu_v3"
        work.mkdir(parents=True)
        status = {
            "global_steps": 10150,
            "updates": 8650,
            "replay_size": 120,
            "continuous_mode": True,
            "checkpoint_loaded_step": 10146,
            "target_steps": None,
            "remaining_steps": None,
            "checkpoint_loaded_path": str(work / "model" / "checkpoints" / "resume"),
        }
        (work / "learner_status.json").write_text(json.dumps(status), encoding="utf-8")
        (work / "trainer_state.json").write_text(
            json.dumps({"steps": 10150, "updates": 8650}), encoding="utf-8"
        )

        import ac_workers.control_center as cc

        old_work = cc.LEARNER_WORK_DIR
        old_status = cc.LEARNER_STATUS
        old_trainer = cc.TRAINER_STATE
        old_resume = cc.LEARNER_RESUME_CKPT
        try:
            cc.LEARNER_WORK_DIR = work
            cc.LEARNER_STATUS = work / "learner_status.json"
            cc.TRAINER_STATE = work / "trainer_state.json"
            cc.LEARNER_RESUME_CKPT = work / "model" / "checkpoints" / "resume"
            cc.LEARNER_PUBLISH_DIR = work / "model" / "publish"
            from ac_workers.control_center import ControlCenter

            report = json.loads(ControlCenter(tmp).learner_status_text())
            assert report["continuous_mode"] is True
            assert report["checkpoint_loaded_step"] == 10146
            assert report["target_steps"] is None
            assert report["remaining_steps"] is None
        finally:
            cc.LEARNER_WORK_DIR = old_work
            cc.LEARNER_STATUS = old_status
            cc.TRAINER_STATE = old_trainer
            cc.LEARNER_RESUME_CKPT = old_resume


def test_trainer_stop_request_marker():
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        stop_path = work / "learner_stop.request"
        stop_path.write_text("stop\n", encoding="utf-8")
        trainer = _trainer(work, continuous=True)
        assert trainer._stop_requested() is True


if __name__ == "__main__":
    tests = [
        test_resume_continuous_does_not_stop_at_config_num_steps,
        test_resume_bounded_extends_target_by_max_steps,
        test_fresh_bounded_uses_absolute_target,
        test_learner_status_includes_training_target_fields,
        test_trainer_stop_request_marker,
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
