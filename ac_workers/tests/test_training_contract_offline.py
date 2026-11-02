"""Offline checks for the action contract, resume bundle, and signed progress."""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import torch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "assetto_corsa_gym"))
sys.path.insert(0, str(REPO / "algorithm" / "discor"))


def test_progress_reward_signed_and_rejects_teleport():
    from AssettoCorsaEnv.progress_reward import progress_reward

    prev = {
        "timestamp_ac": 1.0,
        "NormalizedSplinePosition": 0.10,
        "world_position_x": 0.0,
        "world_position_y": 0.0,
        "speed": 20.0,
        "done": False,
    }
    nxt = dict(prev)
    nxt["timestamp_ac"] = 1.04
    nxt["NormalizedSplinePosition"] = 0.101
    nxt["world_position_x"] = 1.0
    cfg = type("C", (), {})()
    cfg.reward_progress_per_m = 0.05
    cfg.reward_oot_terminal_penalty = 2.0
    cfg.reward_offtrack_per_step = 0.1
    cfg.reward_smoothness_coef = 0.0
    cfg.reward_step_cost = 0.0
    cfg.reward_idle_penalty = 0.0
    cfg.reward_idle_speed_kmh = 8.0
    cfg.reward_motion_per_kmh = 0.0
    good = progress_reward(prev, nxt, 1000.0, cfg, np.zeros(3))
    assert good["progress_valid"] is True
    assert good["signed_progress_m"] > 0
    assert good["reward"] > 0

    backward = dict(nxt)
    backward["NormalizedSplinePosition"] = 0.099
    backward["world_position_x"] = -1.0
    bad_dir = progress_reward(prev, backward, 1000.0, cfg, np.zeros(3))
    assert bad_dir["signed_progress_m"] < 0
    assert bad_dir["reward"] < 0

    teleport = dict(nxt)
    teleport["NormalizedSplinePosition"] = 0.80
    teleport["world_position_x"] = 400.0
    jumped = progress_reward(prev, teleport, 1000.0, cfg, np.zeros(3))
    assert jumped["progress_valid"] is False
    assert jumped["signed_progress_m"] == 0.0

    parked = {
        "timestamp_ac": 2.0,
        "NormalizedSplinePosition": 0.20,
        "world_position_x": 10.0,
        "world_position_y": 10.0,
        "speed": 0.0005,
        "yaw": 0.1,
        "numberOfTyresOut": 0,
        "done": False,
    }
    parked_jump = dict(parked)
    parked_jump["timestamp_ac"] = 2.067
    parked_jump["yaw"] = 0.1 + 1.64
    parked_jump["numberOfTyresOut"] = 4
    from AssettoCorsaEnv.telemetry_consistency import stationary_signal_inconsistent

    assert stationary_signal_inconsistent(parked, parked_jump) is True
    bad_parked = progress_reward(parked, parked_jump, 1000.0, cfg, np.zeros(3))
    assert bad_parked["progress_valid"] is False


def test_recovered_car_is_not_off_track():
    from AssettoCorsaEnv.progress_reward import is_currently_off_track

    assert is_currently_off_track(2.0, lap_invalidated=True) is False
    assert is_currently_off_track(10.0, lap_invalidated=True) is True
    assert is_currently_off_track(10.0, lap_invalidated=False) is False
    assert is_currently_off_track(13.0, lap_invalidated=False) is True


def test_idle_is_worse_than_rolling():
    from AssettoCorsaEnv.progress_reward import progress_reward

    cfg = type("C", (), {})()
    cfg.reward_progress_per_m = 0.0
    cfg.reward_oot_terminal_penalty = 0.0
    cfg.reward_offtrack_per_step = 0.0
    cfg.reward_smoothness_coef = 0.0
    cfg.reward_step_cost = 0.0
    cfg.reward_idle_penalty = 0.08
    cfg.reward_idle_speed_kmh = 8.0
    cfg.reward_motion_per_kmh = 0.003
    parked = {
        "timestamp_ac": 1.04,
        "NormalizedSplinePosition": 0.10,
        "world_position_x": 0.0,
        "world_position_y": 0.0,
        "speed": 0.2,
        "done": False,
        "out_of_track": False,
    }
    rolling = dict(parked)
    rolling["speed"] = 20.0
    still = progress_reward(dict(parked, timestamp_ac=1.0), parked, 1000.0, cfg, np.zeros(3))
    move = progress_reward(dict(rolling, timestamp_ac=1.0), rolling, 1000.0, cfg, np.zeros(3))
    assert abs(still["reward_idle_penalty"] - 0.08) < 1e-8
    assert still["reward"] < 0
    assert move["reward_idle_penalty"] == 0.0
    assert move["reward_motion"] == 0
    assert move["reward"] > still["reward"]


