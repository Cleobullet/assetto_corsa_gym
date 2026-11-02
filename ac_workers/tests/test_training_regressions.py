"""Regression checks for detached diagnostics and interleaved worker replay."""
import sys
import threading
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "algorithm" / "discor")]


def test_closed_launcher_stderr():
    from ac_workers import collector_diag as cd
    broken = mock.Mock()
    broken.write.side_effect = OSError(22, "Invalid argument")
    with mock.patch.object(cd.sys, "stderr", broken), mock.patch.object(cd, "DIAG_BASE_PORT", 24900):
        server = cd.CollectorDiagServer(90, lambda: {}, lambda: {}, lambda: {}, threading.RLock())
        server.start()
        try:
            assert server.wait_until_listening(3)
            assert cd.CollectorDiagClient(90).request("ping")["ok"]
            assert cd.CollectorDiagClient(90).request("status")["ok"]
        finally:
            server.stop()


def test_interleaved_replay():
    from discor.replay_buffer import ReplayBuffer
    replay = ReplayBuffer(20, (1,), (1,), gamma=0.5, nstep=3)
    for step in range(3):
        for worker, reward in ((0, 1), (1, 100)):
            replay.append([worker * 10 + step], [0], reward,
                          [worker * 10 + step + 1], False, stream_id=worker)
    assert len(replay) == 2
    assert list(replay._rewards[:2, 0]) == [1.75, 175.0]
    assert list(replay._next_states[:2, 0]) == [3, 13]
    replay.append([3], [0], 1, [4], True, stream_id=0)
    assert len(replay) == 5
    assert len(replay._nstep_streams[1]) == 2


def test_first_batch_is_not_discarded():
    from ac_workers.multi_sac_trainer import MultiSacTrainer
    trainer = mock.Mock()
    trainer.steps = 0
    trainer._stop_requested.return_value = False
    client = mock.Mock()
    batch = [{"worker_id": worker} for worker in range(6)]
    client.pull_batch.return_value = batch
    MultiSacTrainer._wait_first_transition(trainer, client, timeout_sec=1)
    assert trainer.steps == 6
    assert trainer._ingest_transition.call_args_list == [mock.call(tr) for tr in batch]


def test_resolver_waits_for_reported_worker():
    from ac_workers import collector_pid as cp
    evidence = [(True, 100, "initializing", {}),
                (True, 200, "ok", {"status_sid": "test", "status_pid": 200})]
    with mock.patch.object(cp, "collector_evidence", side_effect=evidence):
        pid, reason = cp.resolve_real_collector_pid(0, "test", 100, poll_sec=0)
    assert (pid, reason) == (200, "ok")


def test_idle_observation_is_ready():
    from ac_workers.collector_observation import assess_observation
    result = assess_observation([0] * 125, phase="idle", telemetry_ready=True,
                                telemetry_age_ms=10, telemetry_snapshot={"speed": 0})
    assert result["observation_complete"]
    stale = assess_observation([0] * 125, phase="idle", telemetry_ready=True,
                               telemetry_age_ms=3000, telemetry_snapshot={"speed": 0})
    assert not stale["observation_complete"]


def test_policy_version_does_not_go_backwards():
    from ac_workers.multi_sac_trainer import MultiSacTrainer
    trainer = mock.Mock()
    trainer.replay = mock.MagicMock()
    trainer.replay.__len__.return_value = 1
    trainer.training_started = True
    trainer.steps, trainer.start_steps, trainer.update_interval = 16001, 1500, 1
    trainer.updates, trainer._updates_since_last_publish = 14493, 0
    trainer._policy_version = 16000
    MultiSacTrainer._maybe_update(trainer)
    assert trainer._policy_version == 16001


def test_compact_observation_status():
    from ac_workers.collector_status import observation_complete
    assert observation_complete({"observation_complete": True, "observation_length": 125,
                                 "telemetry_summary": {"speed": 0}})
    assert not observation_complete({"observation_complete": True, "observation_length": 12,
                                     "telemetry_summary": {"speed": 0}})


if __name__ == "__main__":
    test_closed_launcher_stderr()
    test_interleaved_replay()
    test_first_batch_is_not_discarded()
    test_resolver_waits_for_reported_worker()
    test_idle_observation_is_ready()
    test_policy_version_does_not_go_backwards()
    test_compact_observation_status()
    print("Training regressions: OK")
