"""Offline tests for collector live attach + vJoy startup."""
from __future__ import annotations

import struct
import threading
from pathlib import Path
from unittest import mock

import numpy as np


class _FakeVJoy:
    def __init__(self, *, fail_button_press: bool = False) -> None:
        self.reference = 1
        self.acquired = True
        self._fail_button_press = fail_button_press

    def generateJoystickPosition(self, wAxisX=0, wAxisY=0, wAxisZ=0, lButtons=0, **kwargs) -> bytes:
        return struct.pack(
            "BlllllllllllllllllllIIII",
            1, 0, 0, 0, int(wAxisX), int(wAxisY), int(wAxisZ),
            0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
            int(lButtons), 0, 0, 0, 0,
        )

    def update(self, packet: bytes) -> bool:
        if self._fail_button_press:
            axes = struct.unpack("BlllllllllllllllllllIIII", packet)
            if int(axes[19]):
                return False
        return True


class _FakeLocalControls:
    SCALE = 16384

    def __init__(self, vj: _FakeVJoy | None = None) -> None:
        self.steer = 1.0
        self.acc = 0.0
        self.brake = 0.0
        self.onButtons = 0
        self.vj = vj or _FakeVJoy()

    def set_controls(self, steer, acc, brake, **kwargs) -> None:
        self.steer = float(steer) + 1.0
        self.acc = (float(acc) + 1.0) / 2.0
        self.brake = (float(brake) + 1.0) / 2.0
        self.update()

    def update(self) -> None:
        pos = self.vj.generateJoystickPosition(
            wAxisX=int(self.steer * self.SCALE),
            wAxisY=int(self.acc * 2 * self.SCALE),
            wAxisZ=int(self.brake * 2 * self.SCALE),
            lButtons=int(self.onButtons),
        )
        self.vj.update(pos)


class _FakeDriverControls(dict):
    def __init__(self) -> None:
        super().__init__()
        self.vjoy_executed_by_server = False
        self.local_controls = _FakeLocalControls()
        self._startup_override_active = False
        self._startup_override_action = None
        self._startup_control_write_count = 0
        self._startup_last_control_writer = ""
        self._startup_neutral_overwrite_detected = False
        self["steer"] = 0
        self["acc"] = -1
        self["brake"] = -1

    def startup_diagnostics(self) -> dict:
        action = self._startup_override_action or (
            float(self["steer"]),
            float(self["acc"]),
            float(self["brake"]),
        )
        lc = self.local_controls
        return {
            "startup_controls_object_id": id(lc),
            "startup_override_active": bool(self._startup_override_active),
            "startup_requested_internal_action": [float(action[0]), float(action[1]), float(action[2])],
            "startup_requested_pedal_fraction": (float(action[1]) + 1.0) / 2.0,
            "startup_readback_pedal_fraction": float(lc.acc),
            "startup_control_write_count": int(self._startup_control_write_count),
            "startup_neutral_overwrite_detected": bool(self._startup_neutral_overwrite_detected),
            "startup_last_control_writer": str(self._startup_last_control_writer),
        }

    def _write_local_controls(self, steer, acc, brake, *, writer="") -> None:
        self["steer"] = steer
        self["acc"] = acc
        self["brake"] = brake
        if writer:
            self._startup_control_write_count += 1
            self._startup_last_control_writer = writer
        self.local_controls.set_controls(steer, acc, brake)

    def startup_control_override(self, steer, acc, brake, *, writer="startup_control_override"):
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            self._startup_override_active = True
            self._startup_override_action = (float(steer), float(acc), float(brake))
            self._startup_control_write_count = 0
            self._startup_neutral_overwrite_detected = False
            try:
                self._write_local_controls(steer, acc, brake, writer=writer)
                yield self
            finally:
                self._startup_override_active = False
                self._startup_override_action = None
                self._write_local_controls(0.0, -1.0, -1.0, writer="startup_override_exit")

        return _ctx()

    def set_controls(self, steer, acc, brake, **kwargs) -> None:
        if self._startup_override_active and float(steer) == 0 and float(acc) == -1 and float(brake) == -1:
            self._startup_neutral_overwrite_detected = True
            return
        if self._startup_override_active:
            self._startup_override_action = (float(steer), float(acc), float(brake))
        self._write_local_controls(steer, acc, brake, writer="set_controls")

    def apply_local_controls(self) -> None:
        if self._startup_override_active and self._startup_override_action is not None:
            steer, acc, brake = self._startup_override_action
            self._write_local_controls(steer, acc, brake, writer="apply_local_controls")
            return
        self._write_local_controls(self["steer"], self["acc"], self["brake"], writer="apply_local_controls")

    def set_defaults(self) -> None:
        if self._startup_override_active:
            self._startup_neutral_overwrite_detected = True
            return
        self["steer"] = 0
        self["acc"] = -1
        self["brake"] = -1


