"""Offline tests for AC_AI_CONTROL_SMOKE_TEST vJoy control chain."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest import mock

import numpy as np

REPO = Path(__file__).resolve().parents[1]


class _FakeLocalControls:
    def __init__(self) -> None:
        self.steer = 1.0
        self.acc = 0.0
        self.brake = 0.0
        self.steer_cmd = 0.0
        self.pedal_cmd = -1.0
        self.brake_cmd = -1.0
        self.vj = mock.Mock()
        self.vj.acquired = True
        self.vj.reference = 1
        self._feeder_enabled = True

    def enable_feeder(self) -> None:
        self._feeder_enabled = True

    def feeder_enabled(self) -> bool:
        return bool(self._feeder_enabled)

    def set_controls(self, steer, acc, brake, **kwargs) -> None:
        self.steer_cmd = float(steer)
        self.pedal_cmd = float(acc)
        self.brake_cmd = float(brake)
        self.steer = self.steer_cmd + 1.0
        self.acc = (self.pedal_cmd + 1.0) / 2.0
        self.brake = (self.brake_cmd + 1.0) / 2.0


class _FakeDriverControls(dict):
    def __init__(self) -> None:
        super().__init__()
        self.local_controls = _FakeLocalControls()
        self._local_vjoy_enabled = True
        self["steer"] = 0.0
        self["acc"] = -1.0
        self["brake"] = -1.0

    def enable_local_vjoy_feeder(self) -> None:
        self._local_vjoy_enabled = True
        self.local_controls.enable_feeder()

    def local_vjoy_feeder_enabled(self) -> bool:
        return bool(self._local_vjoy_enabled)

    def set_defaults(self) -> None:
        self.local_controls = _FakeLocalControls()
        self["steer"] = 0.0
        self["acc"] = -1.0
        self["brake"] = -1.0

    def apply_local_controls(self) -> None:
        lc = self.local_controls
        lc.set_controls(self.get("steer", lc.steer_cmd), self.get("acc", lc.pedal_cmd), self.get("brake", lc.brake_cmd))


class _FakeEnv:
    def __init__(self, speed: float = 0.0, rpm: float = 900.0, gear: int = 2) -> None:
        self.client = mock.Mock()
        self.client.controls = _FakeDriverControls()
        self.client.state = {"speed": speed, "RPM": rpm, "actualGear": gear}
        self.client.simulation_management = mock.Mock()
        # Confirmable drive state for the single truth source: sim live + brake
        # released (out of the Drive menu).
        self.client.simulation_management.get_driving_state.return_value = {
            "driving": True,
            "sim_live": True,
            "physics_brake": 0.0,
            "controls_live": True,
        }
        self.info: dict = {"speed": speed}
        self._speed = speed
        self._rpm = rpm
        self._gear = gear
        self._current = np.array([0.0, -1.0, -1.0])
        self.states: list[dict] = []
        self.start_actions = np.array([0.0, -1.0, -1.0], dtype=np.float64)

    def set_actions(self, action) -> None:
        self._current = np.array(action, dtype=np.float64).copy()
        lc = self.client.controls.local_controls
        lc.set_controls(float(action[0]), float(action[1]), float(action[2]))
        self.client.controls["steer"] = float(action[0])
        self.client.controls["acc"] = float(action[1])
        self.client.controls["brake"] = float(action[2])

    def get_executed_action_for_buffer(self) -> np.ndarray:
        return self._current.copy()

    def step(self, action=None):
        self._speed += 0.12
        self._rpm += 200.0
        self.client.state = {"speed": self._speed, "RPM": self._rpm, "actualGear": self._gear}
        self.info = {"speed": self._speed}
        self.states.append({"steerAngle": 0.0, "accStatus": -1.0, "brakeStatus": -1.0})
        return np.zeros(8), 0.0, False, self.info


class _FakeRuntime:
    def __init__(self) -> None:
        self.worker_id = 0
        self.env = None
        self.phase = "running"
        self.last_error = ""
        self.last_telemetry_ts = time.time()
        self.last_requested_action: list[float] = []
        self.last_executed_action: list[float] = []
        self.telemetry_snapshot: dict = {}
        self.control_smoke_test_report: dict = {}
        self.control_smoke_test_result = ""
        self._telemetry_ready = True
        self.last_observation = [0.0]
        self.telemetry_snapshot = {"steer": 0.0}

    def observation_complete(self) -> bool:
        return self._telemetry_ready and bool(self.last_observation)

    def telemetry_age_ms(self) -> float:
        return 0.0

    def set_phase(self, phase: str, error: str = "") -> None:
        self.phase = phase
        if error:
            self.last_error = error

    def touch_heartbeat(self) -> None:
        pass


class _FakeCfg:
    simulation_management_server_port = 2347
    vjoy_device_id = 1


def _infra_patches():
    return (
        mock.patch("ac_workers.worker_handshake.tcp_open", return_value=True),
        mock.patch("ac_workers.worker_handshake.vjoy_accessible", return_value=True),
    )


def test_incomplete_history_then_complete_sends_commands():
    from ac_workers import control_smoke_test as cst
    from AssettoCorsaEnv.ac_env import PAST_ACTIONS_WINDOW

    runtime = _FakeRuntime()
    env = _FakeEnv()
    env.states = []
    runtime.env = env
    phases: list[str] = []

    def track_phase():
        phases.append(runtime.phase)

    p_tcp, p_vjoy = _infra_patches()
    with mock.patch.dict(os.environ, {cst.ENV_VAR: "1"}), p_tcp, p_vjoy:
        report = cst.run_control_smoke_test(
            worker_id=0,
            runtime=runtime,
            env=env,
            cfg=_FakeCfg(),
            neutral_vjoy_fn=lambda: None,
            publish_status=track_phase,
            monotonic=lambda: 0.0,
            sleep_fn=lambda _s: None,
        )

    assert report["commands_sent"] is True
    assert report["observations_received"] >= PAST_ACTIONS_WINDOW
    assert report["history_ready"] is True
    assert "control_smoke_waiting_observation" in phases
    assert report["result"] in ("moved", "no_response", "engine_response_only")
    assert "before" in report
    assert "after" in report
    assert "max_speed" in report


def test_never_fresh_motion_no_commands_explicit_error():
    from ac_workers import control_smoke_test as cst

    runtime = _FakeRuntime()
    env = _FakeEnv()
    runtime.env = env
    runtime.telemetry_age_ms = lambda: 9999.0
    clock = {"t": 0.0}
    p_tcp, p_vjoy = _infra_patches()

    with mock.patch.dict(os.environ, {cst.ENV_VAR: "1"}), p_tcp, p_vjoy, mock.patch.object(
        cst, "MAX_OBS_WAIT_SEC", 1.0
    ):
        report = cst.run_control_smoke_test(
            worker_id=0,
            runtime=runtime,
            env=env,
            cfg=_FakeCfg(),
            neutral_vjoy_fn=lambda: None,
            publish_status=lambda: None,
            monotonic=lambda: clock["t"],
            sleep_fn=lambda _s: clock.__setitem__("t", clock["t"] + 0.2),
        )

    assert report["commands_sent"] is False
    assert report["result"] == "failed"
    assert report["error"]
    assert report["motion_telemetry_ready"] is False


def test_commands_sent_only_after_throttle_apply():
    from ac_workers import control_smoke_test as cst

    runtime = _FakeRuntime()
    env = _FakeEnv()
    runtime.env = env
    sent_throttle = {"n": 0}

    real_apply = cst.apply_smoke_physical_controls

    def track_apply(*args, **kwargs):
        result = real_apply(*args, **kwargs)
        from ac_workers.control_smoke_test import THROTTLE_PEDAL_FRACTION as _T

        pedal = float(kwargs.get("throttle", _T))
        if pedal >= 0.1:
            sent_throttle["n"] += 1
        return result

    p_tcp, p_vjoy = _infra_patches()

    with mock.patch.dict(os.environ, {cst.ENV_VAR: "1"}), p_tcp, p_vjoy, mock.patch.object(
        cst, "apply_smoke_physical_controls", side_effect=track_apply
    ):
        report = cst.run_control_smoke_test(
            worker_id=0,
            runtime=runtime,
            env=env,
            cfg=_FakeCfg(),
            neutral_vjoy_fn=lambda: None,
            publish_status=lambda: None,
            monotonic=lambda: 0.0,
            sleep_fn=lambda _s: None,
        )

    assert report["commands_sent"] is True
    assert sent_throttle["n"] >= 1


def test_total_delay_bounded():
    from ac_workers import control_smoke_test as cst

    runtime = _FakeRuntime()
    env = _FakeEnv()
    runtime.env = env
    clock = {"t": 0.0}
    p_tcp, p_vjoy = _infra_patches()

    with mock.patch.dict(os.environ, {cst.ENV_VAR: "1"}), p_tcp, p_vjoy, mock.patch.object(
        cst, "MAX_OBS_WAIT_SEC", 5.0
    ), mock.patch.object(cst, "MAX_DURATION_SEC", 3.0):
        cst.run_control_smoke_test(
            worker_id=0,
            runtime=runtime,
            env=env,
            cfg=_FakeCfg(),
            neutral_vjoy_fn=lambda: None,
            publish_status=lambda: None,
            monotonic=lambda: clock["t"],
            sleep_fn=lambda _s: clock.__setitem__("t", clock["t"] + 0.5),
        )

    assert clock["t"] <= cst.MAX_OBS_WAIT_SEC + cst.POST_DRIVE_SETTLE_SEC + cst.MAX_DURATION_SEC + 1.0


def test_smoke_does_not_require_collector_observation_complete():
    from ac_workers import control_smoke_test as cst
    from ac_workers.worker_collector import CollectorRuntime

    runtime = CollectorRuntime(0, 1, 2347)
    runtime._telemetry_ready = True
    runtime.last_observation = [0.0]
    runtime.last_telemetry_ts = time.time()
    env = _FakeEnv()
    runtime.env = env
    p_tcp, p_vjoy = _infra_patches()

    with mock.patch.dict(os.environ, {cst.ENV_VAR: "1"}), p_tcp, p_vjoy:
        report = cst.run_control_smoke_test(
            worker_id=0,
            runtime=runtime,
            env=env,
            cfg=_FakeCfg(),
            neutral_vjoy_fn=lambda: None,
            publish_status=lambda: None,
            monotonic=lambda: 0.0,
            sleep_fn=lambda _s: None,
        )

    assert report["commands_sent"] is True
    assert runtime.observation_complete() is False or runtime.phase == "running"


def test_smoke_disabled_by_default():
    from ac_workers.control_smoke_test import is_enabled

    with mock.patch.dict(os.environ, {}, clear=True):
        assert is_enabled(0) is False
        assert is_enabled(1) is False


def test_smoke_enabled_worker_zero_only():
    from ac_workers.control_smoke_test import ENV_VAR, is_enabled

    with mock.patch.dict(os.environ, {ENV_VAR: "1"}):
        assert is_enabled(0) is True
        assert is_enabled(1) is False


def test_requested_action_matches_pedal_conversion():
    from ac_workers.control_smoke_test import build_requested_internal_action
    from AssettoCorsaEnv.training_safety import BRAKE_INTERNAL_OFF, internal_to_pedal, pedal_to_internal

    action = build_requested_internal_action()
    assert action[0] == 0.0
    assert abs(float(action[1]) - pedal_to_internal(0.20)) < 1e-6
    assert action[2] == BRAKE_INTERNAL_OFF
    assert abs(internal_to_pedal(float(action[1])) - 0.20) < 1e-6
    assert abs(internal_to_pedal(float(action[2])) - 0.0) < 1e-6


def test_vjoy_output_neutral_steer_20pct_throttle_no_brake():
    from ac_workers.control_smoke_test import build_requested_internal_action, read_vjoy_sent_values

    env = _FakeEnv()
    action = build_requested_internal_action()
    env.set_actions(action)
    env.client.controls.apply_local_controls()
    vjoy = read_vjoy_sent_values(env)
    assert abs(vjoy["steer_cmd"] - 0.0) < 1e-6
    assert abs(vjoy["acc_cmd"] - (-0.6)) < 1e-6
    assert abs(vjoy["brake_cmd"] - (-1.0)) < 1e-6
    assert abs(vjoy["steer_vjoy"] - 1.0) < 1e-6
    assert abs(vjoy["acc_pedal"] - 0.20) < 1e-6
    assert abs(vjoy["brake_pedal"] - 0.0) < 1e-6


def test_stale_telemetry_sends_no_commands():
    from ac_workers import control_smoke_test as cst

    runtime = _FakeRuntime()
    env = _FakeEnv()
    runtime.env = env
    cfg = _FakeCfg()
    neutral_calls: list[int] = []
    clock = {"t": 0.0}

    def always_stale_age():
        return 5000.0

    runtime.telemetry_age_ms = always_stale_age

    p_tcp, p_vjoy = _infra_patches()
    with mock.patch.dict(os.environ, {cst.ENV_VAR: "1"}), p_tcp, p_vjoy, mock.patch.object(
        cst, "MAX_OBS_WAIT_SEC", 0.5
    ):
        report = cst.run_control_smoke_test(
            worker_id=0,
            runtime=runtime,
            env=env,
            cfg=cfg,
            neutral_vjoy_fn=lambda: neutral_calls.append(1),
            publish_status=lambda: None,
            monotonic=lambda: clock["t"],
            sleep_fn=lambda _s: clock.__setitem__("t", clock["t"] + 0.1),
        )
    assert report["commands_sent"] is False
    assert report["result"] == "failed"
    assert report["error"]
    assert "stale" in report["error"].lower() or "telemetry" in report["error"].lower()
    assert neutral_calls == []


def test_smoke_runs_max_three_seconds():
    from ac_workers import control_smoke_test as cst

    runtime = _FakeRuntime()
    env = _FakeEnv()
    runtime.env = env
    cfg = _FakeCfg()
    clock = {"t": 0.0}
    step_calls = {"n": 0}

    def fake_mono():
        return clock["t"]

    def fake_sleep(_sec):
        clock["t"] += 0.5

    real_step = env.step

    def counting_step(action=None):
        step_calls["n"] += 1
        env._speed += 1.5
        env._rpm += 200.0
        env.client.state = {"speed": env._speed, "RPM": env._rpm, "actualGear": env._gear}
        env.info = {"speed": env._speed}
        env.states.append({"steerAngle": 0.0, "accStatus": -1.0, "brakeStatus": -1.0})
        return np.zeros(8), 0.0, False, env.info

    env.step = counting_step

    p_tcp, p_vjoy = _infra_patches()
    with mock.patch.dict(os.environ, {cst.ENV_VAR: "1"}), p_tcp, p_vjoy, mock.patch.object(
        cst, "MAX_DURATION_SEC", 3.0
    ):
        report = cst.run_control_smoke_test(
            worker_id=0,
            runtime=runtime,
            env=env,
            cfg=cfg,
            neutral_vjoy_fn=lambda: None,
            publish_status=lambda: None,
            monotonic=fake_mono,
            sleep_fn=fake_sleep,
        )

    assert step_calls["n"] >= 1
    assert step_calls["n"] <= 12
    assert report["commands_sent"] is True
    assert report["result"] == "moved"


def test_neutralize_on_exception():
    from ac_workers import control_smoke_test as cst

    runtime = _FakeRuntime()
    env = _FakeEnv()
    runtime.env = env
    cfg = _FakeCfg()
    neutral_calls: list[int] = []

    def boom(*args, **kwargs):
        raise RuntimeError("simulated step failure")

    env.step = boom

    p_tcp, p_vjoy = _infra_patches()
    with mock.patch.dict(os.environ, {cst.ENV_VAR: "1"}), p_tcp, p_vjoy, mock.patch.object(
        cst, "wait_for_smoke_observation", return_value=(True, "")
    ):
        report = cst.run_control_smoke_test(
            worker_id=0,
            runtime=runtime,
            env=env,
            cfg=cfg,
            neutral_vjoy_fn=lambda: neutral_calls.append(1),
            publish_status=lambda: None,
            monotonic=lambda: 0.0,
            sleep_fn=lambda _s: None,
        )

    assert report["result"] == "failed"
    assert neutral_calls == [1]
    lc = env.client.controls.local_controls
    assert abs(lc.acc - 0.0) < 1e-6
    assert abs(lc.brake - 0.0) < 1e-6


def test_status_written_with_result():
    from ac_workers import control_smoke_test as cst
    from ac_workers.worker_collector import CollectorRuntime

    with __import__("tempfile").TemporaryDirectory() as tmp:
        status_dir = Path(tmp)
        runtime = CollectorRuntime(0, 1, 2347)
        runtime._telemetry_ready = True
        runtime.last_observation = [0.0]
        runtime.last_telemetry_ts = time.time()
        env = _FakeEnv()
        runtime.env = env

        published: list[dict] = []

        def capture_status():
            runtime.control_smoke_test_report["phase"] = runtime.phase
            published.append(dict(runtime.control_smoke_test_report))
            with mock.patch("ac_workers.worker_collector.STATUS_DIR", status_dir):
                runtime.write_minimal_status()

        p_tcp, p_vjoy = _infra_patches()
        with mock.patch.dict(os.environ, {cst.ENV_VAR: "1"}), p_tcp, p_vjoy, mock.patch.object(
            cst, "MAX_OBS_WAIT_SEC", 0.0
        ):
            report = cst.run_control_smoke_test(
                worker_id=0,
                runtime=runtime,
                env=env,
                cfg=_FakeCfg(),
                neutral_vjoy_fn=lambda: None,
                publish_status=capture_status,
                monotonic=lambda: 0.0,
                sleep_fn=lambda _s: None,
            )

        path = status_dir / "worker_0.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["phase"] == "running"
        assert data["control_smoke_test_result"] in (
            "moved",
            "no_response",
            "engine_response_only",
            "failed",
        )
        assert data["control_smoke_test"]["result"] == report["result"]
        assert "vjoy_sent" in data["control_smoke_test"]
        assert published


def test_collector_loads_policy_after_smoke_not_during():
    src = (REPO / "ac_workers" / "worker_collector.py").read_text(encoding="utf-8")
    smoke_idx = src.index("run_control_smoke_test")
    policy_idx = src.index("runtime.policy = CollectorPolicy")
    assert smoke_idx < policy_idx


def test_rpm_only_engine_response_not_moved():
    from ac_workers import control_smoke_test as cst

    class _RpmOnlyEnv(_FakeEnv):
        def step(self, action=None):
            self._rpm += 250.0
            self.client.state = {"speed": self._speed, "RPM": self._rpm, "actualGear": self._gear}
            self.info = {"speed": self._speed}
            self.states.append({"steerAngle": 0.0, "accStatus": -1.0, "brakeStatus": -1.0})
            return np.zeros(8), 0.0, False, self.info

    runtime = _FakeRuntime()
    env = _RpmOnlyEnv()
    runtime.env = env
    p_tcp, p_vjoy = _infra_patches()

    with mock.patch.dict(os.environ, {cst.ENV_VAR: "1"}), p_tcp, p_vjoy:
        report = cst.run_control_smoke_test(
            worker_id=0,
            runtime=runtime,
            env=env,
            cfg=_FakeCfg(),
            neutral_vjoy_fn=lambda: None,
            publish_status=lambda: None,
            monotonic=lambda: 0.0,
            sleep_fn=lambda _s: None,
        )

    assert report["result"] == "engine_response_only"
    assert report["vehicle_moved"] is False
    assert report["engine_response"] is True
    assert report["max_speed"] <= 1.0


def test_near_zero_speed_not_moved():
    from ac_workers import control_smoke_test as cst
    from ac_workers.control_smoke_test import evaluate_motion_outcome

    before = {"speed": 0.1, "rpm": 900.0, "gear": 2, "lap_distance": 100.0}
    after = {"speed": 0.2, "rpm": 950.0, "gear": 2, "lap_distance": 100.05}
    result, metrics = evaluate_motion_outcome(
        before,
        after,
        max_speed=0.2,
        max_rpm=950.0,
        min_lap_distance=100.0,
        max_lap_distance=100.05,
        speed_high_samples=0,
    )
    assert result == "no_response"
    assert metrics["vehicle_moved"] is False


def test_speed_and_distance_confirm_moved():
    from ac_workers.control_smoke_test import evaluate_motion_outcome

    before = {"speed": 0.0, "rpm": 900.0, "gear": 2, "lap_distance": 10.0}
    after = {"speed": 5.0, "rpm": 2500.0, "gear": 2, "lap_distance": 10.5}
    result, metrics = evaluate_motion_outcome(
        before,
        after,
        max_speed=5.0,
        max_rpm=2500.0,
        min_lap_distance=10.0,
        max_lap_distance=10.5,
        speed_high_samples=2,
    )
    assert result == "moved"
    assert metrics["vehicle_moved"] is True
    assert metrics["distance_delta"] == 0.5


def test_report_includes_motion_metrics():
    from ac_workers import control_smoke_test as cst

    runtime = _FakeRuntime()
    env = _FakeEnv()
    runtime.env = env
    p_tcp, p_vjoy = _infra_patches()

    with mock.patch.dict(os.environ, {cst.ENV_VAR: "1"}), p_tcp, p_vjoy:
        report = cst.run_control_smoke_test(
            worker_id=0,
            runtime=runtime,
            env=env,
            cfg=_FakeCfg(),
            neutral_vjoy_fn=lambda: None,
            publish_status=lambda: None,
            monotonic=lambda: 0.0,
            sleep_fn=lambda _s: None,
        )

    for key in (
        "before",
        "after",
        "max_speed",
        "max_rpm",
        "vehicle_moved",
        "engine_response",
    ):
        assert key in report
    assert "speed" in report["before"]
    assert "rpm" in report["before"]


def test_failed_always_has_explicit_error():
    from ac_workers import control_smoke_test as cst

    runtime = _FakeRuntime()
    env = _FakeEnv()
    runtime.env = env
    runtime.telemetry_age_ms = lambda: 9999.0
    p_tcp, p_vjoy = _infra_patches()

    with mock.patch.dict(os.environ, {cst.ENV_VAR: "1"}), p_tcp, p_vjoy, mock.patch.object(
        cst, "MAX_OBS_WAIT_SEC", 0.1
    ):
        report = cst.run_control_smoke_test(
            worker_id=0,
            runtime=runtime,
            env=env,
            cfg=_FakeCfg(),
            neutral_vjoy_fn=lambda: None,
            publish_status=lambda: None,
            monotonic=lambda: 0.0,
            sleep_fn=lambda _s: None,
        )

    assert report["result"] == "failed"
    assert report["error"]


def test_validated_startup_bypasses_controls_live():
    from ac_workers import control_smoke_test as cst
    from ac_workers.post_reset_drive import CONTROLS_NOT_LIVE_ERROR

    env = _FakeEnv(gear=2)
    runtime = _FakeRuntime()
    runtime.env = env
    runtime.initial_session_attached = True
    runtime.vjoy_device_acquired = True
    runtime.vjoy_axis_verified = True
    runtime.initial_gear_engaged = True
    runtime.startup_telemetry_fresh = True
    env.client.simulation_management = mock.Mock()
    env.client.simulation_management.get_driving_state.return_value = {
        "driving": True,
        "controls_live": False,
    }
    p_tcp, p_vjoy = _infra_patches()
    with p_tcp, p_vjoy:
        ok, reason = cst.infrastructure_ready(runtime, _FakeCfg())
    assert ok is True
    assert reason == ""
    assert reason != CONTROLS_NOT_LIVE_ERROR


def test_unvalidated_startup_menu_brake_hold_still_blocks():
    from ac_workers import control_smoke_test as cst
    from ac_workers.drive_truth import REASON_MENU_BRAKE_HOLD

    env = _FakeEnv(gear=2)
    runtime = _FakeRuntime()
    runtime.env = env
    env.client.simulation_management = mock.Mock()
    # Car still parked in the Drive menu: live but brake pinned -> the single
    # truth source must block (controls_live is no longer the signal).
    env.client.simulation_management.get_driving_state.return_value = {
        "driving": True,
        "sim_live": True,
        "physics_brake": 1.0,
        "controls_live": False,
    }
    p_tcp, p_vjoy = _infra_patches()
    with p_tcp, p_vjoy:
        ok, reason = cst.infrastructure_ready(runtime, _FakeCfg())
    assert ok is False
    assert reason == REASON_MENU_BRAKE_HOLD


def test_infrastructure_ready_allows_neutral_gear_when_drive_active():
    """Guided smoke test must not fail with 'initial gear not engaged' in N."""
    from ac_workers import control_smoke_test as cst
    from ac_workers.post_reset_drive import GEAR_NOT_ENGAGED_ERROR

    env = _FakeEnv(gear=0)
    runtime = _FakeRuntime()
    runtime.env = env
    p_tcp, p_vjoy = _infra_patches()
    with p_tcp, p_vjoy:
        ok, reason = cst.infrastructure_ready(runtime, _FakeCfg())
    assert ok is True
    assert reason == ""
    assert reason != GEAR_NOT_ENGAGED_ERROR


def test_prepare_smoke_first_gear_already_engaged():
    from ac_workers import control_smoke_test as cst

    env = _FakeEnv(gear=2)
    ok, report = cst.prepare_smoke_first_gear(env, sleep_fn=lambda _s: None)
    assert ok is True
    assert report["engaged"] is True
    assert report["method"] == "already_engaged"


def test_prepare_smoke_first_gear_uses_shift_then_throttle():
    from ac_workers import control_smoke_test as cst

    env = _FakeEnv(gear=0)
    env.states = [dict(env.client.state)]

    def _fake_shift(e):
        e._gear = 2
        e.client.state["actualGear"] = 2
        return True, {"auto_shifter": False, "manual_skipped": False}

    with mock.patch(
        "AssettoCorsaEnv.gear_shift.auto_shift_neutral_to_first",
        side_effect=_fake_shift,
    ):
        ok, report = cst.prepare_smoke_first_gear(env, sleep_fn=lambda _s: None)
    assert ok is True
    assert report["engaged"] is True
    assert report["method"] == "shift_pulse"
    assert report["gear_after"] == 2


def test_smoke_runs_after_neutral_gear_prep():
    from ac_workers import control_smoke_test as cst

    env = _FakeEnv(gear=0)
    runtime = _FakeRuntime()
    runtime.env = env
    runtime.initial_session_attached = True
    runtime.vjoy_device_acquired = True
    runtime.vjoy_axis_verified = True
    runtime.initial_gear_engaged = False
    runtime.startup_telemetry_fresh = True

    def _engage(_env, sleep_fn=None, device_id=None):
        _env._gear = 2
        _env.client.state["actualGear"] = 2
        return True, {"engaged": True, "method": "shift_pulse", "gear_after": 2}

    p_tcp, p_vjoy = _infra_patches()
    with mock.patch.dict(os.environ, {cst.ENV_VAR: "1"}), p_tcp, p_vjoy, mock.patch.object(
        cst, "MAX_OBS_WAIT_SEC", 0.05
    ), mock.patch.object(cst, "MAX_DURATION_SEC", 0.05), mock.patch.object(
        cst, "POST_DRIVE_SETTLE_SEC", 0.0
    ), mock.patch.object(cst, "prepare_smoke_first_gear", side_effect=_engage):
        report = cst.run_control_smoke_test(
            worker_id=0,
            runtime=runtime,
            env=env,
            cfg=_FakeCfg(),
            neutral_vjoy_fn=lambda: None,
            publish_status=lambda: None,
            monotonic=lambda: 0.0,
            sleep_fn=lambda _s: None,
        )
    assert report.get("error") != "initial gear not engaged"
    assert report.get("commands_sent") is True
    assert report.get("gear_prep", {}).get("engaged") is True


def test_validated_startup_smoke_not_blocked_by_controls_live():
    from ac_workers import control_smoke_test as cst

    env = _FakeEnv(gear=2)
    runtime = _FakeRuntime()
    runtime.env = env
    runtime.initial_session_attached = True
    runtime.vjoy_device_acquired = True
    runtime.vjoy_axis_verified = True
    runtime.initial_gear_engaged = True
    runtime.startup_telemetry_fresh = True
    env.client.simulation_management = mock.Mock()
    env.client.simulation_management.get_driving_state.return_value = {
        "driving": True,
        "controls_live": False,
    }
    p_tcp, p_vjoy = _infra_patches()
    with mock.patch.dict(os.environ, {cst.ENV_VAR: "1"}), p_tcp, p_vjoy, mock.patch.object(
        cst, "MAX_OBS_WAIT_SEC", 0.05
    ), mock.patch.object(cst, "MAX_DURATION_SEC", 0.05), mock.patch.object(
        cst, "POST_DRIVE_SETTLE_SEC", 0.0
    ):
        report = cst.run_control_smoke_test(
            worker_id=0,
            runtime=runtime,
            env=env,
            cfg=_FakeCfg(),
            neutral_vjoy_fn=lambda: None,
            publish_status=lambda: None,
            monotonic=lambda: 0.0,
            sleep_fn=lambda _s: None,
        )
    assert report["error"] != "session control menu still open or controls are not live"
    assert report.get("commands_sent") is True


def test_collector_unchanged_when_env_absent():
    from ac_workers.control_smoke_test import is_enabled, run_control_smoke_test

    runtime = _FakeRuntime()
    with mock.patch.dict(os.environ, {}, clear=True):
        assert is_enabled(0) is False
        assert run_control_smoke_test(
            worker_id=0,
            runtime=runtime,
            env=_FakeEnv(),
            cfg=_FakeCfg(),
            neutral_vjoy_fn=lambda: None,
            publish_status=lambda: None,
        ) == {}


def main() -> int:
    tests = [
        test_smoke_disabled_by_default,
        test_smoke_enabled_worker_zero_only,
        test_requested_action_matches_pedal_conversion,
        test_vjoy_output_neutral_steer_20pct_throttle_no_brake,
        test_stale_telemetry_sends_no_commands,
        test_incomplete_history_then_complete_sends_commands,
        test_never_fresh_motion_no_commands_explicit_error,
        test_commands_sent_only_after_throttle_apply,
        test_total_delay_bounded,
        test_smoke_does_not_require_collector_observation_complete,
        test_smoke_runs_max_three_seconds,
        test_rpm_only_engine_response_not_moved,
        test_near_zero_speed_not_moved,
        test_speed_and_distance_confirm_moved,
        test_report_includes_motion_metrics,
        test_failed_always_has_explicit_error,
        test_neutralize_on_exception,
        test_status_written_with_result,
        test_collector_loads_policy_after_smoke_not_during,
        test_validated_startup_bypasses_controls_live,
        test_unvalidated_startup_menu_brake_hold_still_blocks,
        test_validated_startup_smoke_not_blocked_by_controls_live,
        test_collector_unchanged_when_env_absent,
    ]
    for fn in tests:
        fn()
        print("OK", fn.__name__)
    print("Test-ControlSmoke-Offline: OK")
    return 0


if __name__ == "__main__":
    try:
        import pytest  # noqa: F401
    except ImportError:
        pass
    raise SystemExit(main())
