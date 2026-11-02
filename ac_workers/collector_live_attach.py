"""Attach collector to an already-running AC session (no initial env.reset)."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np

from AssettoCorsaEnv.gear_shift import gear_raw, is_first, is_neutral, resolve_shift_up_mapping

from ac_workers.control_smoke_test import motion_telemetry_ready
from ac_workers import pipeline_timeline
from ac_workers.drive_truth import is_drive_really_active
from ac_workers.post_reset_drive import (
    DRIVE_INPUT_NOT_DELIVERED_ERROR,
    wait_for_driving_after_reset,
)
from ac_workers.vjoy_session import (
    attach_feeder_readback,
    apply_neutral_controls,
    controls_from_env,
    device_acquired,
    enable_env_local_vjoy,
    ensure_feeder_readback,
    ensure_vjoy_feeder,
    expected_logical_neutral,
    get_vjd_status,
    neutralize_controls_handle,
    prep_vjoy_device,
    reacquire_vjoy_device,
    release_env_local_vjoy,
    write_and_verify,
)

logger = logging.getLogger(__name__)

from ac_workers.canonical_controls import VJOY_WRITE_LOCK as _VJOY_STARTUP_LOCK

MAX_TELEMETRY_WAIT_SEC = 15.0
TELEMETRY_POLL_SEC = 0.1
# Drive activation waits up to 120s for the AC window, then UI settle + clicks.
# The collector starts before that click, so this wait must outlast the launcher.
MAX_MANAGEMENT_WAIT_SEC = 210.0
MANAGEMENT_POLL_SEC = 0.25
MANAGEMENT_STARTUP_FAILED_ERROR = "driving session active but management port {0} did not open"
DRIVE_INPUT_ENV = "AC_AI_DRIVE_INPUT_DELIVERED"
# Launcher may click Drive then scan (worker 5: ~27s). Wait long enough that a
# Bounded wait for the PLUGIN to report drive started (brake released). This is a
# real synchronization on physics, not a log race, so it does not need to be long:
# a short window surfaces a genuinely failed Drive click instead of masking a race.
DRIVE_INPUT_WAIT_SEC = 45.0


def _copy_startup_diagnostics(driver: Any, report: dict[str, Any]) -> None:
    diag = driver.startup_diagnostics()
    report.update(diag)


def initial_startup_validated(runtime: Any, *, max_telemetry_age_ms: float = 2000.0) -> bool:
    """True after successful first vJoy startup attach (controls_live bypass authority)."""
    if not bool(getattr(runtime, "initial_session_attached", False)):
        return False
    if not bool(getattr(runtime, "vjoy_device_acquired", False)):
        return False
    if not bool(getattr(runtime, "vjoy_axis_verified", False)):
        return False
    if not bool(getattr(runtime, "startup_telemetry_fresh", False)):
        return False
    env = getattr(runtime, "env", None)
    if env is None:
        return False
    try:
        age_ms = float(runtime.telemetry_age_ms())
    except (AttributeError, TypeError, ValueError):
        return False
    if age_ms > float(max_telemetry_age_ms):
        return False
    try:
        gear = int(gear_raw(env.client.state))
    except (AttributeError, KeyError, TypeError, ValueError):
        return False
    return not is_neutral(gear)


def _canonical_startup_write(driver: Any, *, writer: str = "startup_write") -> None:
    if driver._startup_override_active and driver._startup_override_action is not None:
        steer, acc, brake = driver._startup_override_action
        driver._write_local_controls(steer, acc, brake, writer=writer)
        return
    driver.apply_local_controls()


def _launcher_log_path(worker_id: int) -> Path | None:
    session_id = os.environ.get("AC_AI_SESSION_ID", "").strip()
    if not session_id:
        return None
    repo = Path(__file__).resolve().parents[1]
    name = "worker_{0}_{1}.log".format(worker_id, session_id)
    for sub in ("ac_workers/logs", "logs"):
        path = repo / sub / name
        if path.is_file():
            return path
    return None


def _plugin_drive_ready(state: dict | None) -> bool:
    """True when the in-game plugin says the car left the Drive menu."""
    if not state:
        return False
    active, _reason = is_drive_really_active(
        state,
        vjoy_readback_ok=True,
        telemetry_age_sec=0.0,
    )
    if active:
        return True
    # Align with launcher Test-AcDriveEngaged (driving + not menu_brake_hold).
    if state.get("driving") is True and not state.get("menu_brake_hold", True):
        return True
    return False


def _mgmt_drive_physically_ready(mgmt: Any | None) -> bool:
    """THE only signal that the launcher's Drive trigger actually started driving.

    The in-game plugin is the single source of truth: sim live + brake released
    (car left the Drive menu). The launcher click is just a trigger; whatever
    path it took (click, scan, already-active) is irrelevant.
    """
    if mgmt is None:
        return False
    try:
        state = mgmt.get_driving_state()
    except (AttributeError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return _plugin_drive_ready(state if isinstance(state, dict) else None)


def drive_input_status(worker_id: int) -> str:
    """DEBUG-ONLY launcher log/env summary. NOT used to gate worker startup.

    Kept so operators can inspect what the launcher reported, but the business
    logic decides drive readiness solely from the plugin
    (_mgmt_drive_physically_ready). Returns 'delivered', 'not_delivered' or
    'pending'.
    """
    val = os.environ.get(DRIVE_INPUT_ENV, "").strip().lower()
    if val in ("1", "true", "yes", "on"):
        return "delivered"
    log_path = _launcher_log_path(worker_id)
    if log_path is None:
        return "pending"
    text = log_path.read_text(encoding="utf-8", errors="ignore")
    if "Drive confirmed" in text or "driving state confirmed" in text:
        return "delivered"
    if "Drive input not delivered" in text:
        return "not_delivered"
    return "pending"


def is_drive_input_delivered(worker_id: int) -> bool:
    """DEBUG-ONLY. See drive_input_status: never gate startup on this."""
    return drive_input_status(worker_id) == "delivered"


def wait_drive_input_delivered(
    worker_id: int,
    *,
    mgmt: Any | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    timeout_sec: float = DRIVE_INPUT_WAIT_SEC,
    poll_sec: float = 0.5,
    monotonic: Callable[[], float] | None = None,
) -> bool:
    """Block until the PLUGIN confirms the car left the Drive menu.

    Single source of truth: the in-game plugin via the management port
    (is_drive_really_active -> brake released). Launcher logs / the
    AC_AI_DRIVE_INPUT_DELIVERED env var are NEVER consulted here; they only feed
    the debug-only drive_input_status(). A bounded timeout (DRIVE_INPUT_WAIT_SEC)
    surfaces a genuinely failed click (AC never left the menu) rather than masking
    a race with a long wait.
    """
    sleep_fn = sleep_fn or time.sleep
    monotonic = monotonic or time.monotonic
    deadline = monotonic() + timeout_sec
    while True:
        if _mgmt_drive_physically_ready(mgmt):
            return True
        if monotonic() >= deadline:
            return _mgmt_drive_physically_ready(mgmt)
        sleep_fn(poll_sec)


def _mgmt_port_from_env(env: Any) -> int:
    cfg = getattr(env, "config", None)
    if cfg is not None:
        return int(getattr(cfg, "simulation_management_server_port", 2347))
    client = getattr(env, "client", None)
    if client is not None:
        return int(getattr(client, "simulation_management_server_port", 2347))
    return 2347


def wait_for_management_port(
    mgmt_port: int,
    runtime: Any,
    *,
    sleep_fn: Callable[[float], None] | None = None,
    tcp_open_fn: Callable[[str, int], bool] | None = None,
    timeout_sec: float = MAX_MANAGEMENT_WAIT_SEC,
    env: Any | None = None,
) -> tuple[bool, str]:
    from ac_workers.worker_handshake import tcp_open

    sleep_fn = sleep_fn or time.sleep
    tcp_open_fn = tcp_open_fn or tcp_open
    runtime.set_phase("waiting_for_management")
    runtime.write_minimal_status()
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if tcp_open_fn("127.0.0.1", mgmt_port):
            return True, ""
        if env is not None:
            _pulse_vjoy_neutral_if_feeding(env)
        runtime.write_minimal_status()
        sleep_fn(MANAGEMENT_POLL_SEC)
    return False, MANAGEMENT_STARTUP_FAILED_ERROR.format(mgmt_port)


def wait_for_fresh_telemetry(
    runtime: Any,
    env: Any,
    *,
    timeout_sec: float = MAX_TELEMETRY_WAIT_SEC,
    sleep_fn: Callable[[float], None] | None = None,
) -> tuple[bool, str]:
    sleep_fn = sleep_fn or time.sleep
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            env.client.get_servers_input()
            runtime.last_telemetry_ts = time.time()
            runtime.telemetry_snapshot = {
                "steer": float(env.client.state.get("steer", 0.0)),
                "gas": float(env.client.state.get("gas", 0.0)),
                "brake": float(env.client.state.get("brake", 0.0)),
                "speed": float(env.client.state.get("speed", 0.0)),
            }
            runtime._telemetry_ready = True
            ok, reason = motion_telemetry_ready(runtime, env)
            if ok:
                return True, ""
        except (AttributeError, KeyError, OSError, TypeError, ValueError) as exc:
            reason = "telemetry unreadable: {0}".format(exc)
        else:
            reason = reason or "telemetry not fresh"
        if env is not None:
            _pulse_vjoy_neutral_if_feeding(env)
        runtime.write_minimal_status()
        sleep_fn(TELEMETRY_POLL_SEC)
    return False, reason or "telemetry not fresh before timeout"


def bootstrap_env_observation(env: Any) -> Any:
    """Build first observation vector without env.reset() or management reset."""
    if env.n_episodes < 1:
        env.n_episodes = 1
        env.static_info = env.client.simulation_management.get_static_info()
        env.track_length = env.static_info["TrackLength"]
        env.ac_mod_config = env.client.simulation_management.get_config()

    env._safe_emergency_active = False
    env._safe_recover_used = False
    env._force_episode_done = False
    env._pending_shift_fail = False
    env.termination_counter = int(env.low_speed_timeout_sec * env.ctrl_rate)
    env.episode_saved = False
    env.is_out_of_track = False
    env.start_actions = np.array([0.0, -1.0, -1.0])
    env.current_actions = env.start_actions.copy()
    env.ep_steps = 0
    env.states = []

    for _ in range(2):
        obs, _, _, _ = env.step(env.start_actions)
    obs, _, _, info = env.step(env.start_actions)
    env.info = info
    env.ep_reward = 0.0
    env.ep_steps = 0
    if hasattr(env, "_reset_termination_trackers"):
        env._reset_termination_trackers()
    env._progress_previous_state = None
    return obs


RESET_CONSISTENT_SAMPLES = 3
RESET_CONSISTENT_MAX_STEPS = 100


def wait_for_consistent_reset_obs(env: Any) -> tuple[bool, Any]:
    """Require a few consecutive observations that are not a shared-memory collision."""
    from AssettoCorsaEnv.telemetry_consistency import stationary_signal_inconsistent

    previous = None
    streak = 0
    obs = None
    # Drop pre-reset UDP observations before checking the new episode.
    drain = getattr(getattr(env, "client", None), "discard_pending_telemetry", None)
    if callable(drain):
        drain()
    for _ in range(RESET_CONSISTENT_MAX_STEPS):
        obs, _, _, info = env.step(getattr(env, "start_actions", np.array([0.0, -1.0, -1.0])))
        env.info = info
        state = getattr(env, "state", None)
        # Repeated packets are not new evidence, nor a consistency failure.
        if previous is not None and state is not None and state.get("timestamp_ac") == previous.get("timestamp_ac"):
            continue
        if (
            previous is not None
            and state is not None
            and float(state.get("timestamp_ac", 0)) > float(previous.get("timestamp_ac", 0))
            and not state.get("out_of_track", False)
            and not stationary_signal_inconsistent(previous, state)
        ):
            streak += 1
            if streak >= RESET_CONSISTENT_SAMPLES - 1:
                env.ep_steps = 0
                return True, obs
        else:
            streak = 0
        previous = dict(state) if state else None
    env.ep_steps = 0
    logger.warning("reset observations rejected: last=%s streak=%s", previous, streak)
    return False, obs


def _pulse_vjoy_neutral_if_feeding(env: Any) -> None:
    """Keep neutral vJoy output while waiting for mgmt/telemetry (feeder already enabled)."""
    controls = getattr(getattr(env, "client", None), "controls", None)
    if controls is None or not getattr(controls, "local_vjoy_feeder_enabled", lambda: False)():
        return
    lc = getattr(controls, "local_controls", None)
    if lc is None:
        return
    try:
        apply_neutral_controls(lc)
        if hasattr(controls, "apply_local_controls"):
            controls.apply_local_controls()
    except Exception:
        pass


def early_acquire_vjoy_feeder(
    env: Any,
    runtime: Any,
    worker_id: int,
    *,
    get_status_fn: Callable[[int], int] | None = None,
) -> tuple[bool, str, int]:
    """Acquire vJoy and feed neutral before AC DirectInput binds the device."""
    from AssettoCorsaEnv.gear_shift import resolve_shift_up_mapping

    gear_mapping = getattr(env, "_gear_shift_mapping", None) or resolve_shift_up_mapping(worker_id)
    runtime.gear_mapping = gear_mapping
    device_id = int(gear_mapping.get("vjoy_device_id", runtime.vjoy_device))
    ctrl = controls_from_env(env)
    readback = ensure_feeder_readback(ctrl)
    if device_acquired(ctrl, device_id, get_status_fn=get_status_fn):
        enable_env_local_vjoy(env)
        with _VJOY_STARTUP_LOCK:
            apply_neutral_controls(ctrl)
            env.client.controls.apply_local_controls()
            ok, reason, _rb = write_and_verify(readback, expected_logical_neutral())
            if not ok:
                release_env_local_vjoy(env, device_id)
                return False, "neutral axis readback: {0}".format(reason), device_id
        runtime.vjoy_device_acquired = True
        runtime.vjoy_axis_verified = True
        return True, "", device_id

    ok_feed, feed_err, acquired = ensure_vjoy_feeder(
        ctrl,
        device_id,
        get_status_fn=get_status_fn,
    )
    if not ok_feed or not acquired:
        err = feed_err or "vJoy device {0} not acquired".format(device_id)
        release_env_local_vjoy(env, device_id)
        return False, err, device_id

    enable_env_local_vjoy(env)
    with _VJOY_STARTUP_LOCK:
        apply_neutral_controls(ctrl)
        env.client.controls.apply_local_controls()
        ok, reason, _rb = write_and_verify(readback, expected_logical_neutral())
        if not ok:
            release_env_local_vjoy(env, device_id)
            return False, "neutral axis readback: {0}".format(reason), device_id
    runtime.vjoy_device_acquired = True
    runtime.vjoy_axis_verified = True
    return True, "", device_id


def run_vjoy_startup_validation(
    env: Any,
    worker_id: int,
    gear_mapping: dict[str, Any],
    device_id: int,
    *,
    runtime: Any | None = None,
    get_status_fn: Callable[[int], int] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
    allow_global_reset: bool = True,
) -> tuple[bool, str, dict[str, Any]]:
    sleep_fn = sleep_fn or time.sleep
    monotonic = monotonic or time.monotonic
    report: dict[str, Any] = {
        "worker_id": worker_id,
        "vjoy_device_id": device_id,
        "device_acquired": False,
        "axis_verified": False,
        "gear_up_pulse_sent": False,
        "initial_gear_engaged": False,
        "gear_raw_before": -1,
        "gear_raw_after": -1,
        "initial_gear_before_raw": -1,
        "initial_gear_after_raw": -1,
        "initial_gear_samples": [],
        "startup_throttle_assist_used": False,
        "startup_throttle_assist_fraction": 0.0,
        "startup_telemetry_fresh": False,
        "vjoy_neutralization_verified": False,
        "startup_controls_object_id": None,
        "startup_override_active": False,
        "startup_requested_internal_action": [],
        "startup_requested_pedal_fraction": 0.0,
        "startup_readback_pedal_fraction": 0.0,
        "startup_control_write_count": 0,
        "startup_neutral_overwrite_detected": False,
        "startup_last_control_writer": "",
        "startup_final_neutral_readback": {},
        "startup_override_active_final": False,
    }
    ctrl = controls_from_env(env)
    readback = ensure_feeder_readback(ctrl)

    try:
        if not gear_mapping.get("valid"):
            err = gear_mapping.get("error") or "GEARUP mapping invalid"
            report["error"] = err
            return False, err, report

        report["device_acquired"] = device_acquired(ctrl, device_id, get_status_fn=get_status_fn)
        if not report["device_acquired"]:
            from ac_workers import vjoy_session as _vjoy_session

            acquire_retries = (
                _vjoy_session.RESET_VJOY_ACQUIRE_RETRIES
                if not allow_global_reset
                else _vjoy_session.DEFAULT_VJOY_ACQUIRE_RETRIES
            )
            acquire_delay = (
                _vjoy_session.RESET_VJOY_ACQUIRE_DELAY_SEC
                if not allow_global_reset
                else _vjoy_session.DEFAULT_VJOY_ACQUIRE_DELAY_SEC
            )
            ok_feed, feed_err, acquired = ensure_vjoy_feeder(
                ctrl,
                device_id,
                get_status_fn=get_status_fn,
                allow_global_reset=allow_global_reset,
                retries=acquire_retries,
                delay_sec=acquire_delay,
            )
            report["device_acquired"] = acquired
            if not ok_feed:
                report["error"] = feed_err or "vJoy device {0} not acquired".format(device_id)
                release_env_local_vjoy(env, device_id)
                return False, report["error"], report
            report["device_acquired"] = device_acquired(ctrl, device_id, get_status_fn=get_status_fn)
        if not report["device_acquired"]:
            err = "vJoy device {0} not acquired".format(device_id)
            report["error"] = err
            release_env_local_vjoy(env, device_id)
            return False, err, report

        enable_env_local_vjoy(env)

        with _VJOY_STARTUP_LOCK:
            apply_neutral_controls(ctrl)
            env.client.controls.apply_local_controls()
            ok, reason, _rb = write_and_verify(readback, expected_logical_neutral())
            if not ok:
                err = "neutral axis readback: {0}".format(reason)
                report["error"] = err
                return False, err, report
            report["axis_verified"] = True

            # Gear is diagnostic only: AUTO_SHIFTER engages when driving starts.
            # Live telemetry is checked by attach/reset independently of gear.
            current_gear = int(gear_raw(env.client.state))
            report["gear_raw_before"] = current_gear
            report["initial_gear_before_raw"] = current_gear
            report["gear_raw_after"] = current_gear
            report["initial_gear_after_raw"] = current_gear
            report["initial_gear_samples"] = [current_gear]
            report["initial_gear_engaged"] = current_gear == 2
            report["startup_telemetry_fresh"] = True
            return True, "", report
    finally:
        try:
            with _VJOY_STARTUP_LOCK:
                n_ok, _, rb = neutralize_controls_handle(ctrl, readback)
                report["vjoy_neutralization_verified"] = n_ok
                if rb is not None:
                    report["startup_final_neutral_readback"] = dict(rb.last_logical)
                else:
                    report["startup_final_neutral_readback"] = dict(readback().last_logical)
                report["startup_override_active_final"] = False
                env.client.controls.apply_local_controls()
        except Exception:
            report["vjoy_neutralization_verified"] = False
            report["startup_override_active_final"] = False


def attach_live_session(
    env: Any,
    runtime: Any,
    worker_id: int,
    *,
    sleep_fn: Callable[[float], None] | None = None,
    get_status_fn: Callable[[int], int] | None = None,
) -> tuple[bool, str, Any | None]:
    from AssettoCorsaEnv.ac_client import TelemetryConnectionTimeout
    from AssettoCorsaEnv.gear_shift import resolve_shift_up_mapping
    from ac_workers.collector_startup_log import startup_log

    sleep_fn = sleep_fn or time.sleep

    def _publish_phase(phase: str) -> None:
        runtime.set_phase(phase)
        runtime.write_minimal_status()
        startup_log("collector attach: phase={0}".format(phase))

    _publish_phase("attaching_live_session")
    pipeline_timeline.mark(worker_id, pipeline_timeline.START)
    runtime.initial_session_attached = False
    runtime.vjoy_device_acquired = False
    runtime.vjoy_axis_verified = False
    runtime.initial_gear_engaged = False
    runtime.vjoy_neutralization_verified = False
    obs = None

    # Initial telemetry connect uses client.reset(send_reset=False) below.
    # Do not clear send_reset_at_start here: episode resets must still call ext_resetCar().
    # Force N→1st at every episode reset so a fresh/random policy that outputs low
    # throttle is not stuck in neutral (AUTO_SHIFTER needs more gas than random acts give).
    env.auto_shift_to_first = True

    gear_mapping = getattr(env, "_gear_shift_mapping", None) or resolve_shift_up_mapping(worker_id)
    runtime.gear_mapping = gear_mapping

    _publish_phase("vjoy_early_acquire")
    ok_early, early_err, device_id = early_acquire_vjoy_feeder(
        env,
        runtime,
        worker_id,
        get_status_fn=get_status_fn,
    )
    if not ok_early:
        runtime.set_phase("vjoy_startup_failed", early_err)
        runtime.write_minimal_status()
        return False, early_err, None
    _publish_phase("vjoy_early_acquire_complete")
    startup_log("collector attach: vJoy device {0} acquired early".format(device_id))

    mgmt_port = _mgmt_port_from_env(env)
    _publish_phase("management_connect_begin")
    # No log/env short-circuit here: wait_for_management_port returns immediately
    # when the port is already open, so the decision stays purely on the live port
    # state, never on what the launcher wrote.
    ok_mgmt, mgmt_reason = wait_for_management_port(
        mgmt_port, runtime, sleep_fn=sleep_fn, env=env
    )
    if not ok_mgmt:
        err = mgmt_reason or "management_port_unavailable"
        runtime.set_phase("attach_failed", err)
        runtime.write_minimal_status()
        return False, err, None
    _publish_phase("management_connect_complete")
    pipeline_timeline.mark(worker_id, pipeline_timeline.PLUGIN_CONNECTED)

    _publish_phase("telemetry_wait_begin")
    try:
        startup_log(
            "collector attach: telemetry connect host={0} port={1}".format(
                getattr(env.client, "server_host", "?"),
                getattr(env.client, "server_port", "?"),
            )
        )
        env.client.reset(send_reset=False, connection_timeout_sec=MAX_TELEMETRY_WAIT_SEC)
    except TelemetryConnectionTimeout as exc:
        err = str(exc)
        runtime.set_phase("attach_failed", err)
        runtime.write_minimal_status()
        return False, err, None

    ok, reason = wait_for_fresh_telemetry(runtime, env, sleep_fn=sleep_fn)
    if not ok:
        err = reason or "telemetry_not_received"
        if ok_mgmt:
            err = "management_connected_but_no_telemetry: {0}".format(err)
        runtime.set_phase("attach_failed", err)
        runtime.write_minimal_status()
        return False, err, None
    _publish_phase("first_telemetry_received")
    pipeline_timeline.mark(worker_id, pipeline_timeline.FIRST_TELEMETRY)

    runtime.set_phase("waiting_for_drive_input")
    runtime.write_minimal_status()
    drive_input_delivered = wait_drive_input_delivered(
        worker_id, mgmt=env.client.simulation_management, sleep_fn=sleep_fn
    )
    if not drive_input_delivered:
        runtime.set_phase("waiting_for_drive_failed", DRIVE_INPUT_NOT_DELIVERED_ERROR)
        runtime.write_minimal_status()
        return False, DRIVE_INPUT_NOT_DELIVERED_ERROR, None

    ok_drive, drive_state = wait_for_driving_after_reset(
        env.client.simulation_management,
        sleep_fn=sleep_fn,
        get_gear_fn=lambda: int(gear_raw(env.client.state)),
        require_non_neutral_gear=False,
        require_controls_live=False,
    )
    runtime.drive_state_report = drive_state
    runtime.controls_live_reported = drive_state.get("controls_live")
    runtime.controls_live_bypassed_for_initial_attach = drive_state.get("controls_live") is not True
    if not ok_drive:
        err = drive_state.get("reason") or "driving scene not confirmed before timeout"
        runtime.set_phase("waiting_for_drive_failed", err)
        runtime.write_minimal_status()
        return False, err, None

    gear_mapping = getattr(env, "_gear_shift_mapping", None) or resolve_shift_up_mapping(worker_id)
    runtime.gear_mapping = gear_mapping
    runtime.set_phase("vjoy_startup_validation")

    device_id = int(gear_mapping.get("vjoy_device_id", runtime.vjoy_device))
    ok_vjoy, vjoy_err, vjoy_report = run_vjoy_startup_validation(
        env,
        worker_id,
        gear_mapping,
        device_id,
        runtime=runtime,
        get_status_fn=get_status_fn,
        sleep_fn=sleep_fn,
    )
    runtime.vjoy_startup_report = vjoy_report
    runtime.vjoy_device_acquired = bool(vjoy_report.get("device_acquired"))
    runtime.vjoy_axis_verified = bool(vjoy_report.get("axis_verified"))
    runtime.initial_gear_engaged = bool(vjoy_report.get("initial_gear_engaged"))
    runtime.vjoy_neutralization_verified = bool(vjoy_report.get("vjoy_neutralization_verified"))
    runtime.initial_gear_before_raw = vjoy_report.get("initial_gear_before_raw")
    runtime.initial_gear_after_raw = vjoy_report.get("initial_gear_after_raw")
    runtime.initial_gear_samples = list(vjoy_report.get("initial_gear_samples") or [])
    runtime.startup_throttle_assist_used = bool(vjoy_report.get("startup_throttle_assist_used"))
    runtime.startup_throttle_assist_fraction = float(
        vjoy_report.get("startup_throttle_assist_fraction", 0.0)
    )
    runtime.startup_telemetry_fresh = bool(vjoy_report.get("startup_telemetry_fresh"))
    runtime.startup_controls_object_id = vjoy_report.get("startup_controls_object_id")
    runtime.startup_override_active = bool(vjoy_report.get("startup_override_active"))
    runtime.startup_requested_internal_action = list(
        vjoy_report.get("startup_requested_internal_action") or []
    )
    runtime.startup_requested_pedal_fraction = float(
        vjoy_report.get("startup_requested_pedal_fraction", 0.0)
    )
    runtime.startup_readback_pedal_fraction = float(
        vjoy_report.get("startup_readback_pedal_fraction", 0.0)
    )
    runtime.startup_control_write_count = int(vjoy_report.get("startup_control_write_count", 0))
    runtime.startup_neutral_overwrite_detected = bool(
        vjoy_report.get("startup_neutral_overwrite_detected")
    )
    runtime.startup_last_control_writer = str(vjoy_report.get("startup_last_control_writer") or "")
    runtime.startup_final_neutral_readback = dict(
        vjoy_report.get("startup_final_neutral_readback") or {}
    )
    runtime.startup_override_active_final = bool(
        vjoy_report.get("startup_override_active_final", False)
    )

    if not ok_vjoy:
        err = vjoy_err or "vjoy startup validation failed"
        release_env_local_vjoy(env, device_id)
        runtime.set_phase("vjoy_startup_failed", err)
        runtime.initial_gear_engaged = False
        runtime.write_minimal_status()
        return False, err, None

    # Final authoritative gate before handing control to the policy: the single
    # truth source must agree the car is really on track. vJoy readback is proven
    # (axis_verified above); telemetry freshness was just enforced by
    # wait_for_fresh_telemetry, so the meaningful checks here are sim-live + brake
    # released (out of the Drive menu). This is the ONE decision for DRIVE.
    ok_truth, truth_reason = _assert_drive_truth(env, runtime)
    if not ok_truth:
        release_env_local_vjoy(env, device_id)
        runtime.set_phase("waiting_for_drive_failed", truth_reason)
        runtime.write_minimal_status()
        return False, truth_reason, None

    pipeline_timeline.mark(worker_id, pipeline_timeline.DRIVE_CONFIRMED)
    pipeline_timeline.mark(worker_id, pipeline_timeline.VJOY_READY)
    _publish_phase("live_session_attached")
    runtime.initial_session_attached = True
    obs = bootstrap_env_observation(env)
    consistent, obs = wait_for_consistent_reset_obs(env)
    if not consistent:
        neutralize_env_controls(env)
        runtime.set_phase("attach_failed", "initial_observations_inconsistent")
        return False, "initial_observations_inconsistent", None
    env._reset_termination_trackers()
    env._progress_previous_state = None
    runtime.last_observation = obs.ravel().tolist()
    runtime.set_phase("running")
    pipeline_timeline.mark(worker_id, pipeline_timeline.IDLE_READY)
    return True, "", obs


def _assert_drive_truth(
    env: Any,
    runtime: Any,
    *,
    sleep_fn: Callable[[float], None] | None = None,
    attempts: int = 8,
    poll_sec: float = 0.25,
) -> tuple[bool, str]:
    """Poll the single truth source until it confirms DRIVE (or times out)."""
    sleep_fn = sleep_fn or time.sleep
    mgmt = env.client.simulation_management
    reason = "drive truth not confirmed"
    for _ in range(max(1, attempts)):
        try:
            state = mgmt.get_driving_state()
        except (AttributeError, OSError, TypeError, ValueError):
            state = {}
        ok, reason = is_drive_really_active(
            state,
            vjoy_readback_ok=bool(getattr(runtime, "vjoy_axis_verified", False)),
            telemetry_age_sec=0.0,
        )
        if ok:
            return True, ""
        sleep_fn(poll_sec)
    return False, reason


RESET_CONTROLS_HANDLE_CHANGED_ERROR = "vJoy controls handle changed since initial attach"
RESET_MANAGEMENT_RESUME_FAILED_ERROR = "management resume failed after episode reset"
RESET_SESSION_NOT_DRIVING_ERROR = "AC session not driving after episode reset"
RESET_REQUIRES_INITIAL_ATTACH_ERROR = "episode reset requires validated initial attach"


def _apply_vjoy_report(runtime: Any, vjoy_report: dict[str, Any]) -> None:
    runtime.vjoy_startup_report = vjoy_report
    runtime.vjoy_device_acquired = bool(vjoy_report.get("device_acquired"))
    runtime.vjoy_axis_verified = bool(vjoy_report.get("axis_verified"))
    runtime.initial_gear_engaged = bool(vjoy_report.get("initial_gear_engaged"))
    runtime.vjoy_neutralization_verified = bool(vjoy_report.get("vjoy_neutralization_verified"))
    runtime.initial_gear_before_raw = vjoy_report.get("initial_gear_before_raw")
    runtime.initial_gear_after_raw = vjoy_report.get("initial_gear_after_raw")
    runtime.initial_gear_samples = list(vjoy_report.get("initial_gear_samples") or [])
    runtime.startup_throttle_assist_used = bool(vjoy_report.get("startup_throttle_assist_used"))
    runtime.startup_telemetry_fresh = bool(vjoy_report.get("startup_telemetry_fresh"))


def _wait_reset_session(
    env: Any,
    runtime: Any,
    *,
    sleep_fn: Callable[[float], None] | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    sleep_fn = sleep_fn or time.sleep
    mgmt = env.client.simulation_management
    mgmt_port = _mgmt_port_from_env(env)
    ok_mgmt, mgmt_reason = wait_for_management_port(mgmt_port, runtime, sleep_fn=sleep_fn)
    if not ok_mgmt:
        return False, mgmt_reason, {}
    try:
        mgmt.send_resume_drive()
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        return False, "{0}: {1}".format(RESET_MANAGEMENT_RESUME_FAILED_ERROR, exc), {}
    pipeline_timeline.mark(int(getattr(runtime, "worker_id", -1)), pipeline_timeline.RESET_TELEPORT_SENT)
    ok_tel, tel_reason = wait_for_fresh_telemetry(runtime, env, sleep_fn=sleep_fn)
    if not ok_tel:
        return False, tel_reason, {}
    pipeline_timeline.mark(int(getattr(runtime, "worker_id", -1)), pipeline_timeline.RESET_TELEMETRY_FRESH)

    def _telemetry_age_sec() -> float:
        # The normal collector loop is paused during this wait. Receive here
        # instead of measuring the age of the packet read before the wait.
        try:
            env.client.get_servers_input(deadline=time.monotonic() + 0.5)
            runtime.last_telemetry_ts = time.time()
        except OSError:
            pass
        return float(runtime.telemetry_age_ms()) / 1000.0

    def _vjoy_readback_ok() -> bool:
        return bool(getattr(runtime, "vjoy_axis_verified", False))

    _wid = int(getattr(runtime, "worker_id", -1))
    # Pin the epoch this wait belongs to. Every event recorded here carries it,
    # so a stale plugin reading from a previous reset is flagged, not attributed.
    _rid = pipeline_timeline.current_reset_id(_wid)

    def _observe_plugin(state: dict) -> None:
        # Attribute what the plugin reports back so we can tell a plugin-side
        # stall (never reports sim-live / driving) from a collector-side gate.
        # Also fingerprint the AC shared memory: if two workers report the same
        # packet_id, their AC instances share one acpmf_physics mapping.
        pid = state.get("physics_packet_id")
        if pid is not None:
            pipeline_timeline.record_shm_fingerprint(
                _wid, int(pid), int(state.get("session_clock_ms", 0) or 0)
            )
        if state.get("sim_live"):
            pipeline_timeline.mark(
                _wid, pipeline_timeline.PLUGIN_SIM_LIVE,
                component=pipeline_timeline.COMPONENT_PLUGIN,
                expected_reset_id=_rid,
            )
        if state.get("driving"):
            pipeline_timeline.mark(
                _wid, pipeline_timeline.PLUGIN_DRIVING_TRUE,
                component=pipeline_timeline.COMPONENT_PLUGIN,
                expected_reset_id=_rid,
            )

    ok_drive, drive_state = wait_for_driving_after_reset(
        mgmt,
        sleep_fn=sleep_fn,
        get_gear_fn=lambda: int(gear_raw(env.client.state)),
        require_non_neutral_gear=False,
        require_controls_live=False,
        vjoy_readback_fn=_vjoy_readback_ok,
        telemetry_age_fn=_telemetry_age_sec,
        on_poll=_observe_plugin,
        max_wait_sec=25.0,
    )
    if ok_drive:
        pipeline_timeline.mark(
            _wid,
            pipeline_timeline.RESET_DRIVING_CONFIRMED,
            expected_reset_id=_rid,
        )
    runtime.drive_state_report = drive_state
    runtime.controls_live_reported = drive_state.get("controls_live")
    runtime.controls_live_bypassed_for_initial_attach = drive_state.get("controls_live") is not True
    if not ok_drive:
        reason = drive_state.get("reason") or RESET_SESSION_NOT_DRIVING_ERROR
        return False, reason, drive_state
    return True, "", drive_state


def _apply_reset_diag(runtime: Any, diag: dict[str, Any]) -> None:
    runtime.reset_controls_object_id = diag.get("reset_controls_object_id")
    runtime.reset_vjoy_device_status_before = diag.get("reset_vjoy_device_status_before")
    runtime.reset_vjoy_reacquire_attempted = bool(diag.get("reset_vjoy_reacquire_attempted", False))
    runtime.reset_vjoy_reacquire_succeeded = bool(diag.get("reset_vjoy_reacquire_succeeded", False))
    runtime.reset_update_vjd_attempts = int(diag.get("reset_update_vjd_attempts", 0))
    runtime.reset_neutralization_verified = bool(diag.get("reset_neutralization_verified", False))
    runtime.reset_gear_before_raw = diag.get("reset_gear_before_raw")
    runtime.reset_gear_after_raw = diag.get("reset_gear_after_raw")
    if diag.get("reset_last_error"):
        runtime.reset_last_error = str(diag["reset_last_error"])


def run_episode_reset(
    env: Any,
    runtime: Any,
    worker_id: int,
    *,
    sleep_fn: Callable[[float], None] | None = None,
    get_status_fn: Callable[[int], int] | None = None,
) -> tuple[bool, str, Any | None]:
    """Reuse validated vJoy path after env.reset() without relaunching AC/CM."""
    sleep_fn = sleep_fn or time.sleep
    if not getattr(runtime, "initial_session_attached", False):
        runtime.set_phase("reset_failed", RESET_REQUIRES_INITIAL_ATTACH_ERROR)
        return False, RESET_REQUIRES_INITIAL_ATTACH_ERROR, None
    try:
        controls = env.client.controls
        lc = getattr(controls, "local_controls", None)
        current_id = id(lc) if lc is not None else id(controls)
    except AttributeError:
        runtime.set_phase("reset_failed", RESET_CONTROLS_HANDLE_CHANGED_ERROR)
        return False, RESET_CONTROLS_HANDLE_CHANGED_ERROR, None
    prior_id = getattr(runtime, "startup_controls_object_id", None)
    if prior_id is not None and prior_id != current_id:
        runtime.set_phase("reset_failed", RESET_CONTROLS_HANDLE_CHANGED_ERROR)
        return False, RESET_CONTROLS_HANDLE_CHANGED_ERROR, None

    reset_diag: dict[str, Any] = {
        "reset_controls_object_id": current_id,
        "reset_vjoy_device_status_before": None,
        "reset_vjoy_reacquire_attempted": False,
        "reset_vjoy_reacquire_succeeded": False,
        "reset_update_vjd_attempts": 0,
        "reset_neutralization_verified": False,
        "reset_gear_before_raw": None,
        "reset_gear_after_raw": None,
        "reset_last_error": "",
    }
    gear_mapping = getattr(env, "_gear_shift_mapping", None) or resolve_shift_up_mapping(worker_id)
    device_id = int(gear_mapping.get("vjoy_device_id", getattr(runtime, "vjoy_device", 1)))
    try:
        ctrl = controls_from_env(env)
        reset_diag["reset_vjoy_device_status_before"] = get_vjd_status(device_id)
    except Exception:
        reset_diag["reset_vjoy_device_status_before"] = -1
    _apply_reset_diag(runtime, reset_diag)

    runtime.set_phase("reset_waiting_for_session")
    with _VJOY_STARTUP_LOCK:
        ok_neutral, neutral_err = neutralize_env_controls(env, device_id=device_id, diag=reset_diag)
        _apply_reset_diag(runtime, reset_diag)
        if not ok_neutral:
            runtime.set_phase("reset_failed", neutral_err)
            return False, neutral_err or "neutralization failed before episode reset", None
    pipeline_timeline.mark(worker_id, pipeline_timeline.RESET_NEUTRALIZED)

    ok_session, session_err, _drive_state = _wait_reset_session(env, runtime, sleep_fn=sleep_fn)
    if not ok_session:
        neutralize_env_controls(env, device_id=device_id, diag=reset_diag)
        _apply_reset_diag(runtime, reset_diag)
        runtime.set_phase("reset_failed", session_err)
        return False, session_err, None

    runtime.set_phase("reset_vjoy_validation")
    sleep_fn(0.12 * (int(worker_id) % 6))
    ok_vjoy, vjoy_err, vjoy_report = run_vjoy_startup_validation(
        env,
        worker_id,
        gear_mapping,
        device_id,
        runtime=runtime,
        get_status_fn=get_status_fn,
        sleep_fn=sleep_fn,
        allow_global_reset=False,
    )
    _apply_vjoy_report(runtime, vjoy_report)
    reset_diag["reset_gear_before_raw"] = vjoy_report.get("initial_gear_before_raw")
    reset_diag["reset_gear_after_raw"] = vjoy_report.get("initial_gear_after_raw")
    if not ok_vjoy:
        reset_err = vjoy_err or "vjoy startup validation failed after reset"
        reset_diag["reset_last_error"] = reset_err
        _apply_reset_diag(runtime, reset_diag)
        neutralize_env_controls(env, device_id=device_id, diag=reset_diag)
        _apply_reset_diag(runtime, reset_diag)
        runtime.set_phase("reset_failed", reset_err)
        return False, reset_err, None
    pipeline_timeline.mark(worker_id, pipeline_timeline.RESET_VJOY_VALIDATED)

    reset_diag["reset_neutralization_verified"] = bool(
        vjoy_report.get("vjoy_neutralization_verified", False)
    )
    _apply_reset_diag(runtime, reset_diag)
    runtime.set_phase("reset_ready")
    obs = bootstrap_env_observation(env)
    ok_obs, consistent_obs = wait_for_consistent_reset_obs(env)
    if consistent_obs is not None:
        obs = consistent_obs
    if not ok_obs:
        runtime.set_phase("reset_failed", "reset_observations_inconsistent")
        neutralize_env_controls(env)
        return False, "reset_observations_inconsistent", None
    env._reset_termination_trackers()
    env._progress_previous_state = None
    runtime.last_observation = obs.ravel().tolist() if hasattr(obs, "ravel") else list(obs)
    runtime.set_phase("running")
    return True, "", obs


def neutralize_env_controls(
    env: Any | None,
    *,
    device_id: int | None = None,
    diag: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    if env is None:
        return False, "env not available"
    try:
        ctrl = controls_from_env(env)
        readback = ensure_feeder_readback(ctrl)
        if device_id is None:
            device_id = int(getattr(ctrl.vj, "reference", 1))
        if diag is not None:
            diag["reset_controls_object_id"] = id(ctrl)
            if diag.get("reset_vjoy_device_status_before") is None:
                diag["reset_vjoy_device_status_before"] = get_vjd_status(device_id)
        return neutralize_controls_handle(
            ctrl,
            readback,
            device_id=device_id,
            allow_reacquire=True,
            diag=diag,
        )[:2]
    except Exception as exc:
        if diag is not None:
            diag["reset_last_error"] = str(exc)
        return False, str(exc)