class _FakeClient:
    def __init__(self, gear: int = 0) -> None:
        self.state = {"actualGear": gear, "gear": gear, "speed": 1.0, "RPM": 1500.0, "steps": 0}
        self.controls = _FakeDriverControls()
        self.reset_calls: list[bool] = []
        self.simulation_management = object()

    def reset(self, send_reset: bool = True, connection_timeout_sec: float | None = None) -> None:
        self.reset_calls.append(send_reset)

    def get_servers_input(self) -> None:
        self.state["steps"] = int(self.state.get("steps", 0)) + 1

    def respond_to_server(self) -> None:
        self.controls.apply_local_controls()


class _StaleClient(_FakeClient):
    """Telemetry that never advances (no fresh packets)."""

    def get_servers_input(self) -> None:
        pass


class _FakeEnv:
    def __init__(self, gear: int = 0) -> None:
        self.client = _FakeClient(gear=gear)
        self.n_episodes = 0
        self.send_reset_at_start = True
        self.auto_shift_to_first = True
        self.low_speed_timeout_sec = 30.0
        self.ctrl_rate = 25
        self.states: list = []
        self.start_actions = np.array([0.0, -1.0, -1.0])
        self.info = {}
        self._gear_shift_mapping = {
            "valid": True,
            "vjoy_device_id": 1,
            "vjoy_button_mask": 1,
            "vjoy_button_number": 1,
        }
        self.step_calls = 0

    def step(self, action):
        self.step_calls += 1
        self.states.append(dict(self.client.state))
        obs = np.array([float(self.step_calls)], dtype=np.float32)
        return obs, 0.0, False, {"speed": 1.0}


class _ProgressiveGearClient(_FakeClient):
    def __init__(self, engage_after: int = 2) -> None:
        super().__init__(gear=0)
        self._engage_after = engage_after
        self._pumps = 0

    def get_servers_input(self) -> None:
        self._pumps += 1
        self.state["steps"] = int(self.state.get("steps", 0)) + 1
        if self._pumps >= self._engage_after:
            self.state["actualGear"] = 2
            self.state["gear"] = 2


class _FakeRuntime:
    def __init__(self) -> None:
        self.phase = "starting"
        self.last_error = ""
        self.vjoy_device = 1
        self.last_telemetry_ts = 0.0
        self.telemetry_snapshot: dict = {}
        self._telemetry_ready = False
        self.last_observation: list = []
        self.gear_mapping: dict = {}
        self.drive_state_report: dict = {}
        self.vjoy_startup_report: dict = {}
        self.initial_session_attached = False
        self.vjoy_device_acquired = False
        self.vjoy_axis_verified = False
        self.initial_gear_engaged = False
        self.vjoy_neutralization_verified = False
        self.controls_live_reported = None
        self.controls_live_bypassed_for_initial_attach = False

    def telemetry_age_ms(self) -> float:
        if not self._telemetry_ready:
            return 1e9
        import time as _time

        return max(0.0, (_time.time() - self.last_telemetry_ts) * 1000.0)

    def set_phase(self, phase: str, error: str = "") -> None:
        self.phase = phase
        if error:
            self.last_error = error

    def write_minimal_status(self) -> None:
        pass


def _gear_mapping_ok() -> dict:
    return {
        "valid": True,
        "vjoy_device_id": 1,
        "vjoy_button_mask": 1,
        "vjoy_button_number": 1,
    }


def test_live_attach_no_env_reset():
    from ac_workers import collector_live_attach as cla

    env = _FakeEnv()
    runtime = _FakeRuntime()
    with mock.patch.object(cla, "wait_drive_input_delivered", return_value=True), mock.patch.object(
        cla, "is_drive_input_delivered", return_value=True
    ), mock.patch.object(
        cla, "wait_for_fresh_telemetry", return_value=(True, "")
    ), mock.patch.object(
        cla, "wait_for_management_port", return_value=(True, "")
    ), mock.patch.object(
        cla, "wait_for_driving_after_reset", return_value=(True, {"driving": True})
    ), mock.patch.object(cla, "bootstrap_env_observation", return_value=np.array([1.0])), mock.patch.object(
        cla, "run_vjoy_startup_validation", return_value=(True, "", {"initial_gear_engaged": True})
    ), mock.patch.object(cla, "_assert_drive_truth", return_value=(True, "")):
        ok, err, obs = cla.attach_live_session(env, runtime, 0)
    assert ok is True
    assert err == ""
    assert obs is not None
    assert env.client.reset_calls == [False]
    assert env.send_reset_at_start is True
    assert env.auto_shift_to_first is False
    assert runtime.initial_session_attached is True


def test_episode_reset_uses_csp_reset_after_attach():
    """After live attach, env.reset() must send ext_resetCar via send_reset_at_start."""
    from ac_workers import collector_live_attach as cla

    env = _FakeEnv()
    runtime = _FakeRuntime()
    with mock.patch.object(cla, "wait_drive_input_delivered", return_value=True), mock.patch.object(
        cla, "is_drive_input_delivered", return_value=True
    ), mock.patch.object(
        cla, "wait_for_fresh_telemetry", return_value=(True, "")
    ), mock.patch.object(
        cla, "wait_for_management_port", return_value=(True, "")
    ), mock.patch.object(
        cla, "wait_for_driving_after_reset", return_value=(True, {"driving": True})
    ), mock.patch.object(cla, "bootstrap_env_observation", return_value=np.array([1.0])), mock.patch.object(
        cla, "run_vjoy_startup_validation", return_value=(True, "", {"initial_gear_engaged": True})
    ), mock.patch.object(cla, "_assert_drive_truth", return_value=(True, "")):
        ok, err, _obs = cla.attach_live_session(env, runtime, 0)
    assert ok is True
    assert err == ""

    env.client.reset(env.send_reset_at_start)
    assert env.client.reset_calls == [False, True]


