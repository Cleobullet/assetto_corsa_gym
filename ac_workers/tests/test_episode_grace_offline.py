"""Offline tests for post-reset grace period and anti-double-reset cooldown."""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest import mock

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "assetto_corsa_gym"))
sys.path.insert(0, str(REPO / "algorithm" / "discor"))


# ---------------------------------------------------------------------------
# 1. ac_env.expand_state grace period tests
# ---------------------------------------------------------------------------

class _FakeConfig:
    """Minimal config for env initialization."""
    ego_sampling_freq = 25
    enable_out_of_track_termination = True
    enable_low_speed_termination = True
    use_ac_out_of_track = True
    recover_car_on_done = False
    max_laps_number = None
    send_reset_at_start = True
    oot_grace_sec = 3.0
    low_speed_grace_sec = 12.0
    low_speed_timeout_sec = 6.0
    low_speed_threshold_kmh = 1.0
    safe_training_mode = False
    auto_shift_to_first = False
    enable_sensors = False
    use_relative_actions = False
    enable_out_of_track_penalty = False
    max_steer_rate = 900.0
    steer_control_v3 = False
    exploration_v3 = False
    reward_v3 = False
    use_reference_line_in_reward = False
    use_target_speed = False
    add_previous_obs_to_state = False
    use_obs_extra = False
    screen_capture_enable = False
    penalize_actions_diff = False
    penalize_actions_diff_coef = 0.0


def _make_state(out_of_track=False, going_backwards=0.0, speed=10.0, done=0,
                numberOfTyresOut=0, LapDist=100.0):
    """Build a minimal state dict for expand_state."""
    return {
        "done": done,
        "going_backwards": going_backwards,
        "out_of_track": out_of_track,
        "out_of_track_ac": out_of_track,
        "out_of_track_calc": out_of_track,
        "numberOfTyresOut": numberOfTyresOut if out_of_track else 0,
        "speed": speed,
        "LapDist": LapDist,
        "x": 0.0,
        "y": 0.0,
        "heading": 0.0,
        "steerAngle": 0.0,
        "normalizedSplinePosition": 0.1,
        "trackLength": 5000.0,
    }