def test_clean_sector_and_lap_bonuses():
    from AssettoCorsaEnv.progress_reward import CleanLapTracker, progress_reward

    tracker = CleanLapTracker(
        n_sectors=4, sector_bonus=10.0, lap_bonus=100.0, lap_time_ref_sec=100.0,
    )
    cfg = type("C", (), {})()
    cfg.reward_progress_per_m = 0.0
    cfg.reward_oot_terminal_penalty = 0.0
    cfg.reward_offtrack_per_step = 0.0
    cfg.reward_smoothness_coef = 0.0
    cfg.reward_step_cost = 0.0
    track = 1000.0

    def step(frac, t, prev_frac, prev_t, oot=False, backwards=0.0):
        prev = {
            "timestamp_ac": prev_t,
            "NormalizedSplinePosition": prev_frac,
            "world_position_x": prev_frac * track,
            "world_position_y": 0.0,
            "speed": 40.0,
            "done": False,
        }
        nxt = {
            "timestamp_ac": t,
            "NormalizedSplinePosition": frac,
            "world_position_x": frac * track if frac >= prev_frac else (frac + 1.0) * track,
            "world_position_y": 0.0,
            "speed": 40.0,
            "done": False,
            "out_of_track": oot,
            "going_backwards": backwards,
        }
        return progress_reward(prev, nxt, track, cfg, np.zeros(3), tracker=tracker)

    # A sector must be driven, not inferred from a jump between two samples.
    first = step(0.001, 1.04, 0.0, 1.0)
    assert not first["completed_clean_sector"]
    for i in range(2, 251):
        crossed = step(i / 1000., 1.0 + i * .04, (i - 1) / 1000., 1.0 + (i - 1) * .04)
    assert crossed["completed_clean_sector"]
    assert crossed["reward_clean_sector"] == 10.0

    dirty = CleanLapTracker(n_sectors=4, sector_bonus=10.0, lap_bonus=100.0)
    dirty.update({"NormalizedSplinePosition": 0.10, "out_of_track": True}, 10.0, True, track)
    no_bonus = dirty.update({"NormalizedSplinePosition": 0.26, "out_of_track": False}, 10.0, True, track)
    assert no_bonus["completed_clean_sector"] is False
    assert no_bonus["reward_clean_sector"] == 0.0

    lap_tracker = CleanLapTracker(
        n_sectors=4, sector_bonus=10.0, lap_bonus=100.0, lap_time_ref_sec=100.0,
        min_lap_fraction=0.85,
    )
    lap_tracker.forward_m = 900.0
    lap_tracker.last_fraction = 0.98
    lap_tracker.current_sector = 3
    lap_tracker.lap_start_time = 10.0
    lap = lap_tracker.update(
        {"NormalizedSplinePosition": 0.02, "timestamp_ac": 90.0, "out_of_track": False},
        40.0, True, track,
    )
    assert lap["completed_clean_lap"] is True
    # 80s lap vs 100s ref -> 100 * (100/80) = 125
    assert abs(lap["reward_clean_lap"] - 125.0) < 1e-6

    faulted = CleanLapTracker(n_sectors=4, sector_bonus=10.0, lap_bonus=100.0)
    faulted.forward_m = 900.0
    faulted.last_fraction = 0.98
    faulted.current_sector = 3
    faulted.note_fault()
    denied = faulted.update(
        {"NormalizedSplinePosition": 0.02, "timestamp_ac": 90.0, "out_of_track": False},
        40.0, True, track,
    )
    assert denied["completed_clean_lap"] is False
    assert denied["reward_clean_lap"] == 0.0