def test_missing_telemetry_sends_no_vjoy_validation():
    from ac_workers import collector_live_attach as cla

    env = _FakeEnv()
    runtime = _FakeRuntime()
    validated = {"called": False}

    def _boom(*_a, **_k):
        validated["called"] = True
        return True, "", {}

    with mock.patch.object(cla, "wait_for_management_port", return_value=(True, "")), mock.patch.object(
        cla, "wait_for_fresh_telemetry", return_value=(False, "no telemetry")
    ), mock.patch.object(
        cla, "run_vjoy_startup_validation", side_effect=_boom
    ):
        ok, err, obs = cla.attach_live_session(env, runtime, 0)
    assert ok is False
    assert runtime.phase == "attach_failed"
    assert obs is None
    assert validated["called"] is False


def test_invalid_profile_vjoy_startup_failed():
    from ac_workers import collector_live_attach as cla

    env = _FakeEnv()
    env._gear_shift_mapping = {"valid": False, "error": "GEARUP not on vJoy"}
    runtime = _FakeRuntime()
    with mock.patch.object(cla, "wait_drive_input_delivered", return_value=True), mock.patch.object(
        cla, "is_drive_input_delivered", return_value=True
    ), mock.patch.object(
        cla, "wait_for_fresh_telemetry", return_value=(True, "")
    ), mock.patch.object(
        cla, "wait_for_management_port", return_value=(True, "")
    ), mock.patch.object(
        cla, "wait_for_driving_after_reset", return_value=(True, {"driving": True})
    ), mock.patch.object(cla, "bootstrap_env_observation", return_value=np.array([1.0])):
        ok, err, _obs = cla.attach_live_session(env, runtime, 0)
    assert ok is False
    assert runtime.phase == "vjoy_startup_failed"
    assert "GEARUP" in err


def test_same_controls_instance_for_gear_and_actions():
    from ac_workers import collector_live_attach as cla

    env = _FakeEnv(gear=0)
    runtime = _FakeRuntime()
    seen: list[int] = []

    def _validate(e, _wid, _gm, _dev, **kwargs):
        seen.append(id(e.client.controls.local_controls))
        return True, "", {"device_acquired": True, "axis_verified": True, "initial_gear_engaged": True}

    with mock.patch.object(cla, "wait_drive_input_delivered", return_value=True), mock.patch.object(
        cla, "is_drive_input_delivered", return_value=True
    ), mock.patch.object(
        cla, "wait_for_fresh_telemetry", return_value=(True, "")
    ), mock.patch.object(
        cla, "wait_for_management_port", return_value=(True, "")
    ), mock.patch.object(
        cla, "wait_for_driving_after_reset", return_value=(True, {"driving": True})
    ), mock.patch.object(cla, "bootstrap_env_observation", return_value=np.array([1.0])), mock.patch.object(
        cla, "run_vjoy_startup_validation", side_effect=_validate
    ):
        cla.attach_live_session(env, runtime, 0)
    assert seen == [id(env.client.controls.local_controls)]


def test_neutral_gear_sends_single_pulse():
    from ac_workers import collector_live_attach as cla
    from ac_workers import vjoy_session as vs

    env = _FakeEnv(gear=0)
    ctrl = env.client.controls.local_controls
    readback = vs.attach_feeder_readback(ctrl.vj)
    ok, reason = vs.pulse_gear_up(ctrl, 1, readback)
    assert ok is True
    assert reason == ""


def test_already_first_reports_engaged():
    from ac_workers import collector_live_attach as cla
    from ac_workers.vjoy_session import FeederReadback, attach_feeder_readback

    env = _FakeEnv(gear=2)
    empty_rb = FeederReadback.empty()
    with mock.patch("ac_workers.collector_live_attach.device_acquired", return_value=True), mock.patch(
        "ac_workers.collector_live_attach.attach_feeder_readback",
        side_effect=attach_feeder_readback,
    ), mock.patch(
        "ac_workers.collector_live_attach.write_and_verify", return_value=(True, "", empty_rb)
    ):
        ok, err, report = cla.run_vjoy_startup_validation(env, 0, _gear_mapping_ok(), 1)
    assert ok is True
    assert err == ""
    assert report["initial_gear_engaged"] is True
    assert report["startup_throttle_assist_used"] is False


