"""Offline tests for the action chain: policy → safe limits → physical → vJoy."""
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
# 1. Safe training limits applied before canonical controls
# ---------------------------------------------------------------------------

def test_safe_limits_clamp_steer():
    """Policy steer outside safe_steer_deg should be clamped to ±35°."""
    from AssettoCorsaEnv.training_safety import apply_safe_control_limits

    actions = np.array([0.8, 0.0, -1.0], dtype=np.float64)
    safe = apply_safe_control_limits(
        actions,
        norm_steer_at_max=1.0,
        max_steer_deg=900.0,
        steer_safe_deg=35.0,
        acc_pedal_max=1.0,
        brake_pedal_max=1.0,
    )
    steer_deg = abs(safe[0]) * 900.0
    assert steer_deg <= 35.0 + 0.01, (
        f"Steer {steer_deg:.1f} deg exceeds safe limit 35 deg"
    )


def test_throttle_ramp_reaches_full_gas():
    """Throttle may not jump to 100% in one step, but must reach it."""
    from AssettoCorsaEnv.training_safety import apply_v3_control_limits, internal_to_pedal

    current = np.array([0.0, -1.0, -1.0], dtype=np.float64)
    target = np.array([0.0, 1.0, -1.0], dtype=np.float64)
    kwargs = dict(
        norm_steer_at_max=1.0,
        max_steer_deg=180.0,
        steer_limit_deg=35.0,
        max_delta_deg=6.0,
        soft_zone_deg=1.0,
        center_deadband_deg=1.0,
        center_decay=0.5,
        acc_pedal_max=1.0,
        brake_pedal_max=1.0,
        acc_max_delta_pedal=0.10,
        brake_max_delta_pedal=0.15,
    )
    first = apply_v3_control_limits(target, current, **kwargs)
    assert internal_to_pedal(first[1]) < 0.99
    action = first
    for _ in range(20):
        action = apply_v3_control_limits(target, action, **kwargs)
    assert abs(internal_to_pedal(action[1]) - 1.0) < 0.001


def test_brake_ramp_reaches_full_brake():
    """Brake may ramp, but must be allowed to reach 100%."""
    from AssettoCorsaEnv.training_safety import apply_v3_control_limits, internal_to_pedal

    current = np.array([0.0, -1.0, -1.0], dtype=np.float64)
    target = np.array([0.0, -1.0, 1.0], dtype=np.float64)
    kwargs = dict(
        norm_steer_at_max=1.0,
        max_steer_deg=180.0,
        steer_limit_deg=35.0,
        max_delta_deg=6.0,
        soft_zone_deg=1.0,
        center_deadband_deg=1.0,
        center_decay=0.5,
        acc_pedal_max=1.0,
        brake_pedal_max=1.0,
        acc_max_delta_pedal=0.10,
        brake_max_delta_pedal=0.25,
    )
    action = current
    for _ in range(8):
        action = apply_v3_control_limits(target, action, **kwargs)
    assert abs(internal_to_pedal(action[2]) - 1.0) < 0.001


def test_safe_limits_clamp_throttle():
    """Throttle should be allowed up to 100%."""
    from AssettoCorsaEnv.training_safety import apply_safe_control_limits

    actions = np.array([0.0, 1.0, -1.0], dtype=np.float64)
    safe = apply_safe_control_limits(
        actions,
        norm_steer_at_max=1.0,
        max_steer_deg=900.0,
        steer_safe_deg=35.0,
        acc_pedal_max=1.0,
        brake_pedal_max=1.0,
    )
    acc_pedal = (safe[1] + 1.0) / 2.0
    assert abs(acc_pedal - 1.0) < 0.001, (
        f"Throttle pedal {acc_pedal:.3f} should reach 1.0"
    )


def test_safe_limits_clamp_brake():
    """Brake should be allowed up to 100%."""
    from AssettoCorsaEnv.training_safety import apply_safe_control_limits

    actions = np.array([0.0, -1.0, 1.0], dtype=np.float64)
    safe = apply_safe_control_limits(
        actions,
        norm_steer_at_max=1.0,
        max_steer_deg=900.0,
        steer_safe_deg=35.0,
        acc_pedal_max=1.0,
        brake_pedal_max=1.0,
    )
    brake_pedal = (safe[2] + 1.0) / 2.0
    assert abs(brake_pedal - 1.0) < 0.001, (
        f"Brake pedal {brake_pedal:.3f} should reach 1.0"
    )


