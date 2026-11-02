"""Offline tests for episode reset and first-training learner status."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "assetto_corsa_gym"))
sys.path.insert(0, str(REPO / "algorithm" / "discor"))


class _FakeMgmt:
    def __init__(self) -> None:
        self.resume_calls = 0
        self._driving = False

    def send_resume_drive(self) -> None:
        self.resume_calls += 1
        self._driving = True

    def get_driving_state(self) -> dict:
        return {"driving": self._driving, "controls_live": False}

    def get_static_info(self) -> dict:
        return {"TrackLength": 1000.0, "TrackFullName": "monza", "CarName": "lotus_exos_125"}

    def get_config(self) -> dict:
        return {"ego_sampling_freq": 25}


class _ResetRuntime:
    def __init__(self) -> None:
        self.phase = "running"
        self.last_error = ""
        self.initial_session_attached = True
        self.vjoy_device = 1
        self.startup_controls_object_id = None
        self.last_telemetry_ts = time.time()
        self._telemetry_ready = True
        self.vjoy_startup_report = {}
        self.drive_state_report = {}
        self.controls_live_reported = None
        self.controls_live_bypassed_for_initial_attach = False
        self.initial_gear_engaged = False
        self.vjoy_device_acquired = False
        self.vjoy_axis_verified = False
        self.vjoy_neutralization_verified = False
        self.initial_gear_before_raw = None
        self.initial_gear_after_raw = None
        self.initial_gear_samples: list = []
        self.startup_throttle_assist_used = False
        self.startup_telemetry_fresh = False
        self.last_observation: list = []
        self.reset_controls_object_id = None
        self.reset_vjoy_device_status_before = None
        self.reset_vjoy_reacquire_attempted = False
        self.reset_vjoy_reacquire_succeeded = False
        self.reset_update_vjd_attempts = 0
        self.reset_neutralization_verified = False
        self.reset_gear_before_raw = None
        self.reset_gear_after_raw = None
        self.reset_last_error = ""

    def set_phase(self, phase: str, err: str = "") -> None:
        self.phase = phase
        if err:
            self.last_error = err

    def telemetry_age_ms(self) -> float:
        return max(0.0, (time.time() - self.last_telemetry_ts) * 1000.0)

    def publish_status(self) -> None:
        pass


class _ResetEnv:
    def __init__(self, *, gear: int = 1) -> None:
        from ac_workers.test_collector_live_attach_offline import (
            _FakeClient,
            _FakeDriverControls,
        )

        self.client = _FakeClient(gear=gear)
        self.client.simulation_management = _FakeMgmt()
        self.client.controls = _FakeDriverControls()
        self._gear_shift_mapping = {
            "valid": True,
            "vjoy_device_id": 1,
            "vjoy_button_mask": 1,
        }
        self.n_episodes = 1
        self.observation_space = type("S", (), {"shape": (125,)})()
        self.info = {"speed": 0.0}
        self.low_speed_timeout_sec = 6.0
        self.ctrl_rate = 25

    def step(self, action):
        self.client.state["steps"] = float(self.client.state.get("steps", 0)) + 1
        obs = np.zeros(125, dtype=np.float32)
        return obs, 0.0, False, {"speed": 1.0}


def test_episode_reset_neutral_then_first_gear():
    from ac_workers.collector_live_attach import run_episode_reset

    vjoy_report = {
        "device_acquired": True,
        "axis_verified": True,
        "initial_gear_engaged": True,
        "startup_telemetry_fresh": True,
        "initial_gear_before_raw": 1,
        "initial_gear_after_raw": 2,
        "initial_gear_samples": [],
        "startup_throttle_assist_used": True,
        "vjoy_neutralization_verified": True,
    }
    env = _ResetEnv(gear=1)
    runtime = _ResetRuntime()
    runtime.env = env
    runtime.startup_controls_object_id = id(env.client.controls.local_controls)
    with mock.patch(
        "ac_workers.collector_live_attach._wait_reset_session",
        return_value=(True, "", {"driving": True, "controls_live": False}),
    ), mock.patch(
        "ac_workers.collector_live_attach.run_vjoy_startup_validation",
        return_value=(True, "", dict(vjoy_report)),
    ) as mock_vjoy:
        ok, err, obs = run_episode_reset(env, runtime, 0, sleep_fn=lambda _s: None)
    assert ok, err
    assert runtime.phase == "running"
    assert obs is not None
    assert runtime.initial_gear_before_raw == 1
    assert runtime.initial_gear_after_raw == 2
    assert mock_vjoy.called


def test_episode_reset_ten_consecutive():
    from ac_workers.collector_live_attach import run_episode_reset

    vjoy_report = {
        "device_acquired": True,
        "axis_verified": True,
        "initial_gear_engaged": True,
        "startup_telemetry_fresh": True,
        "initial_gear_before_raw": 1,
        "initial_gear_after_raw": 2,
        "initial_gear_samples": [],
        "startup_throttle_assist_used": True,
        "vjoy_neutralization_verified": True,
    }
    with mock.patch(
        "ac_workers.collector_live_attach._wait_reset_session",
        return_value=(True, "", {"driving": True, "controls_live": False}),
    ), mock.patch(
        "ac_workers.collector_live_attach.run_vjoy_startup_validation",
        return_value=(True, "", dict(vjoy_report)),
    ):
        for i in range(10):
            env = _ResetEnv(gear=1)
            runtime = _ResetRuntime()
            runtime.env = env
            runtime.startup_controls_object_id = id(env.client.controls.local_controls)
            ok, err, _obs = run_episode_reset(env, runtime, 0, sleep_fn=lambda _s: None)
            assert ok, "reset {0} failed: {1}".format(i, err)
            assert runtime.phase == "running"


def test_episode_reset_allows_controls_live_bypass():
    from ac_workers.collector_live_attach import _wait_reset_session

    env = _ResetEnv(gear=1)
    runtime = _ResetRuntime()
    runtime.env = env
    with mock.patch("ac_workers.collector_live_attach.wait_for_management_port", return_value=(True, "")), mock.patch(
        "ac_workers.collector_live_attach.wait_for_fresh_telemetry", return_value=(True, "")
    ), mock.patch("ac_workers.collector_live_attach.wait_for_driving_after_reset") as mock_drive:
        mock_drive.return_value = (True, {"driving": True, "controls_live": False})
        ok, err, state = _wait_reset_session(env, runtime, sleep_fn=lambda _s: None)
    assert ok, err
    assert mock_drive.call_args.kwargs["require_controls_live"] is False
    assert mock_drive.call_args.kwargs["require_non_neutral_gear"] is False
    assert runtime.controls_live_bypassed_for_initial_attach is True


def test_episode_reset_failed_controls_handle_changed():
    from ac_workers.collector_live_attach import (
        RESET_CONTROLS_HANDLE_CHANGED_ERROR,
        run_episode_reset,
    )

    env = _ResetEnv(gear=1)
    runtime = _ResetRuntime()
    runtime.startup_controls_object_id = 999999
    ok, err, obs = run_episode_reset(env, runtime, 0, sleep_fn=lambda _s: None)
    assert not ok
    assert err == RESET_CONTROLS_HANDLE_CHANGED_ERROR
    assert runtime.phase == "reset_failed"
    assert obs is None


def test_episode_reset_failed_not_driving():
    from ac_workers.collector_live_attach import run_episode_reset

    env = _ResetEnv(gear=1)
    runtime = _ResetRuntime()
    runtime.env = env
    runtime.startup_controls_object_id = id(env.client.controls.local_controls)
    with mock.patch(
        "ac_workers.collector_live_attach._wait_reset_session",
        return_value=(False, "AC session not driving after episode reset", {}),
    ):
        ok, err, obs = run_episode_reset(env, runtime, 0, sleep_fn=lambda _s: None)
    assert not ok
    assert runtime.phase == "reset_failed"
    assert obs is None


def test_wait_reset_session_accepts_truth_without_legacy_driving_flag():
    """Truth source confirmed (ok_drive) must not be vetoed by plugin driving=False."""
    from ac_workers.collector_live_attach import _wait_reset_session

    env = _ResetEnv(gear=1)
    runtime = _ResetRuntime()
    runtime.env = env
    runtime.vjoy_axis_verified = True
    with mock.patch(
        "ac_workers.collector_live_attach.wait_for_management_port", return_value=(True, "")
    ), mock.patch(
        "ac_workers.collector_live_attach.wait_for_fresh_telemetry", return_value=(True, "")
    ), mock.patch("ac_workers.collector_live_attach.wait_for_driving_after_reset") as mock_drive:
        mock_drive.return_value = (
            True,
            {
                "driving": False,
                "sim_live": True,
                "physics_brake": 0.0,
                "reason": "",
            },
        )
        ok, err, state = _wait_reset_session(env, runtime, sleep_fn=lambda _s: None)
    assert ok, err
    assert state["driving"] is False
    assert mock_drive.call_args.kwargs["max_wait_sec"] == 25.0
    assert mock_drive.call_args.kwargs["vjoy_readback_fn"] is not None
    assert mock_drive.call_args.kwargs["telemetry_age_fn"] is not None


def test_learner_status_warmup_not_reached():
    from ac_workers.multi_sac_trainer import MultiSacTrainer

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        trainer = MultiSacTrainer(REPO / "config_first_sac_cpu_v3.yml", work, max_steps=50, session_id="test_warmup")
        trainer.start_steps = 1500
        trainer.steps = 248
        trainer.training_started = True
        trainer._write_status(None, None)
        data = json.loads((work / "learner_status.json").read_text(encoding="utf-8"))
        assert data["learner_running"] is True
        assert data["learner_global_steps"] == 248
        assert data["learner_training_step"] == 0
        assert data["global_steps"] == 248
        assert data["learner_warmup_required"] == 1500
        assert data["learner_updates_started"] is False
        assert "warmup_not_reached" in data["learner_status_note"]
        assert data["session_id"] == "test_warmup"
        assert "started_at" in data
        assert "heartbeat" in data


def test_learner_status_warmup_reached_updates():
    from ac_workers.multi_sac_trainer import MultiSacTrainer

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        trainer = MultiSacTrainer(REPO / "config_first_sac_cpu_v3.yml", work, max_steps=2000, session_id="s2")
        trainer.start_steps = 100
        trainer.steps = 150
        trainer.updates = 3
        trainer.training_started = True
        trainer._write_status(None, None)
        data = json.loads((work / "learner_status.json").read_text(encoding="utf-8"))
        assert data["learner_updates_started"] is True
        assert data["learner_global_steps"] == 150
        assert data["learner_training_step"] == 3
        assert data["session_id"] == "s2"


def test_learner_warmup_counters_global_vs_training_step():
    from ac_workers.multi_sac_trainer import MultiSacTrainer

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        trainer = MultiSacTrainer(REPO / "config_first_sac_cpu_v3.yml", work, max_steps=5000, session_id="cnt")
        trainer.start_steps = 1500
        trainer.steps = 263
        trainer.updates = 0
        trainer.training_started = True
        trainer._write_status(None, None)
        data = json.loads((work / "learner_status.json").read_text(encoding="utf-8"))
        assert data["learner_global_steps"] == 263
        assert data["learner_replay_size"] == 0
        assert data["learner_training_step"] == 0
        assert data["learner_updates_started"] is False
        assert "warmup_not_reached" in data["learner_status_note"]


def test_learner_training_step_tracks_updates():
    from ac_workers.multi_sac_trainer import MultiSacTrainer

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        trainer = MultiSacTrainer(REPO / "config_first_sac_cpu_v3.yml", work, max_steps=5000, session_id="upd")
        trainer.start_steps = 100
        trainer.steps = 500
        trainer.updates = 42
        trainer.training_started = True
        trainer._last_checkpoint_step = 400
        trainer._write_status(None, None)
        data = json.loads((work / "learner_status.json").read_text(encoding="utf-8"))
        assert data["learner_global_steps"] == 500
        assert data["learner_training_step"] == 42
        assert data["learner_updates_started"] is True
        assert data["learner_last_checkpoint_step"] == 400


def test_learner_stop_request_saves_checkpoint():
    from ac_workers.multi_sac_trainer import MultiSacTrainer
    from common.transition_server import TransitionServer

    srv = TransitionServer(port=24071)
    srv.start()
    time.sleep(0.1)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        trainer = MultiSacTrainer(REPO / "config_first_sac_cpu_v3.yml", work, max_steps=5, session_id="stop_test")
        dummy = {
            "obs": [0.0] * 125,
            "action": [0.0, -1.0, -1.0],
            "reward": 0.0,
            "next_state": [0.0] * 125,
            "done": False,
        }
        trainer._ingest_transition(dummy)
        trainer.steps = 1
        (work / "learner_stop.request").write_text("stop", encoding="utf-8")
        assert trainer._stop_requested() is True
        trainer._save_checkpoint()
        trainer._publish()
        meta_path = work / "model" / "publish" / "policy_metadata.json"
        assert meta_path.is_file()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["state_dim"] == 125
    srv.stop()


def test_learner_pid_in_status():
    from ac_workers.multi_sac_trainer import MultiSacTrainer

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        trainer = MultiSacTrainer(REPO / "config_first_sac_cpu_v3.yml", work, max_steps=10, session_id="pid_test")
        trainer._write_status(None, None)
        data = json.loads((work / "learner_status.json").read_text(encoding="utf-8"))
        assert data["learner_pid"] == os.getpid()
        assert data["session_id"] == "pid_test"


def test_build_metadata_rejects_8dim_with_125_schema():
    from ac_workers.policy_metadata import build_metadata

    try:
        build_metadata(state_dim=8, action_dim=3)
        assert False, "should have raised ValueError"
    except ValueError as exc:
        assert "125" in str(exc)
        assert "8" in str(exc)


def test_publish_8dim_model_refused():
    from ac_workers.multi_sac_trainer import MultiSacTrainer

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        trainer = MultiSacTrainer(REPO / "config_first_sac_cpu_v3.yml", work, max_steps=5, session_id="dim8")
        dummy = {
            "obs": [0.0] * 8,
            "action": [0.0, -1.0, -1.0],
            "reward": 0.0,
            "next_state": [0.0] * 8,
            "done": False,
        }
        trainer._ingest_transition(dummy)
        try:
            trainer._publish()
            assert False, "should have raised"
        except (ValueError, RuntimeError):
            pass
        assert not (work / "model" / "publish" / "policy_metadata.json").is_file()


def test_publish_125dim_model_accepted():
    from ac_workers.multi_sac_trainer import MultiSacTrainer

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        trainer = MultiSacTrainer(REPO / "config_first_sac_cpu_v3.yml", work, max_steps=5, session_id="dim125")
        dummy = {
            "obs": [0.0] * 125,
            "action": [0.0, -1.0, -1.0],
            "reward": 0.0,
            "next_state": [0.0] * 125,
            "done": False,
        }
        trainer._ingest_transition(dummy)
        trainer._publish()
        meta = json.loads((work / "model" / "publish" / "policy_metadata.json").read_text(encoding="utf-8"))
        assert meta["state_dim"] == 125


def test_stale_learner_status_archived():
    from ac_workers.multi_sac_trainer import MultiSacTrainer

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        old_status = {"session_id": "old_session", "learner_running": False, "replay_size": 999}
        (work / "learner_status.json").write_text(json.dumps(old_status), encoding="utf-8")
        trainer = MultiSacTrainer(
            REPO / "config_first_sac_cpu_v3.yml", work, max_steps=5, session_id="new_session"
        )
        assert not (work / "learner_status.json").is_file()
        archives = list(work.glob("learner_status_archive_*.json"))
        assert len(archives) == 1
        archived = json.loads(archives[0].read_text(encoding="utf-8"))
        assert archived["session_id"] == "old_session"


def test_learner_status_session_id_matches_not_ignored():
    from ac_workers.multi_sac_trainer import MultiSacTrainer

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        current = {"session_id": "same_id", "learner_running": True, "replay_size": 10}
        (work / "learner_status.json").write_text(json.dumps(current), encoding="utf-8")
        trainer = MultiSacTrainer(
            REPO / "config_first_sac_cpu_v3.yml", work, max_steps=5, session_id="same_id"
        )
        assert (work / "learner_status.json").is_file()


def test_learner_status_write_survives_windows_lock():
    from ac_workers.multi_sac_trainer import MultiSacTrainer

    denied = PermissionError(13, "Acces refuse")
    denied.winerror = 5

    def always_denied(*_args, **_kwargs):
        raise denied

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        trainer = MultiSacTrainer(
            REPO / "config_first_sac_cpu_v3.yml", work, max_steps=5, session_id="lock"
        )
        with mock.patch("ac_workers.collector_status.os.replace", always_denied), mock.patch(
            "ac_workers.collector_status.time.sleep", return_value=None
        ):
            trainer._write_status(None, None)


def test_reset_update_vjd_false_then_reacquire_succeeds():
    from ac_workers.collector_live_attach import run_episode_reset
    from ac_workers.vjoy_session import FeederReadback, expected_logical_neutral

    vjoy_report = {
        "device_acquired": True,
        "axis_verified": True,
        "initial_gear_engaged": True,
        "startup_telemetry_fresh": True,
        "initial_gear_before_raw": 1,
        "initial_gear_after_raw": 2,
        "initial_gear_samples": [],
        "startup_throttle_assist_used": True,
        "vjoy_neutralization_verified": True,
    }
    env = _ResetEnv(gear=1)
    runtime = _ResetRuntime()
    runtime.env = env
    runtime.startup_controls_object_id = id(env.client.controls.local_controls)
    calls = {"n": 0}

    def _write_side(readback, expected):
        calls["n"] += 1
        if calls["n"] == 1:
            return False, "UpdateVJD returned false", FeederReadback.empty()
        good = FeederReadback(
            last_axes={},
            last_logical=expected_logical_neutral(),
            update_ok=True,
        )
        return True, "", good

    with mock.patch(
        "ac_workers.collector_live_attach._wait_reset_session",
        return_value=(True, "", {"driving": True, "controls_live": False}),
    ), mock.patch(
        "ac_workers.collector_live_attach.run_vjoy_startup_validation",
        return_value=(True, "", dict(vjoy_report)),
    ), mock.patch(
        "ac_workers.vjoy_session.write_and_verify",
        side_effect=_write_side,
    ), mock.patch(
        "ac_workers.vjoy_session.reacquire_vjoy_device",
        return_value=(True, ""),
    ):
        ok, err, obs = run_episode_reset(env, runtime, 0, sleep_fn=lambda _s: None)
    assert ok, err
    assert runtime.reset_vjoy_reacquire_attempted is True
    assert runtime.reset_vjoy_reacquire_succeeded is True
    assert runtime.reset_update_vjd_attempts >= 2
    assert obs is not None


def test_reset_update_vjd_always_false_fails_safe():
    from ac_workers.collector_live_attach import run_episode_reset
    from ac_workers.vjoy_session import FeederReadback

    env = _ResetEnv(gear=1)
    runtime = _ResetRuntime()
    runtime.env = env
    runtime.startup_controls_object_id = id(env.client.controls.local_controls)

    def _always_fail(_readback, _expected):
        return False, "UpdateVJD returned false", FeederReadback.empty()

    with mock.patch(
        "ac_workers.vjoy_session.write_and_verify",
        side_effect=_always_fail,
    ), mock.patch(
        "ac_workers.vjoy_session.reacquire_vjoy_device",
        return_value=(True, ""),
    ):
        ok, err, obs = run_episode_reset(env, runtime, 0, sleep_fn=lambda _s: None)
    assert not ok
    assert "UpdateVJD" in err or runtime.reset_last_error
    assert runtime.phase == "reset_failed"
    assert obs is None
    assert runtime.reset_vjoy_reacquire_attempted is True


def test_reset_same_controls_object_id():
    from ac_workers.collector_live_attach import run_episode_reset

    vjoy_report = {
        "device_acquired": True,
        "initial_gear_engaged": True,
        "initial_gear_before_raw": 1,
        "initial_gear_after_raw": 2,
        "vjoy_neutralization_verified": True,
    }
    env = _ResetEnv(gear=1)
    runtime = _ResetRuntime()
    runtime.env = env
    ctrl_id = id(env.client.controls.local_controls)
    runtime.startup_controls_object_id = ctrl_id
    with mock.patch(
        "ac_workers.collector_live_attach._wait_reset_session",
        return_value=(True, "", {"driving": True, "controls_live": False}),
    ), mock.patch(
        "ac_workers.collector_live_attach.run_vjoy_startup_validation",
        return_value=(True, "", dict(vjoy_report)),
    ):
        ok, err, _obs = run_episode_reset(env, runtime, 0, sleep_fn=lambda _s: None)
    assert ok, err
    assert runtime.reset_controls_object_id == ctrl_id
    assert id(env.client.controls.local_controls) == ctrl_id


def test_ensure_feeder_readback_single_owner():
    from ac_workers.vjoy_session import attach_feeder_readback, ensure_feeder_readback

    class _FakeVj:
        def __init__(self):
            self.calls = 0
            self.reference = 1
            self.acquired = True

        def generateJoystickPosition(self, wAxisX=0, wAxisY=0, wAxisZ=0, lButtons=0, **kwargs):
            import struct
            return struct.pack(
                "BlllllllllllllllllllIIII",
                1, 0, 0, 0, int(wAxisX), int(wAxisY), int(wAxisZ),
                0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                int(lButtons), 0, 0, 0, 0,
            )

        def update(self, packet):
            self.calls += 1
            return True

    class _FakeCtrl:
        def __init__(self):
            self.vj = _FakeVj()

    ctrl = _FakeCtrl()
    rb1 = ensure_feeder_readback(ctrl)
    rb2 = ensure_feeder_readback(ctrl)
    assert rb1 is rb2
    assert hasattr(ctrl.vj, "_ac_feeder_readback_fn")
    packet = ctrl.vj.generateJoystickPosition(wAxisX=0, wAxisY=0, wAxisZ=0, lButtons=0)
    ctrl.vj.update(packet)
    assert rb1().update_ok is True


def test_start_first_training_dryrun_script():
    import subprocess

    script = REPO / "legacy" / "scripts" / "Start-First-Training.ps1"
    proc = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-DryRun",
            "-MaxSteps",
            "5000",
            "-DurationMinutes",
            "5",
        ],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Start-First-Training DryRun: OK" in proc.stdout


def main() -> int:
    tests = [
        test_episode_reset_neutral_then_first_gear,
        test_episode_reset_ten_consecutive,
        test_episode_reset_allows_controls_live_bypass,
        test_episode_reset_failed_controls_handle_changed,
        test_episode_reset_failed_not_driving,
        test_learner_status_warmup_not_reached,
        test_learner_status_warmup_reached_updates,
        test_learner_warmup_counters_global_vs_training_step,
        test_learner_training_step_tracks_updates,
        test_learner_stop_request_saves_checkpoint,
        test_learner_pid_in_status,
        test_build_metadata_rejects_8dim_with_125_schema,
        test_publish_8dim_model_refused,
        test_publish_125dim_model_accepted,
        test_stale_learner_status_archived,
        test_learner_status_session_id_matches_not_ignored,
        test_learner_status_write_survives_windows_lock,
        test_reset_update_vjd_false_then_reacquire_succeeds,
        test_reset_update_vjd_always_false_fails_safe,
        test_reset_same_controls_object_id,
        test_ensure_feeder_readback_single_owner,
        test_start_first_training_dryrun_script,
    ]
    for fn in tests:
        fn()
        print("OK", fn.__name__)
    print("Test-Episode-Reset-Offline: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