def test_startup_does_not_engage_gear():
    """No scripted gear boost: startup never pulses GEARUP and never applies
    throttle. The AI engages first gear itself via gas (auto_shifter). We only
    report the current gear for diagnostics and hand control to the policy."""
    from ac_workers import collector_live_attach as cla
    from ac_workers.vjoy_session import FeederReadback, attach_feeder_readback

    env = _FakeEnv(gear=0)
    runtime = _FakeRuntime()
    empty_rb = FeederReadback.empty()
    with mock.patch("ac_workers.collector_live_attach.device_acquired", return_value=True), mock.patch(
        "ac_workers.collector_live_attach.attach_feeder_readback",
        side_effect=attach_feeder_readback,
    ), mock.patch(
        "ac_workers.collector_live_attach.write_and_verify", return_value=(True, "", empty_rb)
    ):
        ok, err, report = cla.run_vjoy_startup_validation(
            env, 0, _gear_mapping_ok(), 1, runtime=runtime,
        )
    assert ok is True
    assert err == ""
    assert report["initial_gear_engaged"] is False
    assert report["gear_up_pulse_sent"] is False
    assert report["startup_throttle_assist_used"] is False
    assert report["startup_throttle_assist_fraction"] == 0.0
    assert report["startup_telemetry_fresh"] is True
    # The gear-boost helper and its GEARUP import are fully removed.
    assert not hasattr(cla, "wait_initial_gear_engagement")
    assert not hasattr(cla, "pulse_gear_up")


def test_gear_remained_neutral_after_assist():
    from ac_workers import collector_live_attach as cla
    from ac_workers.vjoy_session import FeederReadback, attach_feeder_readback

    env = _FakeEnv(gear=0)
    runtime = _FakeRuntime()
    empty_rb = FeederReadback.empty()
    with mock.patch("ac_workers.collector_live_attach.device_acquired", return_value=True), mock.patch(
        "ac_workers.collector_live_attach.attach_feeder_readback",
        side_effect=attach_feeder_readback,
    ), mock.patch(
        "ac_workers.collector_live_attach.write_and_verify", return_value=(True, "", empty_rb)
    ):
        ok, err, report = cla.run_vjoy_startup_validation(
            env, 0, _gear_mapping_ok(), 1, runtime=runtime,
        )
    assert ok is True
    assert err == ""
    assert report["initial_gear_engaged"] is False
    assert report["startup_telemetry_fresh"] is True
    assert report["initial_gear_after_raw"] in (0, 1)


def test_vjoy_startup_lock_blocks_concurrent_writes():
    import time

    from ac_workers import collector_live_attach as cla

    gate = threading.Event()
    started = threading.Event()

    def holder() -> None:
        with cla._VJOY_STARTUP_LOCK:
            started.set()
            gate.wait(timeout=2.0)

    holder_thread = threading.Thread(target=holder)
    holder_thread.start()
    assert started.wait(timeout=2.0)

    result = {"done": False}

    def runner() -> None:
        cla.run_vjoy_startup_validation(_FakeEnv(gear=2), 0, _gear_mapping_ok(), 1)
        result["done"] = True

    runner_thread = threading.Thread(target=runner)
    runner_thread.start()
    time.sleep(0.05)
    assert result["done"] is False
    gate.set()
    runner_thread.join(timeout=5.0)
    holder_thread.join(timeout=5.0)
    assert result["done"] is True


def test_startup_succeeds_when_gear_stays_neutral():
    """Even if the car is still in neutral, startup succeeds and hands control to
    the AI (no pulse, no throttle, no failure)."""
    from ac_workers import collector_live_attach as cla
    from ac_workers.vjoy_session import FeederReadback, attach_feeder_readback

    env = _FakeEnv(gear=0)
    runtime = _FakeRuntime()
    empty_rb = FeederReadback.empty()
    with mock.patch("ac_workers.collector_live_attach.device_acquired", return_value=True), mock.patch(
        "ac_workers.collector_live_attach.attach_feeder_readback",
        side_effect=attach_feeder_readback,
    ), mock.patch(
        "ac_workers.collector_live_attach.write_and_verify", return_value=(True, "", empty_rb)
    ):
        ok, err, report = cla.run_vjoy_startup_validation(
            env, 0, _gear_mapping_ok(), 1, runtime=runtime,
        )
    assert ok is True
    assert err == ""
    assert report["initial_gear_engaged"] is False
    assert report["gear_up_pulse_sent"] is False


def test_neutral_drive_scene_attaches_before_gearup():
    from ac_workers import collector_live_attach as cla

    class _Mgmt:
        def get_driving_state(self) -> dict:
            # Brake released = out of menu -> drive confirmed regardless of the
            # (now ignored) controls_live signal.
            return {"driving": True, "sim_live": True, "physics_brake": 0.0, "controls_live": False}

        def send_resume_drive(self) -> None:
            pass

    env = _FakeEnv(gear=1)
    env.client.simulation_management = _Mgmt()
    runtime = _FakeRuntime()
    seen: list[tuple[str, bool]] = []

    def _vjoy(_env, _wid, _gm, _dev, **kwargs):
        seen.append(("vjoy", runtime.initial_session_attached))
        return True, "", {
            "device_acquired": True,
            "axis_verified": True,
            "initial_gear_engaged": True,
            "gear_up_pulse_sent": True,
        }

    with mock.patch.object(cla, "wait_drive_input_delivered", return_value=True), mock.patch.object(
        cla, "is_drive_input_delivered", return_value=True
    ), mock.patch.object(
        cla, "wait_for_fresh_telemetry", return_value=(True, "")
    ), mock.patch.object(cla, "wait_for_management_port", return_value=(True, "")), mock.patch.object(
        cla, "bootstrap_env_observation", return_value=np.array([1.0])
    ), mock.patch.object(cla, "run_vjoy_startup_validation", side_effect=_vjoy), mock.patch.object(
        cla, "_assert_drive_truth", return_value=(True, "")
    ):
        ok, err, obs = cla.attach_live_session(env, runtime, 0)
    assert ok is True
    assert err == ""
    assert obs is not None
    assert seen == [("vjoy", False)]
    assert runtime.initial_session_attached is True
    assert runtime.initial_gear_engaged is True
    assert runtime.controls_live_reported is False
    assert runtime.controls_live_bypassed_for_initial_attach is True


