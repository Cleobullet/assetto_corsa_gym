"""Offline tests for the distributed actor-learner architecture with local actors.

Validates:
- Frequent policy publishing (publish_every_updates)
- CollectorPolicy hot-reload detection with version tracking
- Per-worker independence (no worker blocks another)
- Atomic publish_meta.json with policy_version
- Worker-level transition tracking
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "assetto_corsa_gym"))
sys.path.insert(0, str(REPO / "algorithm" / "discor"))


def test_publish_every_updates_default_is_1():
    config = REPO / "config_first_sac_cpu_v3.yml"
    if not config.is_file():
        print("  SKIP  test_publish_every_updates_default_is_1 (no config)")
        return
    from ac_workers.multi_sac_trainer import MultiSacTrainer

    with tempfile.TemporaryDirectory() as td:
        trainer = MultiSacTrainer(config, Path(td), continuous=True)
        assert trainer.publish_every_updates == 1, (
            "default publish_every_updates should be 1, got {}".format(
                trainer.publish_every_updates
            )
        )


def _init_trainer_with_networks(config, work_dir):
    """Helper: create a trainer and init its SAC networks so _publish() works."""
    from ac_workers.multi_sac_trainer import MultiSacTrainer
    import numpy as np

    trainer = MultiSacTrainer(config, work_dir, continuous=True)
    fake_tr = {
        "obs": [0.0] * 125, "action": [0.0, 0.0, 0.0],
        "reward": 0.0, "done": False, "worker_id": 0,
    }
    trainer._ingest_transition(fake_tr)
    return trainer


def test_publish_meta_written_with_policy_version():
    """A publish writes a versioned dir + an atomic latest.json pointer."""
    config = REPO / "config_first_sac_cpu_v3.yml"
    if not config.is_file():
        print("  SKIP  test_publish_meta_written_with_policy_version (no config)")
        return

    with tempfile.TemporaryDirectory() as td:
        trainer = _init_trainer_with_networks(config, Path(td))
        trainer._policy_version = 42
        trainer._publish()

        model_dir = Path(td) / "model"
        pointer_path = model_dir / "latest.json"
        assert pointer_path.is_file(), "latest.json pointer not created"
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        assert pointer["complete"] is True
        assert pointer["policy_version"] == 42
        version_dir = model_dir / "publish_versions" / pointer["version_dir"]
        assert version_dir.is_dir()
        assert (version_dir / "policy_net.pth").stat().st_size > 0
        assert trainer._last_publish_complete is True
        assert trainer._last_publish_version == 42


def test_collector_policy_tracks_reload_count():
    """CollectorPolicy tracks reload_count and local_policy_version from publish_meta."""
    config = REPO / "config_first_sac_cpu_v3.yml"
    if not config.is_file():
        print("  SKIP  test_collector_policy_tracks_reload_count (no config)")
        return
    from ac_workers.collector_policy import CollectorPolicy

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        trainer = _init_trainer_with_networks(config, work)
        trainer._policy_version = 10
        trainer._publish()

        policy = CollectorPolicy(
            config, work, state_dim=125, worker_id=0,
        )
        loaded = policy.reload_if_updated()
        assert loaded, "policy should have loaded"
        status = policy.policy_status()
        assert status["policy_reload_count"] == 1
        assert status["local_policy_version"] == 10
        assert status["last_policy_reload_age_sec"] is not None
        assert status["last_policy_reload_age_sec"] < 5.0

        time.sleep(0.05)
        trainer._policy_version = 20
        trainer._publish()

        loaded2 = policy.reload_if_updated()
        assert loaded2, "policy should reload on new mtime"
        status2 = policy.policy_status()
        assert status2["policy_reload_count"] == 2
        assert status2["local_policy_version"] == 20


def test_maybe_publish_triggers_after_n_updates():
    config = REPO / "config_first_sac_cpu_v3.yml"
    if not config.is_file():
        print("  SKIP  test_maybe_publish_triggers_after_n_updates (no config)")
        return
    from ac_workers.multi_sac_trainer import MultiSacTrainer

    with tempfile.TemporaryDirectory() as td:
        trainer = MultiSacTrainer(config, Path(td), continuous=True)
        trainer.publish_every_updates = 3
        trainer._updates_since_last_publish = 0
        trainer._policy_published_count = 0

        trainer._updates_since_last_publish = 2
        trainer._maybe_publish()
        assert trainer._policy_published_count == 0, (
            "should not publish with only 2 updates (need 3)"
        )

        trainer._updates_since_last_publish = 3
        trainer._maybe_publish()
        assert trainer._policy_published_count == 1
        assert trainer._updates_since_last_publish == 0


def test_transitions_by_worker_tracking():
    config = REPO / "config_first_sac_cpu_v3.yml"
    if not config.is_file():
        print("  SKIP  test_transitions_by_worker_tracking (no config)")
        return

    with tempfile.TemporaryDirectory() as td:
        trainer = _init_trainer_with_networks(config, Path(td))
        trainer._transitions_by_worker.clear()
        tr0 = {"worker_id": 0, "obs": [0.0] * 125, "action": [0.0, 0.0, 0.0],
               "reward": 0.1, "done": False}
        tr2 = {"worker_id": 2, "obs": [0.0] * 125, "action": [0.0, 0.0, 0.0],
               "reward": 0.2, "done": False}
        trainer._ingest_transition(tr0)
        trainer._ingest_transition(tr0)
        trainer._ingest_transition(tr2)
        assert trainer._transitions_by_worker == {0: 2, 2: 1}
        assert trainer._last_transition_worker_id == 2


def test_default_control_mode_is_async_local_actor():
    from common.transition_server import TransitionServer, TransitionClient

    port = 24562
    srv = TransitionServer(port=port)
    srv.start()
    time.sleep(0.2)
    try:
        client = TransitionClient(port=port)
        stats = client.stats()
        assert stats["control_mode"] == "async_local_actor", (
            "default control_mode should be async_local_actor, got {}".format(
                stats["control_mode"]
            )
        )
    finally:
        srv.stop()


def test_two_policies_reload_independently():
    """Two CollectorPolicy instances (two workers) reload at their own pace."""
    config = REPO / "config_first_sac_cpu_v3.yml"
    if not config.is_file():
        print("  SKIP  test_two_policies_reload_independently (no config)")
        return
    from ac_workers.collector_policy import CollectorPolicy

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        trainer = _init_trainer_with_networks(config, work)
        trainer._policy_version = 1
        trainer._publish()

        p0 = CollectorPolicy(config, work, state_dim=125, worker_id=0)
        p1 = CollectorPolicy(config, work, state_dim=125, worker_id=1)
        p0.reload_if_updated()
        assert p0.policy_status()["policy_reload_count"] == 1
        assert p1.policy_status()["policy_reload_count"] == 0

        time.sleep(0.05)
        trainer._policy_version = 2
        trainer._publish()
        p1.reload_if_updated()
        assert p0.policy_status()["local_policy_version"] == 1
        assert p1.policy_status()["local_policy_version"] == 2

        p0.reload_if_updated()
        assert p0.policy_status()["local_policy_version"] == 2
        assert p0.policy_status()["policy_reload_count"] == 2
        assert p1.policy_status()["policy_reload_count"] == 1


def test_worker_seeds_differ():
    """Different worker_ids produce different policy seeds for exploration diversity."""
    config = REPO / "config_first_sac_cpu_v3.yml"
    if not config.is_file():
        print("  SKIP  test_worker_seeds_differ (no config)")
        return
    from ac_workers.collector_policy import CollectorPolicy

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        p0 = CollectorPolicy(config, work, state_dim=125, worker_id=0)
        p1 = CollectorPolicy(config, work, state_dim=125, worker_id=1)
        p2 = CollectorPolicy(config, work, state_dim=125, worker_id=2)
        seeds = {p0.policy_seed, p1.policy_seed, p2.policy_seed}
        assert len(seeds) == 3, "all worker seeds should be unique: {}".format(seeds)


if __name__ == "__main__":
    tests = [
        test_publish_every_updates_default_is_1,
        test_publish_meta_written_with_policy_version,
        test_collector_policy_tracks_reload_count,
        test_maybe_publish_triggers_after_n_updates,
        test_transitions_by_worker_tracking,
        test_default_control_mode_is_async_local_actor,
        test_two_policies_reload_independently,
        test_worker_seeds_differ,
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
