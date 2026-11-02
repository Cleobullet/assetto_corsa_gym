"""Offline tests for per-worker SAC exploration diversity and learner replay guard."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest import mock

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "assetto_corsa_gym"))
sys.path.insert(0, str(REPO / "algorithm" / "discor"))

CONFIG = REPO / "config_first_sac_cpu_v3.yml"
WORK_DIR = REPO / "outputs" / "multi_sac_cpu_v3"


def test_worker_policy_seeds_are_distinct():
    from ac_workers.action_diagnostics import worker_policy_seed

    seeds = [worker_policy_seed(0, wid) for wid in range(6)]
    assert len(set(seeds)) == 6


def test_stochastic_explore_differs_per_worker_same_observation():
    from ac_workers.collector_policy import CollectorPolicy

    state = np.linspace(-0.1, 0.1, 125, dtype=np.float32)
    raw_actions: list[tuple[float, float, float]] = []
    for wid in range(6):
        policy = CollectorPolicy(CONFIG, WORK_DIR, state_dim=125, worker_id=wid)
        policy._weights_loaded = True
        action, _ = policy.algo.explore(state)
        raw_actions.append(tuple(float(x) for x in action[:3]))
    assert len(set(raw_actions)) == 6, "expected 6 distinct explore actions, got {0}".format(raw_actions)


def test_deterministic_exploit_can_match_with_same_seed():
    from ac_workers.collector_policy import CollectorPolicy

    state = np.linspace(-0.1, 0.1, 125, dtype=np.float32)
    p0 = CollectorPolicy(CONFIG, WORK_DIR, state_dim=125, worker_id=0)
    p1 = CollectorPolicy(CONFIG, WORK_DIR, state_dim=125, worker_id=0)
    p0._weights_loaded = True
    p1._weights_loaded = True
    a0, _ = p0.algo.exploit(state)
    a1, _ = p1.algo.exploit(state)
    assert np.allclose(a0, a1)


def test_maybe_update_skips_empty_replay_after_resume():
    from ac_workers.multi_sac_trainer import MultiSacTrainer

    with tempfile.TemporaryDirectory() as td:
        work = Path(td) / "work"
        trainer = MultiSacTrainer(CONFIG, work, resume=False)
        trainer._init_networks(125)
        trainer.steps = 5000
        trainer.training_started = True
        assert trainer.replay is not None
        assert len(trainer.replay) == 0
        assert trainer._maybe_update() is None


def test_action_diversity_summary_reads_status_fields():
    from ac_workers.action_diagnostics import summarize_live_worker_actions

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        status_dir = tmp / "ac_workers" / "status"
        status_dir.mkdir(parents=True)
        for wid in range(2):
            (status_dir / "worker_{0}.json".format(wid)).write_text(
                (
                    '{{"policy_action_source":"sac_explore","policy_seed":{seed},'
                    '"observation_hash":"abc{wid}","policy_raw_action":[{steer},-0.9,-0.98],'
                    '"physical_action":[0.1,0.2,0.3]}}'
                ).format(seed=wid * 10007, wid=wid, steer=0.9 + wid * 0.01),
                encoding="utf-8",
            )
        text = summarize_live_worker_actions(tmp, worker_ids=[0, 1])
        assert "raw_unique=2" in text
        assert "W0" in text and "W1" in text


def test_learner_start_log_written_on_failure():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        import ac_workers.control_center as cc

        old_log = cc.LEARNER_START_LOG
        try:
            log_path = tmp / "ac_workers" / "logs" / "learner_start_last.log"
            cc.LEARNER_START_LOG = log_path
            center = object.__new__(cc.ControlCenter)
            center._learner_last_start_error = ""
            center._write_learner_start_log(1, "Traceback: ValueError high <= 0", 1.23)
            assert log_path.is_file()
            text = center.learner_error_text()
            assert "ValueError" in text
        finally:
            cc.LEARNER_START_LOG = old_log