def test_controls_live_false_bypassed_with_drive_input():
    from ac_workers import collector_live_attach as cla

    class _Mgmt:
        def get_driving_state(self) -> dict:
            # Brake released = out of menu -> drive confirmed regardless of the
            # (now ignored) controls_live signal.
            return {"driving": True, "sim_live": True, "physics_brake": 0.0, "controls_live": False}

        def send_resume_drive(self) -> None:
            pass

    env = _FakeEnv(gear=1)
    env.client.simulation_management = _Mgmt()
    runtime = _FakeRuntime()
    validated = {"called": False}

    def _vjoy(*_a, **_k):
        validated["called"] = True
        return True, "", {"initial_gear_engaged": True}

    with mock.patch.object(cla, "wait_drive_input_delivered", return_value=True), mock.patch.object(
        cla, "is_drive_input_delivered", return_value=True
    ), mock.patch.object(
        cla, "wait_for_fresh_telemetry", return_value=(True, "")
    ), mock.patch.object(cla, "wait_for_management_port", return_value=(True, "")), mock.patch.object(
        cla, "bootstrap_env_observation", return_value=np.array([1.0])
    ), mock.patch.object(cla, "run_vjoy_startup_validation", side_effect=_vjoy), mock.patch.object(
        cla, "_assert_drive_truth", return_value=(True, "")
    ):
        ok, err, obs = cla.attach_live_session(env, runtime, 0)
    assert ok is True
    assert validated["called"] is True
    assert runtime.controls_live_bypassed_for_initial_attach is True


def test_no_drive_input_fails_before_vjoy():
    from ac_workers import collector_live_attach as cla
    from ac_workers.post_reset_drive import DRIVE_INPUT_NOT_DELIVERED_ERROR

    env = _FakeEnv(gear=1)
    runtime = _FakeRuntime()
    validated = {"called": False}

    def _boom(*_a, **_k):
        validated["called"] = True
        return True, "", {}

    with mock.patch.object(cla, "wait_drive_input_delivered", return_value=False), mock.patch.object(
        cla, "is_drive_input_delivered", return_value=False
    ), mock.patch.object(
        cla, "wait_for_fresh_telemetry", return_value=(True, "")
    ), mock.patch.object(cla, "wait_for_management_port", return_value=(True, "")), mock.patch.object(
        cla, "run_vjoy_startup_validation", side_effect=_boom
    ):
        ok, err, obs = cla.attach_live_session(env, runtime, 0)
    assert ok is False
    assert obs is None
    assert validated["called"] is False
    assert runtime.phase == "waiting_for_drive_failed"
    assert err == DRIVE_INPUT_NOT_DELIVERED_ERROR


def test_management_delayed_then_succeeds():
    from ac_workers import collector_live_attach as cla

    env = _FakeEnv()
    runtime = _FakeRuntime()
    attempts = {"n": 0}

    def _tcp(_host, _port):
        attempts["n"] += 1
        return attempts["n"] >= 3

    with mock.patch.object(cla, "wait_drive_input_delivered", return_value=True), mock.patch.object(
        cla, "is_drive_input_delivered", return_value=True
    ), mock.patch.object(
        cla, "wait_for_fresh_telemetry", return_value=(True, "")
    ), mock.patch(
        "ac_workers.worker_handshake.tcp_open", side_effect=_tcp
    ), mock.patch.object(
        cla, "wait_for_driving_after_reset", return_value=(True, {"driving": True})
    ), mock.patch.object(cla, "bootstrap_env_observation", return_value=np.array([1.0])), mock.patch.object(
        cla, "run_vjoy_startup_validation", return_value=(True, "", {"initial_gear_engaged": True})
    ), mock.patch.object(cla, "_assert_drive_truth", return_value=(True, "")):
        ok, err, obs = cla.attach_live_session(env, runtime, 0, sleep_fn=lambda _s: None)
    assert ok is True
    assert err == ""
    assert obs is not None
    assert attempts["n"] >= 3


def test_management_never_opens_startup_failed():
    from ac_workers import collector_live_attach as cla

    env = _FakeEnv()
    runtime = _FakeRuntime()
    with mock.patch.object(cla, "wait_for_fresh_telemetry", return_value=(True, "")), mock.patch.object(
        cla, "wait_for_management_port",
        return_value=(False, "driving session active but management port 2347 did not open"),
    ):
        ok, err, obs = cla.attach_live_session(env, runtime, 0, sleep_fn=lambda _s: None)
    assert ok is False
    assert obs is None
    assert runtime.phase == "attach_failed"
    assert "management port 2347 did not open" in err


