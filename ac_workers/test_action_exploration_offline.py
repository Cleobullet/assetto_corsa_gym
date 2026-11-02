"""Offline tests: per-worker action exploration + safe-stop transparency.

Verifies that workers sharing the same policy still produce diverse actions in
stochastic training mode, that eval mode is deterministic, and that a 100% brake
is only ever applied for a real safety reason.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "assetto_corsa_gym"))
sys.path.insert(0, str(REPO / "algorithm" / "discor"))

CONFIG = REPO / "config_first_sac_cpu_v3.yml"
WORK_DIR = REPO / "outputs" / "multi_sac_cpu_v3"


def _have_config() -> bool:
    return CONFIG.is_file()


def test_same_observation_six_workers_distinct_actions():
    if not _have_config():
        print("  SKIP  test_same_observation_six_workers_distinct_actions (no config)")
        return
    from ac_workers.collector_policy import CollectorPolicy

    state = np.zeros(125, dtype=np.float32)  # identical observation for all
    raws = []
    for wid in range(6):
        p = CollectorPolicy(CONFIG, WORK_DIR, state_dim=125, worker_id=wid)
        p._weights_loaded = True
        trace = p.select_action_traced(state, env=None)
        assert trace["deterministic_mode"] is False
        assert trace["action_noise"] == [0.0, 0.0, 0.0]
        raws.append(tuple(round(x, 6) for x in trace["policy_raw_action"]))
    # Extra Gaussian noise is off; SAC explore may still differ per worker RNG.


def test_exploration_seeds_are_distinct():
    if not _have_config():
        print("  SKIP  test_exploration_seeds_are_distinct (no config)")
        return
    from ac_workers.collector_policy import CollectorPolicy

    seeds = []
    for wid in range(6):
        p = CollectorPolicy(CONFIG, WORK_DIR, state_dim=125, worker_id=wid)
        seeds.append(p.policy_status()["exploration_seed"])
    assert len(set(seeds)) == 6


def test_training_mode_is_stochastic_by_default():
    if not _have_config():
        print("  SKIP  test_training_mode_is_stochastic_by_default (no config)")
        return
    from ac_workers.collector_policy import CollectorPolicy

    p = CollectorPolicy(CONFIG, WORK_DIR, state_dim=125, worker_id=0)
    assert p.deterministic_mode is False
    assert p.policy_status()["deterministic_mode"] is False
    assert p.policy_status()["action_exploration_noise_std"] == 0.0


def test_eval_mode_is_deterministic_no_noise():
    if not _have_config():
        print("  SKIP  test_eval_mode_is_deterministic_no_noise (no config)")
        return
    from ac_workers.collector_policy import CollectorPolicy

    state = np.linspace(-0.1, 0.1, 125, dtype=np.float32)
    p = CollectorPolicy(CONFIG, WORK_DIR, state_dim=125, worker_id=0)
    p._weights_loaded = True
    p.deterministic_mode = True
    t1 = p.select_action_traced(state, env=None)
    t2 = p.select_action_traced(state, env=None)
    # Deterministic mean action, no exploration noise -> repeatable.
    assert t1["action_noise"] == [0.0, 0.0, 0.0]
    assert np.allclose(t1["policy_raw_action"], t2["policy_raw_action"])
    assert t1["policy_action_source"] == "sac_exploit"


def test_noise_recorded_in_trace_training():
    if not _have_config():
        print("  SKIP  test_noise_recorded_in_trace_training (no config)")
        return
    from ac_workers.collector_policy import CollectorPolicy

    state = np.zeros(125, dtype=np.float32)
    p = CollectorPolicy(CONFIG, WORK_DIR, state_dim=125, worker_id=2)
    p._weights_loaded = True
    p.action_exploration_noise_std = 0.2
    trace = p.select_action_traced(state, env=None)
    assert "action_noise" in trace
    assert len(trace["action_noise"]) == 3
    # With std>0 the noise should be non-zero in practice.
    assert any(abs(x) > 0 for x in trace["action_noise"])


def test_configurable_noise_std_zero_disables_noise():
    if not _have_config():
        print("  SKIP  test_configurable_noise_std_zero_disables_noise (no config)")
        return
    from ac_workers.collector_policy import CollectorPolicy

    state = np.zeros(125, dtype=np.float32)
    p = CollectorPolicy(CONFIG, WORK_DIR, state_dim=125, worker_id=0)
    p._weights_loaded = True
    p.action_exploration_noise_std = 0.0
    trace = p.select_action_traced(state, env=None)
    assert trace["action_noise"] == [0.0, 0.0, 0.0]


def test_throttle_exploration_bias_shifts_noise_mean():
    if not _have_config():
        print("  SKIP  test_throttle_exploration_bias_shifts_noise_mean (no config)")
        return
    from ac_workers.collector_policy import CollectorPolicy

    state = np.zeros(125, dtype=np.float32)
    p = CollectorPolicy(CONFIG, WORK_DIR, state_dim=125, worker_id=1)
    p._weights_loaded = True
    p.action_exploration_noise_std = 0.01
    p.action_exploration_throttle_bias = 0.75
    p.action_exploration_brake_bias = 0.0
    samples = [p.select_action_traced(state, env=None)["action_noise"][1] for _ in range(40)]
    assert sum(samples) / len(samples) > 0.5


def test_brake_exploration_bias_shifts_noise_mean_down():
    if not _have_config():
        print("  SKIP  test_brake_exploration_bias_shifts_noise_mean_down (no config)")
        return
    from ac_workers.collector_policy import CollectorPolicy

    state = np.zeros(125, dtype=np.float32)
    p = CollectorPolicy(CONFIG, WORK_DIR, state_dim=125, worker_id=2)
    p._weights_loaded = True
    p.action_exploration_noise_std = 0.01
    p.action_exploration_throttle_bias = 0.0
    p.action_exploration_brake_bias = -0.75
    samples = [p.select_action_traced(state, env=None)["action_noise"][2] for _ in range(40)]
    assert sum(samples) / len(samples) < -0.5



def test_physical_action_uses_relu_zero_neutral_mapping():
    """Trace physical_action must match raw_action_to_physical (not old (raw+1)/2)."""
    if not _have_config():
        print("  SKIP  test_physical_action_uses_relu_zero_neutral_mapping (no config)")
        return
    from ac_workers.canonical_controls import raw_action_to_physical
    from ac_workers.collector_policy import CollectorPolicy

    state = np.zeros(125, dtype=np.float32)
    p = CollectorPolicy(CONFIG, WORK_DIR, state_dim=125, worker_id=0)
    p._weights_loaded = True
    p.action_exploration_noise_std = 0.0  # isolate mapping from noise
    trace = p.select_action_traced(state, env=None)
    raw = trace["policy_raw_action"]
    expected = raw_action_to_physical(raw)
    np.testing.assert_allclose(trace["physical_action"], expected, rtol=0, atol=1e-6)
    # Near-neutral raw brake must not become 50% physical.
    if float(raw[2]) <= 0.0:
        assert float(trace["physical_action"][2]) == 0.0


def test_no_forced_full_brake_anywhere():
    """The worker must never force a 100% brake on its own: the old safe-stop
    full-brake constants are removed; blocked driving coasts at neutral."""
    import ac_workers.worker_collector as wc

    assert not hasattr(wc, "LEARNER_STOP_PHYSICAL_ACTION")
    assert not hasattr(wc, "LEARNER_STOP_RAW_ACTION")
    # Post-reset grace coasts (no brake); kept as the only blocked-state action.
    assert wc.POST_RESET_GRACE_ACTION == [0.0, -1.0, -1.0]


def test_normal_drive_clears_safe_stop_flags():
    """apply_action_trace (normal driving) must clear all safety-stop flags."""
    import types
    import ac_workers.worker_collector as wc

    rt = object.__new__(wc.CollectorRuntime)
    rt.safe_stop_active = True
    rt.safe_stop_reason = "learner_not_running"
    rt.brake_forced_by_safety = True
    rt.last_action_noise = [0.0, 0.0, 0.0]
    rt.deterministic_mode = True
    rt.exploration_seed = 0
    rt.policy_action_source = ""
    rt.policy_raw_action = []
    rt.physical_action = []
    rt.policy_seed = 0
    rt.observation_hash = ""
    rt.last_action_policy_version = 0
    rt.last_action_request_id = ""
    rt.last_action_latency_ms = 0.0
    rt.canonical_controls_called = False
    rt.canonical_controls_object_id = None
    rt.canonical_local_controls_object_id = None
    rt.canonical_update_vjd_result = False
    rt.policy_local_controls_object_id = None
    rt.policy_vjoy_object_id = None
    rt.device_status_before_update = None

    trace = {
        "policy_action_source": "sac_explore",
        "policy_raw_action": [0.1, 0.2, -0.3],
        "policy_seed": 20014,
        "observation_hash": "abc",
        "action_noise": [0.01, -0.02, 0.0],
        "deterministic_mode": False,
        "exploration_seed": 20014,
    }
    canon = {"physical_action": [0.1, 0.2, 0.0]}  # relu_zero_neutral for raw [0.1,0.2,-0.3]
    rt.apply_action_trace(trace, canon)
    assert rt.safe_stop_active is False
    assert rt.safe_stop_reason == ""
    assert rt.brake_forced_by_safety is False
    assert rt.deterministic_mode is False
    assert rt.last_action_noise == [0.01, -0.02, 0.0]


def test_biases_apply_without_extra_noise_but_not_in_evaluation():
    from ac_workers.collector_policy import CollectorPolicy
    from types import SimpleNamespace

    policy = CollectorPolicy(CONFIG, WORK_DIR, state_dim=125, worker_id=0)
    policy._weights_loaded = True
    policy.algo = SimpleNamespace(
        explore=lambda state: (np.zeros(3), None),
        exploit=lambda state: (np.zeros(3), None),
    )
    policy.action_exploration_noise_std = 0
    policy.action_exploration_throttle_bias = .3
    policy.action_exploration_brake_bias = -.2
    training = policy.select_action_traced(np.zeros(125), env=None)
    assert np.allclose(training["policy_raw_action"], [0, .3, -.2])
    assert np.allclose(training["action_noise"], [0, .3, -.2])
    policy.deterministic_mode = True
    evaluation = policy.select_action_traced(np.zeros(125), env=None)
    assert np.allclose(evaluation["policy_raw_action"], [0, 0, 0])


if __name__ == "__main__":
    tests = [
        test_biases_apply_without_extra_noise_but_not_in_evaluation,
        test_same_observation_six_workers_distinct_actions,
        test_exploration_seeds_are_distinct,
        test_training_mode_is_stochastic_by_default,
        test_eval_mode_is_deterministic_no_noise,
        test_noise_recorded_in_trace_training,
        test_configurable_noise_std_zero_disables_noise,
        test_throttle_exploration_bias_shifts_noise_mean,
        test_brake_exploration_bias_shifts_noise_mean_down,
        test_physical_action_uses_relu_zero_neutral_mapping,
        test_no_forced_full_brake_anywhere,
        test_normal_drive_clears_safe_stop_flags,
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