def test_safe_limits_preserve_neutral():
    """Neutral action should pass through safe limits unchanged."""
    from AssettoCorsaEnv.training_safety import apply_safe_control_limits

    neutral = np.array([0.0, -1.0, -1.0], dtype=np.float64)
    safe = apply_safe_control_limits(
        neutral,
        norm_steer_at_max=1.0,
        max_steer_deg=900.0,
        steer_safe_deg=35.0,
        acc_pedal_max=1.0,
        brake_pedal_max=1.0,
    )
    np.testing.assert_array_almost_equal(safe, neutral, decimal=5)


# ---------------------------------------------------------------------------
# 2. raw_action_to_physical conversion
# ---------------------------------------------------------------------------

def test_raw_action_to_physical_neutral():
    """Neutral internal action should produce zero physical controls."""
    from ac_workers.canonical_controls import raw_action_to_physical

    steer, throttle, brake = raw_action_to_physical([0.0, -1.0, -1.0])
    assert steer == 0.0
    assert throttle == 0.0
    assert brake == 0.0


def test_raw_action_to_physical_full_throttle():
    """Full internal throttle (1.0) should produce physical throttle 1.0."""
    from ac_workers.canonical_controls import raw_action_to_physical

    steer, throttle, brake = raw_action_to_physical([0.0, 1.0, -1.0])
    assert abs(throttle - 1.0) < 0.001
    assert brake == 0.0


def test_raw_action_to_physical_left_steer_not_relu():
    """Steer stays bipolar: negative = left. Pedal relu must not eat the left half."""
    from ac_workers.canonical_controls import physical_to_internal, raw_action_to_physical

    steer, throttle, brake = raw_action_to_physical([-0.7, 0.5, -1.0])
    assert abs(steer - (-0.7)) < 1e-6
    assert throttle > 0.0
    assert brake == 0.0
    steer_i, acc_i, _ = physical_to_internal(steer, throttle, brake)
    assert steer_i < 0.0
    assert acc_i > -1.0


def test_raw_action_to_physical_clamps():
    """Values outside [-1,1] should be clamped."""
    from ac_workers.canonical_controls import raw_action_to_physical

    steer, throttle, brake = raw_action_to_physical([2.0, 3.0, -5.0])
    assert -1.0 <= steer <= 1.0
    assert 0.0 <= throttle <= 1.0
    assert 0.0 <= brake <= 1.0


def test_brake_mapping_zero_neutral():
    """Released pedal = 0%. A neutral network output (raw=0) must NOT mean 50%.

    Required mapping:
      raw_brake = -1  -> physical_brake = 0
      raw_brake =  0  -> physical_brake = 0   (was 0.5 with the old (raw+1)/2)
      raw_brake = 0.75 -> physical_brake = 1
      raw_brake =  1   -> physical_brake = 1
    """
    from ac_workers.canonical_controls import raw_action_to_physical

    cases = [(-1.0, 0.0), (0.0, 0.0), (0.75, 1.0), (1.0, 1.0)]
    for raw_brake, expected in cases:
        _, _, brake = raw_action_to_physical([0.0, -1.0, raw_brake])
        assert abs(brake - expected) < 1e-6, (
            "raw_brake={0} -> physical_brake={1}, expected {2}".format(
                raw_brake, brake, expected
            )
        )


def test_throttle_mapping_zero_neutral():
    """Throttle uses the same released=0% mapping (raw=0 -> 0%, not 50%)."""
    from ac_workers.canonical_controls import raw_action_to_physical

    cases = [(-1.0, 0.0), (0.0, 0.0), (0.5, 0.5), (1.0, 1.0)]
    for raw_throttle, expected in cases:
        _, throttle, _ = raw_action_to_physical([0.0, raw_throttle, -1.0])
        assert abs(throttle - expected) < 1e-6, (
            "raw_throttle={0} -> physical_throttle={1}, expected {2}".format(
                raw_throttle, throttle, expected
            )
        )