def test_wait_for_management_port_polls_until_open():
    from ac_workers import collector_live_attach as cla

    runtime = _FakeRuntime()
    clock = {"t": 0.0}
    attempts = {"n": 0}

    def _tcp(_host, _port):
        attempts["n"] += 1
        return attempts["n"] >= 2

    ok, reason = cla.wait_for_management_port(
        2347,
        runtime,
        sleep_fn=lambda _s: clock.__setitem__("t", clock["t"] + 0.25),
        tcp_open_fn=_tcp,
        timeout_sec=1.0,
    )
    assert ok is True
    assert reason == ""
    assert runtime.phase == "waiting_for_management"
    assert attempts["n"] >= 2


def test_startup_diagnostics_are_neutral_no_throttle():
    from ac_workers import collector_live_attach as cla
    from ac_workers.vjoy_session import FeederReadback, attach_feeder_readback

    env = _FakeEnv(gear=0)
    runtime = _FakeRuntime()
    empty_rb = FeederReadback.empty()
    with mock.patch("ac_workers.collector_live_attach.device_acquired", return_value=True), mock.patch(
        "ac_workers.collector_live_attach.attach_feeder_readback",
        side_effect=attach_feeder_readback,
    ), mock.patch(
        "ac_workers.collector_live_attach.write_and_verify", return_value=(True, "", empty_rb)
    ):
        ok, err, report = cla.run_vjoy_startup_validation(
            env, 0, _gear_mapping_ok(), 1, runtime=runtime,
        )
    assert ok is True
    assert err == ""
    # Nothing dictated: no throttle requested at any point during startup.
    assert report["startup_requested_pedal_fraction"] == 0.0
    assert abs(report["startup_readback_pedal_fraction"]) < 0.01
    assert report["startup_throttle_assist_used"] is False
    assert report["startup_override_active_final"] is False
    assert float(report["startup_final_neutral_readback"].get("acc_pedal", 1.0)) < 0.01


def test_startup_override_blocks_neutral_apply():
    from ac_workers.control_smoke_test import build_requested_internal_action

    client = _FakeClient(gear=0)
    action = build_requested_internal_action()
    lc = client.controls.local_controls
    with client.controls.startup_control_override(float(action[0]), float(action[1]), float(action[2])):
        assert abs(lc.acc - 0.2) < 0.01
        client.controls.set_controls(0.0, -1.0, -1.0)
        client.respond_to_server()
        assert abs(lc.acc - 0.2) < 0.01
        assert client.controls._startup_neutral_overwrite_detected is True
    assert abs(lc.acc - 0.0) < 0.01


def test_respond_to_server_reapplies_throttle_during_override():
    from ac_workers.control_smoke_test import build_requested_internal_action

    client = _ProgressiveGearClient(engage_after=3)
    action = build_requested_internal_action()
    lc = client.controls.local_controls
    with client.controls.startup_control_override(float(action[0]), float(action[1]), float(action[2])):
        client.get_servers_input()
        client.respond_to_server()
        assert abs(lc.acc - 0.2) < 0.01
        assert client.controls.startup_diagnostics()["startup_last_control_writer"] == "apply_local_controls"


def test_gear_engage_neutral_readback_failure_reported():
    from ac_workers import collector_live_attach as cla
    from ac_workers.vjoy_session import FeederReadback, attach_feeder_readback

    env = _FakeEnv(gear=0)
    runtime = _FakeRuntime()
    empty_rb = FeederReadback.empty()
    clock = {"t": 0.0}

    def _verify_side_effect(_readback, expected):
        # Fail the neutral axis baseline check.
        return False, "acc_pedal expected 0.0000 readback 1.0000", empty_rb

    with mock.patch("ac_workers.collector_live_attach.device_acquired", return_value=True), mock.patch(
        "ac_workers.collector_live_attach.attach_feeder_readback",
        side_effect=attach_feeder_readback,
    ), mock.patch(
        "ac_workers.collector_live_attach.write_and_verify",
        side_effect=_verify_side_effect,
    ):
        ok, err, report = cla.run_vjoy_startup_validation(
            env,
            0,
            _gear_mapping_ok(),
            1,
            runtime=runtime,
            sleep_fn=lambda _s: clock.__setitem__("t", clock["t"] + 0.01),
            monotonic=lambda: clock["t"],
        )
    assert ok is False
    # The neutral baseline axis verification fails before any gear logic.
    assert "axis readback" in err or "neutral readback" in err


def test_driver_controls_real_override_blocks_neutral():
    from ac_workers.control_smoke_test import build_requested_internal_action

    with mock.patch("AssettoCorsaEnv.ac_client.Controls", _FakeLocalControls):
        from AssettoCorsaEnv.ac_client import DriverControls

        driver = DriverControls(vjoy_executed_by_server=False)
        driver.enable_local_vjoy_feeder()
        action = build_requested_internal_action()
        lc = driver.local_controls
        with driver.startup_control_override(float(action[0]), float(action[1]), float(action[2])):
            assert abs(lc.acc - 0.2) < 0.01
            driver.apply_local_controls()
            assert abs(lc.acc - 0.2) < 0.01
            driver.set_controls(0.0, -1.0, -1.0)
            assert driver._startup_neutral_overwrite_detected is True
        assert abs(lc.acc - 0.0) < 0.01