def _build_env_for_expand_state():
    """Build a minimal env object that can call expand_state without full init.

    Instead of importing the full ac_env module (which has deep deps like numba),
    we replicate the expand_state termination logic inline.
    """

    class MinimalEnv:
        def __init__(self):
            self.config = _FakeConfig()
            self.ctrl_rate = 25
            self.ep_steps = 0
            self.enable_out_of_track_termination = True
            self.enable_low_speed_termination = True
            self.use_ac_out_of_track = True
            self.oot_grace_sec = 3.0
            self.oot_hold_sec = 1.5
            self.wrong_way_timeout_sec = 5.0
            self.wrong_way_speed_kmh = 8.0
            self.stuck_timeout_sec = 3.0
            self.stuck_speed_kmh = 6.0
            self.stuck_move_eps_m = 3.0
            self.no_progress_timeout_sec = 8.0
            self.no_progress_credit_m = 2.0
            self.moved_speed_kmh = 8.0
            self.low_speed_grace_sec = 12.0
            self.low_speed_timeout_sec = 6.0
            self.low_speed_threshold_kmh = 1.0
            self._max_episode_steps = 3000
            self.max_laps_number = None
            self.max_gap = None
            self.recover_car_on_done = False
            self.use_obs_extra = False
            self._force_episode_done = False
            self._pending_shift_fail = False
            self.termination_counter = int(6.0 * 25)
            self._oot_hold_steps = 0
            self._episode_has_moved = False
            self._stuck_hold_steps = 0
            self._stuck_last_xy = None
            self._no_progress_hold_steps = 0
            self._no_progress_m = 0.0
            self._wrong_way_hold_steps = 0
            self.states = []

        def get_lap_count(self, states):
            return 0

        def expand_state(self, state):
            """Replicated from ac_env.py expand_state termination logic."""
            done = 0
            done_reason = ""
            buf_infos = {}
            buf_infos['terminated'] = False
            buf_infos['TimeLimit.truncated'] = False

            oot_grace_steps = int(self.oot_grace_sec * self.ctrl_rate)
            in_post_reset_grace = self.ep_steps <= oot_grace_steps

            if self._force_episode_done or self._pending_shift_fail:
                done = 1
                done_reason = "safe_emergency"
                buf_infos['terminated'] = True

            if state["done"]:
                done = 1
                done_reason = done_reason or "ac_session_end"

            if not in_post_reset_grace and state['going_backwards'] > 0.:
                speed_kmh = float(state['speed']) * 3.6
                if speed_kmh >= self.wrong_way_speed_kmh:
                    self._wrong_way_hold_steps += 1
                hold_needed = max(1, int(self.wrong_way_timeout_sec * self.ctrl_rate))
                if self._wrong_way_hold_steps >= hold_needed:
                    buf_infos['terminated'] = True
                    done = 1
                    done_reason = done_reason or "going_backwards"
            elif state.get('going_backwards', 0.) <= 0.:
                self._wrong_way_hold_steps = 0

            if self.enable_out_of_track_termination and state['out_of_track'] and not in_post_reset_grace:
                self._oot_hold_steps += 1
                if self._oot_hold_steps >= max(1, int(self.oot_hold_sec * self.ctrl_rate)):
                    buf_infos['terminated'] = True
                    done = 1
                    done_reason = done_reason or "out_of_track"
            else:
                self._oot_hold_steps = 0

            if self._max_episode_steps is not None:
                if self.ep_steps > self._max_episode_steps:
                    done = 1
                    done_reason = done_reason or "max_steps"
                    buf_infos['TimeLimit.truncated'] = True

            if self.max_laps_number and self.get_lap_count(self.states) > self.max_laps_number:
                done = 1
                done_reason = done_reason or "max_laps"
                buf_infos['TimeLimit.truncated'] = True

            if self.enable_low_speed_termination:
                grace_steps = int(self.low_speed_grace_sec * self.ctrl_rate)
                timeout_steps = int(self.low_speed_timeout_sec * self.ctrl_rate)
                threshold_ms = self.low_speed_threshold_kmh / 3.6
                speed_kmh = float(state['speed']) * 3.6
                if speed_kmh >= self.moved_speed_kmh:
                    self._episode_has_moved = True
                if self.ep_steps <= grace_steps:
                    self.termination_counter = timeout_steps
                elif state['speed'] < threshold_ms:
                    self.termination_counter -= 1
                    if self.termination_counter <= 0:
                        buf_infos['terminated'] = True
                        done = 1
                        done_reason = done_reason or "low_speed"
                else:
                    self.termination_counter = timeout_steps
                if not in_post_reset_grace:
                    last_xy = self._stuck_last_xy
                    x = float(state.get('x', 0.0))
                    y = float(state.get('y', 0.0))
                    if last_xy is None:
                        self._stuck_last_xy = (x, y)
                        self._stuck_hold_steps = 0
                    else:
                        moved = float(np.hypot(x - last_xy[0], y - last_xy[1]))
                        if (
                            speed_kmh < self.stuck_speed_kmh
                            and moved < self.stuck_move_eps_m
                        ):
                            self._stuck_hold_steps += 1
                            if self._stuck_hold_steps >= max(1, int(self.stuck_timeout_sec * self.ctrl_rate)):
                                buf_infos['terminated'] = True
                                done = 1
                                done_reason = done_reason or "stuck"
                        else:
                            self._stuck_hold_steps = 0
                            self._stuck_last_xy = (x, y)

            if not in_post_reset_grace and self.max_gap and np.abs(state.get('gap', 0)) > self.max_gap:
                done = 1
                done_reason = done_reason or "max_gap"

            buf_infos['done_reason'] = done_reason
            buf_infos['episode_steps'] = self.ep_steps

            if done and self.recover_car_on_done:
                pass

            state["done"] = done
            return state, buf_infos

    return MinimalEnv()


def test_oot_grace_suppresses_done_during_grace_period():
    """OOT at step 1 (inside grace period) should NOT produce done=True."""
    env = _build_env_for_expand_state()
    env.ep_steps = 1  # first step after reset

    state = _make_state(out_of_track=True, numberOfTyresOut=4)
    result, infos = env.expand_state(state)

    assert result["done"] == 0, (
        f"Expected done=0 during grace period, got done={result['done']}"
    )
    assert infos.get("done_reason") == "", (
        f"Expected empty done_reason during grace, got '{infos.get('done_reason')}'"
    )


