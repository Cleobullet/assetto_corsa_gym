"""Offline tests: the policy hot-reload must never crash a worker.

Reproduces the "Ran out of input" failure mode (empty/partial .pth read during
a publish) and asserts the worker keeps its last good policy instead of dying.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
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


def test_missing_publish_uses_random_init():
    if not _have_config():
        print("  SKIP  test_missing_publish_uses_random_init (no config)")
        return
    from ac_workers.collector_policy import CollectorPolicy

    with tempfile.TemporaryDirectory() as td:
        policy = CollectorPolicy(CONFIG, Path(td), state_dim=125, worker_id=0)
        assert policy.reload_if_updated() is True
        st = policy.policy_status()
        assert st["policy_ready"] is True
        assert st["policy_load_error"] == ""
        assert st["policy_checkpoint_path"] == "random_init"
        assert st["policy_reload_failed_count"] == 0


def test_publish_then_reload_succeeds():
    if not _have_config():
        print("  SKIP  test_publish_then_reload_succeeds (no config)")
        return
    from ac_workers.collector_policy import CollectorPolicy

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        trainer = _init_trainer(work)
        trainer._policy_version = 5
        trainer._publish()

        policy = CollectorPolicy(CONFIG, work, state_dim=125, worker_id=0)
        assert policy.reload_if_updated() is True
        st = policy.policy_status()
        assert st["policy_ready"] is True
        assert st["local_policy_version"] == 5
        assert st["last_good_policy_version"] == 5
        assert st["policy_reload_failed_count"] == 0


def test_empty_policy_file_keeps_old_policy():
    """An empty/partial policy file must NOT crash and must keep the old policy."""
    if not _have_config():
        print("  SKIP  test_empty_policy_file_keeps_old_policy (no config)")
        return
    from ac_workers.collector_policy import CollectorPolicy

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        trainer = _init_trainer(work)
        trainer._policy_version = 1
        trainer._publish()

        policy = CollectorPolicy(CONFIG, work, state_dim=125, worker_id=0)
        assert policy.reload_if_updated() is True
        good_algo = policy.algo

        # Simulate a half-written publish: a NEW version dir whose policy_net.pth
        # is empty (0 bytes), with the pointer flipped to it (mid-write race).
        model_dir = work / "model"
        bad_dir = model_dir / "publish_versions" / "version_00000002"
        bad_dir.mkdir(parents=True, exist_ok=True)
        (bad_dir / "policy_net.pth").write_bytes(b"")
        (bad_dir / "online_q_net.pth").write_bytes(b"")
        (bad_dir / "target_q_net.pth").write_bytes(b"")
        from ac_workers.policy_metadata import build_metadata, write_metadata
        write_metadata(bad_dir, build_metadata(state_dim=125, training_step=2))
        pointer = {
            "complete": True, "policy_version": 2,
            "version_dir": "version_00000002", "timestamp": time.time(),
        }
        (model_dir / "latest.json").write_text(json.dumps(pointer), encoding="utf-8")
        # bump the legacy marker mtime so the reload actually attempts
        time.sleep(0.05)
        (model_dir / "latest.pt").write_bytes(b"v3")

        # Must NOT raise, must keep old policy.
        result = policy.reload_if_updated()
        assert result is True, "worker should still report a usable policy"
        assert policy.algo is good_algo, "old policy must be preserved"
        st = policy.policy_status()
        assert st["policy_ready"] is True
        assert st["policy_reload_failed_count"] >= 1
        assert st["last_good_policy_version"] == 1
        assert st["local_policy_version"] == 1


def test_truncated_policy_file_keeps_old_policy():
    """A truncated zip (classic 'Ran out of input') must be survived."""
    if not _have_config():
        print("  SKIP  test_truncated_policy_file_keeps_old_policy (no config)")
        return
    from ac_workers.collector_policy import CollectorPolicy

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        trainer = _init_trainer(work)
        trainer._policy_version = 1
        trainer._publish()
        policy = CollectorPolicy(CONFIG, work, state_dim=125, worker_id=0)
        policy.reload_if_updated()

        # Build a version whose policy file is a valid file head, then truncated.
        trainer2_version = work / "model" / "publish_versions" / "version_00000003"
        trainer2_version.mkdir(parents=True, exist_ok=True)
        good_v1 = work / "model" / "publish_versions" / "version_00000001"
        full = (good_v1 / "policy_net.pth").read_bytes()
        (trainer2_version / "policy_net.pth").write_bytes(full[: len(full) // 2])
        (trainer2_version / "online_q_net.pth").write_bytes(full[: len(full) // 2])
        (trainer2_version / "target_q_net.pth").write_bytes(full[: len(full) // 2])
        from ac_workers.policy_metadata import build_metadata, write_metadata
        write_metadata(trainer2_version, build_metadata(state_dim=125, training_step=3))
        model_dir = work / "model"
        (model_dir / "latest.json").write_text(json.dumps({
            "complete": True, "policy_version": 3,
            "version_dir": "version_00000003", "timestamp": time.time(),
        }), encoding="utf-8")
        time.sleep(0.05)
        (model_dir / "latest.pt").write_bytes(b"v3")

        result = policy.reload_if_updated()
        assert result is True
        st = policy.policy_status()
        assert st["policy_ready"] is True
        assert st["policy_reload_failed_count"] >= 1
        assert st["local_policy_version"] == 1  # stayed on the good version


def test_recovers_after_bad_then_good_publish():
    """After a bad publish is replaced by a good one, the worker reloads it."""
    if not _have_config():
        print("  SKIP  test_recovers_after_bad_then_good_publish (no config)")
        return
    from ac_workers.collector_policy import CollectorPolicy

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        trainer = _init_trainer(work)
        trainer._policy_version = 1
        trainer._publish()
        policy = CollectorPolicy(CONFIG, work, state_dim=125, worker_id=0)
        policy.reload_if_updated()

        model_dir = work / "model"
        # Bad publish (empty)
        bad_dir = model_dir / "publish_versions" / "version_00000002"
        bad_dir.mkdir(parents=True, exist_ok=True)
        for n in ("policy_net.pth", "online_q_net.pth", "target_q_net.pth"):
            (bad_dir / n).write_bytes(b"")
        from ac_workers.policy_metadata import build_metadata, write_metadata
        write_metadata(bad_dir, build_metadata(state_dim=125, training_step=2))
        (model_dir / "latest.json").write_text(json.dumps({
            "complete": True, "policy_version": 2,
            "version_dir": "version_00000002", "timestamp": time.time(),
        }), encoding="utf-8")
        time.sleep(0.05)
        (model_dir / "latest.pt").write_bytes(b"v3")
        policy.reload_if_updated()
        assert policy.policy_status()["local_policy_version"] == 1

        # Good publish version 3
        time.sleep(0.05)
        trainer._policy_version = 3
        trainer._publish()
        assert policy.reload_if_updated() is True
        assert policy.policy_status()["local_policy_version"] == 3
        assert policy.policy_status()["policy_ready"] is True


def test_publish_every_updates_1_concurrent_no_crash():
    """Stress: learner publishes rapidly while a worker reloads in a tight loop.

    The worker must never raise and must always end with a usable policy.
    """
    if not _have_config():
        print("  SKIP  test_publish_every_updates_1_concurrent_no_crash (no config)")
        return
    from ac_workers.collector_policy import CollectorPolicy

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        trainer = _init_trainer(work)
        trainer._policy_version = 1
        trainer._publish()
        policy = CollectorPolicy(CONFIG, work, state_dim=125, worker_id=0)
        policy.reload_if_updated()

        stop = threading.Event()
        errors: list[str] = []

        def _publisher():
            v = 2
            while not stop.is_set():
                trainer._policy_version = v
                try:
                    trainer._publish()
                except Exception as exc:
                    errors.append("publish:{0}".format(exc))
                v += 1
                time.sleep(0.005)

        def _reloader():
            while not stop.is_set():
                try:
                    policy.reload_if_updated()
                except Exception as exc:  # must never happen
                    errors.append("reload:{0}".format(exc))
                time.sleep(0.002)

        tp = threading.Thread(target=_publisher)
        tr = threading.Thread(target=_reloader)
        tp.start()
        tr.start()
        time.sleep(1.5)
        stop.set()
        tp.join(timeout=3.0)
        tr.join(timeout=3.0)

        assert not errors, "reload/publish raised: {0}".format(errors[:3])
        st = policy.policy_status()
        assert st["policy_ready"] is True
        assert st["local_policy_version"] >= 1


def test_two_workers_reload_no_partial_read():
    """Two workers reloading concurrently never crash and converge."""
    if not _have_config():
        print("  SKIP  test_two_workers_reload_no_partial_read (no config)")
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

        stop = threading.Event()
        errors: list[str] = []

        def _reloader(pol):
            while not stop.is_set():
                try:
                    pol.reload_if_updated()
                except Exception as exc:
                    errors.append(str(exc))
                time.sleep(0.002)

        threads = [threading.Thread(target=_reloader, args=(p,)) for p in (p0, p1)]
        for t in threads:
            t.start()
        for v in range(2, 30):
            trainer._policy_version = v
            trainer._publish()
            time.sleep(0.01)
        stop.set()
        for t in threads:
            t.join(timeout=3.0)

        assert not errors, "concurrent reload raised: {0}".format(errors[:3])
        assert p0.policy_status()["policy_ready"] is True
        assert p1.policy_status()["policy_ready"] is True


if __name__ == "__main__":
    tests = [
        test_missing_publish_uses_random_init,
        test_publish_then_reload_succeeds,
        test_empty_policy_file_keeps_old_policy,
        test_truncated_policy_file_keeps_old_policy,
        test_recovers_after_bad_then_good_publish,
        test_publish_every_updates_1_concurrent_no_crash,
        test_two_workers_reload_no_partial_read,
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