def test_attach_publish_uses_minimal_status_not_publish():
    import inspect

    from ac_workers import collector_live_attach as cla

    body = inspect.getsource(cla.attach_live_session)
    publish_fn = body.split("def _publish_phase", 1)[1].split("\n\n", 1)[0]
    assert "write_minimal_status" in publish_fn
    assert "publish_status" not in publish_fn


def test_attach_path_management_before_telemetry():
    from ac_workers import collector_live_attach as cla

    src = cla.attach_live_session.__code__.co_names
    assert "wait_for_management_port" in src
    assert "wait_for_fresh_telemetry" in src
    body = __import__("inspect").getsource(cla.attach_live_session)
    assert body.index("management_connect_begin") < body.index("telemetry_wait_begin")
    assert "connection_timeout_sec=MAX_TELEMETRY_WAIT_SEC" in body


def test_telemetry_connection_timeout_reports_attach_failed():
    from ac_workers import collector_live_attach as cla
    from AssettoCorsaEnv.ac_client import TelemetryConnectionTimeout

    env = _FakeEnv()
    runtime = _FakeRuntime()

    def _reset_fail(*_a, **_k):
        raise TelemetryConnectionTimeout("sensors_par_not_streaming: port 2345")

    env.client.reset = _reset_fail  # type: ignore[method-assign]
    with mock.patch.object(cla, "wait_for_management_port", return_value=(True, "")):
        ok, err, obs = cla.attach_live_session(env, runtime, 0)
    assert ok is False
    assert obs is None
    assert runtime.phase == "attach_failed"
    assert "sensors_par_not_streaming" in err


def test_first_telemetry_phase_marker():
    from ac_workers import collector_live_attach as cla

    env = _FakeEnv()
    runtime = _FakeRuntime()
    phases: list[str] = []
    original = runtime.set_phase

    def _track(phase, error=""):
        phases.append(phase)
        original(phase, error)

    runtime.set_phase = _track  # type: ignore[method-assign]
    with mock.patch.object(cla, "wait_for_management_port", return_value=(True, "")), mock.patch.object(
        cla, "wait_for_fresh_telemetry", return_value=(True, "")
    ), mock.patch.object(cla, "wait_drive_input_delivered", return_value=False), mock.patch.object(
        cla, "is_drive_input_delivered", return_value=False
    ):
        cla.attach_live_session(env, runtime, 0)
    assert "first_telemetry_received" in phases


def test_worker_0_ports_match_functional_config():
    from AssettoCorsaEnv.ac_worker_runtime import load_worker_config
    from ac_workers.worker_ports import worker_ports

    cfg = load_worker_config(0)
    ports = worker_ports(0)
    assert ports.ego == cfg.ego_server_port == 2345
    assert ports.mgmt == cfg.simulation_management_server_port == 2347
    assert ports.vjoy == cfg.vjoy_device_id == 1


def test_launcher_log_path_uses_ac_workers_logs():
    import os
    import tempfile
    from ac_workers import collector_live_attach as cla

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        log_dir = repo / "ac_workers" / "logs"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "worker_2_20260627_test.log"
        log_file.write_text("Drive confirmed via click\n", encoding="utf-8")
        with mock.patch.object(cla, "__file__", str(repo / "ac_workers" / "collector_live_attach.py")):
            with mock.patch.dict(os.environ, {"AC_AI_SESSION_ID": "20260627_test"}, clear=False):
                path = cla._launcher_log_path(2)
        assert path == log_file


def test_is_drive_input_delivered_reads_confirmed_log():
    import os
    import tempfile
    from ac_workers import collector_live_attach as cla

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        log_dir = repo / "ac_workers" / "logs"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "worker_0_20260627_test.log"
        log_file.write_text("Drive confirmed after 4 clicks\n", encoding="utf-8")
        with mock.patch.object(cla, "__file__", str(repo / "ac_workers" / "collector_live_attach.py")):
            with mock.patch.dict(
                os.environ,
                {"AC_AI_SESSION_ID": "20260627_test", cla.DRIVE_INPUT_ENV: ""},
                clear=False,
            ):
                assert cla.is_drive_input_delivered(0) is True


def test_is_drive_input_delivered_ignores_already_active_only():
    import os
    import tempfile
    from ac_workers import collector_live_attach as cla

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        log_dir = repo / "ac_workers" / "logs"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "worker_3_20260627_test.log"
        log_file.write_text("Drive session already active\n", encoding="utf-8")
        with mock.patch.object(cla, "__file__", str(repo / "ac_workers" / "collector_live_attach.py")):
            with mock.patch.dict(
                os.environ,
                {"AC_AI_SESSION_ID": "20260627_test", cla.DRIVE_INPUT_ENV: ""},
                clear=False,
            ):
                assert cla.is_drive_input_delivered(3) is False