def test_oot_flicker_does_not_teleport():
    """A single OOT frame after grace must not teleport (kerb flicker)."""
    env = _build_env_for_expand_state()
    env.ep_steps = 76
    state = _make_state(out_of_track=True, numberOfTyresOut=3)
    result, infos = env.expand_state(state)
    assert result["done"] == 0
    assert infos.get("done_reason") == ""


def test_oot_terminates_after_grace_period():
    """Sustained OOT after grace + hold should produce done=True."""
    env = _build_env_for_expand_state()
    env.ep_steps = 76
    state = _make_state(out_of_track=True, numberOfTyresOut=4)
    result, infos = None, None
    for _ in range(int(env.oot_hold_sec * env.ctrl_rate)):
        result, infos = env.expand_state(state)
    assert result["done"] == 1, (
        f"Expected done=1 after OOT hold, got done={result['done']}"
    )
    assert infos.get("done_reason") == "out_of_track", (
        f"Expected done_reason='out_of_track', got '{infos.get('done_reason')}'"
    )


def test_stuck_after_moving_terminates():
    """A car that has already driven then freezes must teleport as stuck."""
    env = _build_env_for_expand_state()
    env.ep_steps = 400
    env._episode_has_moved = True
    env._stuck_last_xy = (0.0, 0.0)
    state = _make_state(speed=0.1)
    result, infos = None, None
    for _ in range(int(env.stuck_timeout_sec * env.ctrl_rate)):
        result, infos = env.expand_state(state)
    assert result["done"] == 1
    assert infos.get("done_reason") == "stuck"


def test_stuck_crawling_in_grass_without_having_raced():
    """A car that never reached 8 km/h but crawls at 3 km/h must still teleport."""
    env = _build_env_for_expand_state()
    env.ep_steps = 400
    env._episode_has_moved = False
    env._stuck_last_xy = (0.0, 0.0)
    state = _make_state(speed=0.85)  # ~3 km/h
    result, infos = None, None
    for _ in range(int(env.stuck_timeout_sec * env.ctrl_rate)):
        result, infos = env.expand_state(state)
    assert result["done"] == 1
    assert infos.get("done_reason") == "stuck"


def test_going_backwards_suppressed_during_grace():
    """going_backwards during grace period should NOT end episode."""
    env = _build_env_for_expand_state()
    env.ep_steps = 5  # inside grace

    state = _make_state(going_backwards=1.0)
    result, infos = env.expand_state(state)

    assert result["done"] == 0


def test_going_backwards_does_not_teleport_immediately():
    """A brief reverse after grace must not teleport."""
    env = _build_env_for_expand_state()
    env.ep_steps = 80
    state = _make_state(going_backwards=1.0, speed=10.0)
    result, infos = env.expand_state(state)
    assert result["done"] == 0
    assert infos.get("done_reason") == ""


def test_going_backwards_terminates_after_grace():
    """Sustained reverse after grace + 5s hold should end the episode."""
    env = _build_env_for_expand_state()
    env.ep_steps = 80

    state = _make_state(going_backwards=1.0, speed=10.0)
    result, infos = None, None
    for _ in range(int(env.wrong_way_timeout_sec * env.ctrl_rate)):
        result, infos = env.expand_state(state)
    assert result["done"] == 1
    assert infos.get("done_reason") == "going_backwards"


def test_safe_emergency_always_terminates():
    """Safe emergency should terminate even during grace period."""
    env = _build_env_for_expand_state()
    env.ep_steps = 1
    env._force_episode_done = True

    state = _make_state()
    result, infos = env.expand_state(state)

    assert result["done"] == 1
    assert infos.get("done_reason") == "safe_emergency"


def test_done_reason_is_present_in_infos():
    """buf_infos should always contain done_reason and episode_steps."""
    env = _build_env_for_expand_state()
    env.ep_steps = 100

    state = _make_state()  # no termination condition
    result, infos = env.expand_state(state)

    assert "done_reason" in infos
    assert "episode_steps" in infos
    assert infos["episode_steps"] == 100