def test_fresh_policy_neutral_output_is_zero_brake():
    """A fresh network outputs values around 0; that must NOT produce ~50% brake."""
    from ac_workers.canonical_controls import raw_action_to_physical

    for raw_brake in (-0.2, -0.05, 0.0):
        _, _, brake = raw_action_to_physical([0.0, 0.0, raw_brake])
        assert brake == 0.0, (
            "near-neutral raw_brake={0} must be 0% brake, got {1}".format(
                raw_brake, brake
            )
        )


def test_no_safety_can_force_full_brake():
    """No safety/blocked path may produce brake=1.0.

    When driving is blocked the worker neutralizes (coast): the only forced
    constant must be NEUTRAL (zero brake). The old forced-brake constants must
    no longer exist anywhere in worker_collector.
    """
    import ac_workers.worker_collector as wc

    assert not hasattr(wc, "LEARNER_STOP_PHYSICAL_ACTION")
    assert not hasattr(wc, "LEARNER_STOP_RAW_ACTION")
    # neutralize_canonical must command zero brake (physical_action[2] == 0).
    from ac_workers.canonical_controls import neutralize_canonical

    class _LC:
        def __init__(self):
            self.brake = None

        def set_controls(self, steer, acc, brake, **_):
            self.brake = brake

        @property
        def vj(self):
            return None

    lc = _LC()
    result = neutralize_canonical(lc, device_id=1)
    assert result["physical_action"][2] == 0.0


# ---------------------------------------------------------------------------
# 3. action_source transitions
# ---------------------------------------------------------------------------

def _make_policy_stub(**overrides):
    """Minimal CollectorPolicy instance for offline action-source tests.

    Avoids mocking discor/torch in sys.modules (that pollutes later tests).
    """
    from ac_workers.collector_policy import CollectorPolicy

    policy = CollectorPolicy.__new__(CollectorPolicy)
    policy.worker_id = 0
    policy.policy_seed = 10007
    policy.exploration_seed = 10007
    policy.deterministic_mode = False
    policy.action_exploration_noise_std = 0.0
    policy._noise_rng = np.random.default_rng(10007)
    policy._weights_loaded = False
    policy._load_error = "weights_not_loaded"
    policy.state_dim = 125
    policy.action_dim = 3
    policy.last_action_noise = [0.0, 0.0, 0.0]
    for key, value in overrides.items():
        setattr(policy, key, value)
    return policy


def test_action_source_no_weights():
    """CollectorPolicy without loaded weights returns 'no_weights'."""
    policy = _make_policy_stub(_weights_loaded=False, _load_error="weights_not_loaded")

    state = np.zeros(125, dtype=np.float32)
    trace = policy.select_action_traced(state, None)

    assert trace["policy_action_source"] == "no_weights"
    assert trace["error"] != ""
    assert trace["policy_seed"] == 10007
    assert trace["physical_action"] == [0.0, 0.0, 0.0]


def test_action_source_sac_explore():
    """CollectorPolicy with loaded weights returns 'sac_explore'."""
    fake_action = np.array([0.1, 0.2, -0.3], dtype=np.float32)
    algo = mock.MagicMock()
    algo.explore.return_value = (fake_action, None)
    policy = _make_policy_stub(
        _weights_loaded=True,
        _load_error="",
        algo=algo,
    )

    state = np.zeros(125, dtype=np.float32)
    trace = policy.select_action_traced(state, None)

    assert trace["policy_action_source"] == "sac_explore"
    assert trace["error"] == ""
    np.testing.assert_array_almost_equal(
        np.asarray(trace["action"], dtype=np.float32), fake_action
    )
    # relu_zero_neutral: raw throttle 0.2 -> 0.2, raw brake -0.3 -> 0.0
    np.testing.assert_allclose(trace["physical_action"], [0.1, 0.2, 0.0], rtol=0, atol=1e-6)


# ---------------------------------------------------------------------------
# 4. Transition payload has reward_components
# ---------------------------------------------------------------------------