def test_drive_input_status_three_states():
    import os
    import tempfile
    from ac_workers import collector_live_attach as cla

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        log_dir = repo / "ac_workers" / "logs"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "worker_1_20260627_t.log"
        with mock.patch.object(cla, "__file__", str(repo / "ac_workers" / "collector_live_attach.py")):
            with mock.patch.dict(
                os.environ,
                {"AC_AI_SESSION_ID": "20260627_t", cla.DRIVE_INPUT_ENV: ""},
                clear=False,
            ):
                log_file.write_text("Drive UI stable\n", encoding="utf-8")
                assert cla.drive_input_status(1) == "pending"
                log_file.write_text("Drive UI stable\nDrive confirmed via click\n", encoding="utf-8")
                assert cla.drive_input_status(1) == "delivered"
                log_file.write_text("Drive input not delivered: timeout\n", encoding="utf-8")
                assert cla.drive_input_status(1) == "not_delivered"


def test_wait_drive_input_confirms_when_plugin_reports_drive():
    """Plugin reports brake released after a few polls -> delivered (no log)."""
    from ac_workers import collector_live_attach as cla

    class _Mgmt:
        def __init__(self):
            self.calls = 0

        def get_driving_state(self):
            self.calls += 1
            # Still in the menu (brake pinned) for the first polls, then drive starts.
            brake = 0.0 if self.calls >= 3 else 1.0
            return {"sim_live": True, "physics_brake": brake}

    clock = {"t": 0.0}
    ok = cla.wait_drive_input_delivered(
        0,
        mgmt=_Mgmt(),
        sleep_fn=lambda _s: clock.__setitem__("t", clock["t"] + 0.5),
        monotonic=lambda: clock["t"],
        timeout_sec=30.0,
    )
    assert ok is True


def test_wait_drive_input_times_out_when_plugin_stuck_in_menu():
    """Plugin never leaves the menu (brake pinned) -> failure, log is ignored."""
    from ac_workers import collector_live_attach as cla

    class _Mgmt:
        def get_driving_state(self):
            return {"sim_live": True, "physics_brake": 1.0}

    clock = {"t": 0.0}
    # Even if a launcher log claimed success, the plugin is the only truth.
    with mock.patch.object(cla, "drive_input_status", return_value="delivered"):
        ok = cla.wait_drive_input_delivered(
            0,
            mgmt=_Mgmt(),
            sleep_fn=lambda _s: clock.__setitem__("t", clock["t"] + 1.0),
            monotonic=lambda: clock["t"],
            timeout_sec=10.0,
        )
    assert ok is False


def test_wait_drive_input_ignores_log_when_no_mgmt():
    """No plugin handle -> never confirms from a log alone."""
    from ac_workers import collector_live_attach as cla

    clock = {"t": 0.0}
    with mock.patch.object(cla, "drive_input_status", return_value="delivered"):
        ok = cla.wait_drive_input_delivered(
            0,
            mgmt=None,
            sleep_fn=lambda _s: clock.__setitem__("t", clock["t"] + 1.0),
            monotonic=lambda: clock["t"],
            timeout_sec=5.0,
        )
    assert ok is False


def test_neutralization_after_exception():
    from ac_workers import collector_live_attach as cla

    env = _FakeEnv()
    ok, reason = cla.neutralize_env_controls(env)
    assert ok is True
    assert reason == ""


def main() -> int:
    tests = [
        test_live_attach_no_env_reset,
        test_missing_telemetry_sends_no_vjoy_validation,
        test_invalid_profile_vjoy_startup_failed,
        test_same_controls_instance_for_gear_and_actions,
        test_neutral_gear_sends_single_pulse,
        test_already_first_reports_engaged,
        test_startup_does_not_engage_gear,
        test_startup_diagnostics_are_neutral_no_throttle,
        test_gear_remained_neutral_after_assist,
        test_vjoy_startup_lock_blocks_concurrent_writes,
        test_startup_override_blocks_neutral_apply,
        test_respond_to_server_reapplies_throttle_during_override,
        test_gear_engage_neutral_readback_failure_reported,
        test_driver_controls_real_override_blocks_neutral,
        test_startup_succeeds_when_gear_stays_neutral,
        test_neutral_drive_scene_attaches_before_gearup,
        test_controls_live_false_bypassed_with_drive_input,
        test_no_drive_input_fails_before_vjoy,
        test_management_delayed_then_succeeds,
        test_management_never_opens_startup_failed,
        test_attach_publish_uses_minimal_status_not_publish,
        test_attach_path_management_before_telemetry,
        test_telemetry_connection_timeout_reports_attach_failed,
        test_first_telemetry_phase_marker,
        test_worker_0_ports_match_functional_config,
        test_wait_for_management_port_polls_until_open,
        test_launcher_log_path_uses_ac_workers_logs,
        test_is_drive_input_delivered_reads_confirmed_log,
        test_is_drive_input_delivered_ignores_already_active_only,
        test_drive_input_status_three_states,
        test_wait_drive_input_confirms_when_plugin_reports_drive,
        test_wait_drive_input_times_out_when_plugin_stuck_in_menu,
        test_wait_drive_input_ignores_log_when_no_mgmt,
        test_neutralization_after_exception,
    ]
    for fn in tests:
        fn()
        print("OK", fn.__name__)
    print("Test-CollectorLiveAttach-Offline: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