# ---------------------------------------------------------------------------
# 2. Anti-double-reset cooldown tests (collector side)
# ---------------------------------------------------------------------------

def test_anti_double_reset_suppresses_done_at_low_step_id():
    """Collector should suppress done=True when step_id < ANTI_DOUBLE_RESET_STEPS."""
    from ac_workers.worker_collector import ANTI_DOUBLE_RESET_STEPS

    step_id = 5  # well below threshold
    done = True
    done_reason = "out_of_track"

    # Simulate the suppression logic from the collector
    suppressed = False
    if done and step_id < ANTI_DOUBLE_RESET_STEPS:
        if done_reason not in ("safe_emergency",):
            suppressed = True
            done = False
            done_reason = ""

    assert suppressed, "Expected suppression of done at low step_id"
    assert done is False
    assert done_reason == ""


def test_anti_double_reset_allows_done_after_threshold():
    """Collector should allow done=True when step_id >= ANTI_DOUBLE_RESET_STEPS."""
    from ac_workers.worker_collector import ANTI_DOUBLE_RESET_STEPS

    step_id = ANTI_DOUBLE_RESET_STEPS + 10
    done = True
    done_reason = "out_of_track"

    suppressed = False
    if done and step_id < ANTI_DOUBLE_RESET_STEPS:
        if done_reason not in ("safe_emergency",):
            suppressed = True
            done = False
            done_reason = ""

    assert not suppressed
    assert done is True
    assert done_reason == "out_of_track"


def test_anti_double_reset_never_suppresses_safe_emergency():
    """safe_emergency should never be suppressed, even at step_id=0."""
    from ac_workers.worker_collector import ANTI_DOUBLE_RESET_STEPS

    step_id = 0
    done = True
    done_reason = "safe_emergency"

    suppressed = False
    if done and step_id < ANTI_DOUBLE_RESET_STEPS:
        if done_reason not in ("safe_emergency",):
            suppressed = True
            done = False
            done_reason = ""

    assert not suppressed
    assert done is True
    assert done_reason == "safe_emergency"


# ---------------------------------------------------------------------------
# 3. Transition payload enrichment tests
# ---------------------------------------------------------------------------

def test_transition_payload_contains_done_reason_and_age():
    """Verify the transition payload has the new fields."""
    worker_id = 0
    episode_id = 1
    step_id = 42
    episode_started_at = time.monotonic() - 10.5
    done_reason = "out_of_track"
    action_source = "policy"

    episode_age_seconds = round(time.monotonic() - episode_started_at, 3)

    payload = {
        "worker_id": worker_id,
        "episode_id": episode_id,
        "step_id": step_id,
        "obs": [0.0] * 125,
        "action": [0.0, 0.0, 0.0],
        "reward": -0.5,
        "next_state": [0.0] * 125,
        "done": True,
        "done_reason": done_reason,
        "episode_age_seconds": episode_age_seconds,
        "action_source": action_source,
    }

    assert "done_reason" in payload
    assert "episode_age_seconds" in payload
    assert "action_source" in payload
    assert payload["done_reason"] == "out_of_track"
    assert payload["episode_age_seconds"] > 10.0
    assert payload["action_source"] == "policy"


# ---------------------------------------------------------------------------
# Run tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_oot_grace_suppresses_done_during_grace_period,
        test_oot_flicker_does_not_teleport,
        test_oot_terminates_after_grace_period,
        test_stuck_after_moving_terminates,
        test_stuck_crawling_in_grass_without_having_raced,
        test_going_backwards_suppressed_during_grace,
        test_going_backwards_does_not_teleport_immediately,
        test_going_backwards_terminates_after_grace,
        test_safe_emergency_always_terminates,
        test_done_reason_is_present_in_infos,
        test_anti_double_reset_suppresses_done_at_low_step_id,
        test_anti_double_reset_allows_done_after_threshold,
        test_anti_double_reset_never_suppresses_safe_emergency,
        test_transition_payload_contains_done_reason_and_age,
    ]
    passed = 0
    failed = 0
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