def test_wrong_way_needs_confirm_metres():
    from AssettoCorsaEnv.progress_reward import update_wrong_way

    meters, going = update_wrong_way(0.0, -3.0, 8.0)
    assert going is False
    meters, going = update_wrong_way(meters, -6.0, 8.0)
    assert going is True
    meters, going = update_wrong_way(meters, 1.0, 8.0)
    assert going is False
    assert meters == -8.0
    meters, going = update_wrong_way(meters, 20.0, 8.0)
    assert going is False
    assert meters == 0.0


def test_replay_stores_policy_action_not_internal():
    from discor.replay_buffer import ReplayBuffer

    replay = ReplayBuffer(8, (2,), (3,), gamma=0.9, nstep=1)
    policy = np.array([0.8, 0.5, -0.2], dtype=np.float32)
    replay.append(np.zeros(2), policy, 0.1, np.ones(2), False, episode_done=False, stream_id=0)
    stored = replay._actions[0]
    np.testing.assert_allclose(stored, policy)
    assert abs(stored[0]) > 0.5  # not the Lotus internal 0.013 scale


def test_resume_waits_for_real_replay_fill():
    from discor.replay_buffer import ReplayBuffer

    replay = ReplayBuffer(64, (1,), (1,), gamma=0.99, nstep=1)
    replay.append(np.zeros(1), np.zeros(1), 0.0, np.zeros(1), False)
    # One sample must not be enough for a batch of 64.
    assert len(replay) < 64
    idx = replay._sample_idxes(4)
    assert idx.max() < len(replay)


def test_checkpoint_roundtrip(tmp_path: Path | None = None):
    from ac_workers.training_checkpoint import capture, restore, save_atomic
    from discor.algorithm import SAC
    from discor.replay_buffer import ReplayBuffer
    from omegaconf import OmegaConf

    class Fake:
        pass

    trainer = Fake()
    trainer.config = OmegaConf.create({"SAC": {"gamma": 0.99}, "AssettoCorsa": {}, "seed": 0, "Agent": {}})
    trainer.algo = SAC(2, 3, torch.device("cpu"), gamma=0.99, nstep=1)
    trainer.replay = ReplayBuffer(16, (2,), (3,), gamma=0.99, nstep=1)
    trainer.replay.append(np.zeros(2), np.array([0.2, 0.1, 0.0]), 0.3, np.ones(2), False)
    trainer.steps = 12
    trainer.updates = 7
    trainer._policy_version = 12
    path = (tmp_path or Path(".")) / "training_state.pt"
    if tmp_path is None:
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "training_state.pt"
    save_atomic(path, capture(trainer))
    trainer2 = Fake()
    trainer2.config = trainer.config
    trainer2.algo = SAC(2, 3, torch.device("cpu"), gamma=0.99, nstep=1)
    trainer2.replay = ReplayBuffer(16, (2,), (3,), gamma=0.99, nstep=1)
    restore(trainer2, path)
    assert trainer2.steps == 12
    assert trainer2.updates == 7
    assert len(trainer2.replay) == 1
    assert math.isclose(float(trainer2.algo._log_alpha.item()), float(trainer.algo._log_alpha.item()))


def main() -> int:
    import tempfile

    test_progress_reward_signed_and_rejects_teleport()
    print("OK progress")
    test_recovered_car_is_not_off_track()
    print("OK oot latch")
    test_idle_is_worse_than_rolling()
    print("OK idle")
    test_clean_sector_and_lap_bonuses()
    print("OK milestones")
    test_wrong_way_needs_confirm_metres()
    print("OK wrong-way")
    test_replay_stores_policy_action_not_internal()
    print("OK action contract")
    test_resume_waits_for_real_replay_fill()
    print("OK replay gate")
    with tempfile.TemporaryDirectory() as tmp:
        test_checkpoint_roundtrip(Path(tmp))
    print("OK checkpoint")
    print("Test-TrainingContract-Offline: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