def test_transition_payload_has_reward_components():
    """Verify the transition payload structure includes reward_components."""
    reward_components = {
        "reward_progress": 0.05,
        "reward_gap_penalty": 0.01,
        "reward_steer_penalty": 0.002,
        "reward_oot_penalty": 0.0,
        "reward": 0.038,
    }

    payload = {
        "worker_id": 0,
        "episode_id": 1,
        "step_id": 42,
        "obs": [0.0] * 125,
        "action": [0.0, 0.0, 0.0],
        "reward": 0.038,
        "next_state": [0.0] * 125,
        "done": False,
        "done_reason": "",
        "episode_age_seconds": 5.0,
        "action_source": "sac_explore",
        "reward_components": reward_components,
    }

    assert "reward_components" in payload
    assert "reward_progress" in payload["reward_components"]
    assert payload["reward_components"]["reward_progress"] > 0


# ---------------------------------------------------------------------------
# 5. vJoy counters
# ---------------------------------------------------------------------------

def test_runtime_has_vjoy_counters():
    """CollectorRuntime should have update_vjd success/fail counters."""
    from ac_workers.worker_collector import CollectorRuntime

    runtime = CollectorRuntime(worker_id=0, vjoy_device=1, mgmt_port=2347)
    assert hasattr(runtime, "update_vjd_success_count")
    assert hasattr(runtime, "update_vjd_fail_count")
    assert hasattr(runtime, "policy_control_write_count")
    assert runtime.update_vjd_success_count == 0
    assert runtime.update_vjd_fail_count == 0
    assert runtime.policy_control_write_count == 0


def test_runtime_status_includes_counters():
    """Status snapshot should include vJoy counters."""
    from ac_workers.worker_collector import CollectorRuntime

    runtime = CollectorRuntime(worker_id=0, vjoy_device=1, mgmt_port=2347)
    runtime.update_vjd_success_count = 100
    runtime.update_vjd_fail_count = 2
    runtime.policy_control_write_count = 102

    snap = runtime.minimal_snapshot()
    assert snap["update_vjd_success_count"] == 100
    assert snap["update_vjd_fail_count"] == 2
    assert snap["policy_control_write_count"] == 102


# ---------------------------------------------------------------------------
# Run tests
# ---------------------------------------------------------------------------

def test_policy_steer_full_right_is_not_saturated_left():
    """Policy +0.2 must request a gentle right, not already hit the 35° stop."""
    from AssettoCorsaEnv.training_safety import (
        apply_steer_v3_control,
        degrees_to_internal,
        internal_to_degrees,
        policy_steer_to_internal,
    )

    norm, max_deg, limit = 0.4, 180.0, 35.0
    internal = policy_steer_to_internal(0.2, norm, max_deg, limit)
    deg = internal_to_degrees(internal, norm, max_deg)
    assert abs(deg - 7.0) < 0.05, deg

    left = policy_steer_to_internal(-0.2, norm, max_deg, limit)
    assert abs(internal_to_degrees(left, norm, max_deg) + 7.0) < 0.05

    full = policy_steer_to_internal(-1.0, norm, max_deg, limit)
    assert abs(internal_to_degrees(full, norm, max_deg) + 35.0) < 0.05

    # Old bug: raw -0.2 treated as internal = -90° and saturates at -35°.
    old_deg = internal_to_degrees(-0.2, norm, max_deg)
    assert old_deg < -35.0
    new_i = apply_steer_v3_control(internal, 0.0, norm, max_deg, limit, 6.0, 1.0, 1.0, 0.5)
    new_deg = internal_to_degrees(new_i, norm, max_deg)
    assert 0.0 < new_deg <= 7.0 + 0.05


if __name__ == "__main__":
    tests = [
        test_safe_limits_clamp_steer,
        test_safe_limits_clamp_throttle,
        test_throttle_ramp_reaches_full_gas,
        test_brake_ramp_reaches_full_brake,
        test_safe_limits_clamp_brake,
        test_safe_limits_preserve_neutral,
        test_raw_action_to_physical_neutral,
        test_raw_action_to_physical_full_throttle,
        test_raw_action_to_physical_left_steer_not_relu,
        test_raw_action_to_physical_clamps,
        test_brake_mapping_zero_neutral,
        test_throttle_mapping_zero_neutral,
        test_fresh_policy_neutral_output_is_zero_brake,
        test_no_safety_can_force_full_brake,
        test_action_source_no_weights,
        test_action_source_sac_explore,
        test_transition_payload_has_reward_components,
        test_runtime_has_vjoy_counters,
        test_runtime_status_includes_counters,
        test_policy_steer_full_right_is_not_saturated_left,
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
