"""Offline tests: policy swaps only happen at episode boundaries.

A newer published policy must NOT replace the active policy mid-episode (that
caused teleport/reset loops). It is flagged as pending during driving and only
loaded by apply_pending_policy() at the episode boundary.
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "assetto_corsa_gym"))
sys.path.insert(0, str(REPO / "algorithm" / "discor"))

CONFIG = REPO / "config_first_sac_cpu_v3.yml"


def _have_config() -> bool:
    return CONFIG.is_file()


def _init_trainer(work_dir: Path):
    from ac_workers.multi_sac_trainer import MultiSacTrainer

    trainer = MultiSacTrainer(CONFIG, work_dir, continuous=True)
    trainer._ingest_transition(
        {"obs": [0.0] * 125, "action": [0.0, 0.0, 0.0],
         "reward": 0.0, "done": False, "worker_id": 0}
    )
    return trainer


def test_new_policy_does_not_swap_mid_episode():
    if not _have_config():
        print("  SKIP  test_new_policy_does_not_swap_mid_episode (no config)")
        return
    from ac_workers.collector_policy import CollectorPolicy

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        trainer = _init_trainer(work)
        trainer._policy_version = 1
        trainer._publish()
        policy = CollectorPolicy(CONFIG, work, state_dim=125, worker_id=0)
        policy.reload_if_updated()
        assert policy.policy_status()["active_policy_version"] == 1
        active_algo = policy.algo

        # Learner publishes v2 while the worker is "driving".
        time.sleep(0.05)
        trainer._policy_version = 2
        trainer._publish()

        # Mid-episode: detect only — must NOT swap the live policy.
        pending = policy.detect_pending_version()
        assert pending is True
        st = policy.policy_status()
        assert st["active_policy_version"] == 1, "active must stay v1 mid-episode"
        assert st["pending_policy_version"] == 2
        assert st["policy_swap_pending"] is True
        assert policy.algo is active_algo, "live policy object must not change mid-episode"


def test_pending_policy_applied_at_boundary():
    if not _have_config():
        print("  SKIP  test_pending_policy_applied_at_boundary (no config)")
        return
    from ac_workers.collector_policy import CollectorPolicy

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        trainer = _init_trainer(work)
        trainer._policy_version = 1
        trainer._publish()
        policy = CollectorPolicy(CONFIG, work, state_dim=125, worker_id=0)
        policy.reload_if_updated()

        time.sleep(0.05)
        trainer._policy_version = 2
        trainer._publish()
        policy.detect_pending_version()
        assert policy.policy_status()["active_policy_version"] == 1

        # Episode boundary: now the swap is allowed.
        swapped = policy.apply_pending_policy(episode_id=7)
        assert swapped is True
        st = policy.policy_status()
        assert st["active_policy_version"] == 2
        assert st["policy_swap_pending"] is False
        assert st["policy_swap_count"] == 1
        assert st["last_swap_episode"] == 7


def test_boundary_applies_only_latest_version():
    """Learner publishes several versions during an episode; the boundary swap
    must jump straight to the most recent one."""
    if not _have_config():
        print("  SKIP  test_boundary_applies_only_latest_version (no config)")
        return
    from ac_workers.collector_policy import CollectorPolicy

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        trainer = _init_trainer(work)
        trainer._policy_version = 1
        trainer._publish()
        policy = CollectorPolicy(CONFIG, work, state_dim=125, worker_id=0)
        policy.reload_if_updated()

        for v in (2, 3, 4, 5):
            time.sleep(0.02)
            trainer._policy_version = v
            trainer._publish()
            policy.detect_pending_version()
            assert policy.policy_status()["active_policy_version"] == 1

        policy.apply_pending_policy(episode_id=1)
        assert policy.policy_status()["active_policy_version"] == 5
        assert policy.policy_status()["policy_swap_count"] == 1


def test_no_swap_when_no_new_version():
    if not _have_config():
        print("  SKIP  test_no_swap_when_no_new_version (no config)")
        return
    from ac_workers.collector_policy import CollectorPolicy

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        trainer = _init_trainer(work)
        trainer._policy_version = 3
        trainer._publish()
        policy = CollectorPolicy(CONFIG, work, state_dim=125, worker_id=0)
        policy.reload_if_updated()
        assert policy.policy_status()["active_policy_version"] == 3

        # No new publish -> boundary swap is a no-op.
        assert policy.apply_pending_policy(episode_id=2) is False
        assert policy.policy_status()["policy_swap_count"] == 0
        assert policy.policy_status()["active_policy_version"] == 3


def test_two_workers_swap_at_different_episodes():
    if not _have_config():
        print("  SKIP  test_two_workers_swap_at_different_episodes (no config)")
        return
    from ac_workers.collector_policy import CollectorPolicy

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        trainer = _init_trainer(work)
        trainer._policy_version = 1
        trainer._publish()
        p0 = CollectorPolicy(CONFIG, work, state_dim=125, worker_id=0)
        p1 = CollectorPolicy(CONFIG, work, state_dim=125, worker_id=1)
        p0.reload_if_updated()
        p1.reload_if_updated()

        time.sleep(0.05)
        trainer._policy_version = 2
        trainer._publish()
        p0.detect_pending_version()
        p1.detect_pending_version()

        # Worker 0 hits its boundary first; worker 1 keeps driving v1.
        assert p0.apply_pending_policy(episode_id=10) is True
        assert p0.policy_status()["active_policy_version"] == 2
        assert p1.policy_status()["active_policy_version"] == 1

        # Worker 1 reaches its own boundary later.
        assert p1.apply_pending_policy(episode_id=4) is True
        assert p1.policy_status()["active_policy_version"] == 2
        assert p0.policy_status()["last_swap_episode"] == 10
        assert p1.policy_status()["last_swap_episode"] == 4


def test_bad_pending_keeps_active_at_boundary():
    """A corrupt newest publish must not swap; the boundary keeps the old policy."""
    if not _have_config():
        print("  SKIP  test_bad_pending_keeps_active_at_boundary (no config)")
        return
    import json
    from ac_workers.collector_policy import CollectorPolicy
    from ac_workers.policy_metadata import build_metadata, write_metadata

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        trainer = _init_trainer(work)
        trainer._policy_version = 1
        trainer._publish()
        policy = CollectorPolicy(CONFIG, work, state_dim=125, worker_id=0)
        policy.reload_if_updated()

        # Forge a corrupt v2 pointer.
        model_dir = work / "model"
        bad_dir = model_dir / "publish_versions" / "version_00000002"
        bad_dir.mkdir(parents=True, exist_ok=True)
        for n in ("policy_net.pth", "online_q_net.pth", "target_q_net.pth"):
            (bad_dir / n).write_bytes(b"")
        write_metadata(bad_dir, build_metadata(state_dim=125, training_step=2))
        (model_dir / "latest.json").write_text(json.dumps({
            "complete": True, "policy_version": 2,
            "version_dir": "version_00000002", "timestamp": time.time(),
        }), encoding="utf-8")
        time.sleep(0.05)
        (model_dir / "latest.pt").write_bytes(b"v3")

        policy.detect_pending_version()  # sees v2 pending
        swapped = policy.apply_pending_policy(episode_id=3)
        assert swapped is False
        st = policy.policy_status()
        assert st["active_policy_version"] == 1, "must keep good v1"
        assert st["policy_reload_failed_count"] >= 1


def test_wipe_reset_swaps_even_when_version_goes_to_zero():
    """train wipe publishes version 0; workers must drop the old high version."""
    if not _have_config():
        print("  SKIP  test_wipe_reset_swaps_even_when_version_goes_to_zero (no config)")
        return
    from ac_workers.collector_policy import CollectorPolicy
    from ac_workers.init_fresh_policy import init_fresh_policy

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        trainer = _init_trainer(work)
        trainer._policy_version = 5
        trainer._publish()
        policy = CollectorPolicy(CONFIG, work, state_dim=125, worker_id=0)
        policy.reload_if_updated()
        assert policy.policy_status()["active_policy_version"] == 5

        init_fresh_policy(config_path=CONFIG, work_dir=work, created_from="wipe")

        pending = policy.detect_pending_version()
        assert pending is True
        assert policy.policy_status()["active_policy_version"] == 5
        assert policy.policy_status()["policy_force_reset"] is True
        swapped = policy.apply_pending_policy(episode_id=9)
        assert swapped is True
        assert policy.policy_status()["active_policy_version"] == 0
        assert policy.policy_status()["policy_force_reset"] is False


if __name__ == "__main__":
    tests = [
        test_new_policy_does_not_swap_mid_episode,
        test_pending_policy_applied_at_boundary,
        test_boundary_applies_only_latest_version,
        test_no_swap_when_no_new_version,
        test_two_workers_swap_at_different_episodes,
        test_bad_pending_keeps_active_at_boundary,
        test_wipe_reset_swaps_even_when_version_goes_to_zero,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print("  PASS  {}".format(t.__name__))
            passed += 1
        except Exception as e:
            import traceback

            print("  FAIL  {}: {}".format(t.__name__, e))
            traceback.print_exc()
            failed += 1
    print("\n{} passed, {} failed out of {} tests".format(passed, failed, len(tests)))
    sys.exit(1 if failed else 0)
