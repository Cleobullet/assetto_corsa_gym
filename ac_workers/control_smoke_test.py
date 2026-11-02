"""vJoy control-chain smoke test (AC_AI_CONTROL_SMOKE_TEST=1, worker 0 only)."""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable

import numpy as np

from AssettoCorsaEnv.ac_env import PAST_ACTIONS_WINDOW
from AssettoCorsaEnv.gear_shift import gear_raw
from AssettoCorsaEnv.training_safety import BRAKE_INTERNAL_OFF, pedal_to_internal

logger = logging.getLogger(__name__)

ENV_VAR = "AC_AI_CONTROL_SMOKE_TEST"
WORKER_ID_ONLY = 0
MAX_DURATION_SEC = 3.0
MAX_OBS_WAIT_SEC = 5.0
MAX_LOOP_ITERATIONS = 200
THROTTLE_PEDAL_FRACTION = 0.20
TELEMETRY_MAX_AGE_MS = 2000.0
POST_DRIVE_SETTLE_SEC = 1.0
BASELINE_SAMPLE_COUNT = 3
BASELINE_SAMPLE_GAP_SEC = 0.1
MIN_SPEED_KMH = 1.0
MIN_SPEED_HIGH_SAMPLES = 2
MIN_LAP_DISTANCE_DELTA_M = 0.25
RPM_ENGINE_DELTA = 100.0

# From AssettoCorsaEnv.ac_env.set_actions -> client.controls.set_controls(steer, acc, brake)
# -> car_control.Controls.setJoy(wAxisX=steer, wAxisY=acc, wAxisZ=brake).
ACTION_AXIS_ORDER = ("steer", "acc", "brake")

# Direct vJoy control does not need a full RL observation vector: ac_env.get_obs() pads
# past-action history with zeros when len(env.states) < PAST_ACTIONS_WINDOW.


def is_enabled(worker_id: int) -> bool:
    if worker_id != WORKER_ID_ONLY:
        return False
    from pathlib import Path
    request = Path(__file__).resolve().parent / "status" / "single_car_test.request"
    if request.exists():
        fresh = time.time() - request.stat().st_mtime < 600
        request.unlink()
        if fresh:
            return True
    val = os.environ.get(ENV_VAR, "").strip().lower()
    return val in ("1", "true", "yes", "on")


def action_conversion_info() -> dict[str, Any]:
    return {
        "action_axis_order": list(ACTION_AXIS_ORDER),
        "internal_indices": {"steer": 0, "acc": 1, "brake": 2},
        "pedal_fraction": "physical 0..1; internal = 2*pedal - 1 (OFF at -1.0)",
        "vjoy_path": (
            "apply_canonical_controls(local_controls, steer, throttle, brake) -> "
            "Controls.set_controls(internal) -> setJoy (same path as training)"
        ),
    }


def build_requested_internal_action() -> np.ndarray:
    """Internal [steer, acc, brake] for 0% steer, 20% throttle, brake released."""
    return np.array(
        [0.0, pedal_to_internal(THROTTLE_PEDAL_FRACTION), BRAKE_INTERNAL_OFF],
        dtype=np.float64,
    )


def neutral_action(env: Any) -> np.ndarray:
    start = getattr(env, "start_actions", None)
    if start is not None:
        return np.array(start, dtype=np.float64).copy()
    return np.array([0.0, -1.0, -1.0], dtype=np.float64)


def read_motion_sample(env: Any) -> dict[str, float | int | None]:
    state = env.client.state
    info = getattr(env, "info", None) or {}
    speed = float(info.get("speed", state.get("speed", 0.0)))
    rpm = float(state.get("RPM", state.get("rpm", 0.0)))
    lap_raw = state.get("LapDist", state.get("lapDistance"))
    lap_distance = None if lap_raw is None else float(lap_raw)
    return {
        "speed": speed,
        "rpm": rpm,
        "gear": int(gear_raw(state)),
        "lap_distance": lap_distance,
        "gas": float(state.get("accStatus", state.get("gas", 0.0))),
        "brake": float(state.get("brakeStatus", state.get("brake", 0.0))),
        "world_position_x": state.get("world_position_x"),
        "world_position_y": state.get("world_position_y"),
    }


def collect_stable_baseline(
    env: Any,
    *,
    sleep_fn: Callable[[float], None],
    sample_count: int = BASELINE_SAMPLE_COUNT,
    gap_sec: float = BASELINE_SAMPLE_GAP_SEC,
) -> dict[str, float | int | None]:
    neutral = neutral_action(env)
    samples: list[dict[str, float | int | None]] = []
    for _ in range(sample_count):
        env.set_actions(neutral)
        env.client.controls.apply_local_controls()
        samples.append(read_motion_sample(env))
        sleep_fn(gap_sec)
    return samples[-1]


def evaluate_motion_outcome(
    before: dict[str, float | int | None],
    after: dict[str, float | int | None],
    *,
    max_speed: float,
    max_rpm: float,
    min_lap_distance: float | None,
    max_lap_distance: float | None,
    speed_high_samples: int,
) -> tuple[str, dict[str, Any]]:
    before_lap = before.get("lap_distance")
    after_lap = after.get("lap_distance")
    distance_delta = None
    if before_lap is not None and after_lap is not None:
        distance_delta = float(after_lap) - float(before_lap)

    engine_response = (
        float(max_rpm) - float(before["rpm"]) >= RPM_ENGINE_DELTA
        or float(after["rpm"]) - float(before["rpm"]) >= RPM_ENGINE_DELTA
    )
    vehicle_moved = False
    if speed_high_samples >= MIN_SPEED_HIGH_SAMPLES:
        vehicle_moved = True
    elif distance_delta is not None and distance_delta > MIN_LAP_DISTANCE_DELTA_M:
        vehicle_moved = True
    elif min_lap_distance is not None and max_lap_distance is not None:
        if (float(max_lap_distance) - float(min_lap_distance)) > MIN_LAP_DISTANCE_DELTA_M:
            vehicle_moved = True

    metrics: dict[str, Any] = {
        "before": dict(before),
        "after": dict(after),
        "max_speed": float(max_speed),
        "max_rpm": float(max_rpm),
        "distance_delta": distance_delta,
        "vehicle_moved": vehicle_moved,
        "engine_response": engine_response,
        "speed_high_samples": int(speed_high_samples),
    }
    if before_lap is not None:
        metrics["before_lap_distance"] = float(before_lap)
    if after_lap is not None:
        metrics["after_lap_distance"] = float(after_lap)

    if vehicle_moved:
        return "moved", metrics
    if engine_response:
        return "engine_response_only", metrics
    return "no_response", metrics


def read_vjoy_sent_values(env: Any) -> dict[str, float]:
    ctrl = env.client.controls.local_controls
    return {
        "steer_vjoy": float(ctrl.steer),
        "acc_pedal": float(ctrl.acc),
        "brake_pedal": float(ctrl.brake),
        "steer_cmd": float(ctrl.steer_cmd),
        "acc_cmd": float(ctrl.pedal_cmd),
        "brake_cmd": float(ctrl.brake_cmd),
    }


def observation_history_status(env: Any) -> dict[str, int | bool]:
    history_len = len(getattr(env, "states", None) or [])
    required = int(PAST_ACTIONS_WINDOW)
    missing = max(0, required - history_len)
    return {
        "observations_received": history_len,
        "observations_required": required,
        "observations_missing": missing,
        "history_ready": history_len >= required,
    }


def motion_telemetry_ready(runtime: Any, env: Any) -> tuple[bool, str]:
    if runtime.env is None:
        return False, "env not ready"
    age_ms = runtime.telemetry_age_ms()
    if age_ms > TELEMETRY_MAX_AGE_MS:
        return False, "telemetry stale ({0:.0f}ms > {1:.0f}ms)".format(
            age_ms, TELEMETRY_MAX_AGE_MS
        )
    try:
        sample = read_motion_sample(env)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        return False, "motion telemetry unreadable: {0}".format(exc)
    for field in ("speed", "rpm", "gear"):
        if field not in sample:
            return False, "motion telemetry missing {0}".format(field)
    return True, ""


def infrastructure_ready(runtime: Any, cfg: Any) -> tuple[bool, str]:
    """Smoke-test gate: mgmt + vJoy + Drive actif.

    La 1ʳᵉ n'est pas exigée ici : les workers d'entraînement skippent déjà
    l'engagement initial (AUTO_SHIFTER + gaz). Le test guidé engage la 1ʳᵉ
    explicitement via prepare_smoke_first_gear avant le gaz à 20 %.
    """
    from ac_workers.collector_live_attach import initial_startup_validated
    from ac_workers.post_reset_drive import driving_confirmed
    from ac_workers.worker_handshake import tcp_open, vjoy_accessible
    from AssettoCorsaEnv.gear_shift import gear_raw

    if runtime.env is None:
        return False, "env not ready"
    if initial_startup_validated(runtime, max_telemetry_age_ms=TELEMETRY_MAX_AGE_MS):
        return True, ""
    if not tcp_open("127.0.0.1", cfg.simulation_management_server_port):
        return False, "management not connected"
    if not vjoy_accessible(cfg.vjoy_device_id):
        return False, "vjoy not accessible"
    try:
        drive = runtime.env.client.simulation_management.get_driving_state()
        gear = int(gear_raw(runtime.env.client.state))
        # Drive menu / physics only — gear is prepared later for this diagnostic.
        if not driving_confirmed(drive, gear_raw=gear, require_non_neutral_gear=False):
            from ac_workers.drive_truth import is_drive_really_active

            active, truth_reason = is_drive_really_active(
                drive, vjoy_readback_ok=True, telemetry_age_sec=0.0
            )
            if not active:
                return False, truth_reason or "driving not confirmed"
            return False, drive.get("reason") or "driving not confirmed"
    except OSError as exc:
        return False, "driving state unavailable: {0}".format(exc)
    return True, ""


GEAR_PREP_THROTTLE_SAMPLES = 30
GEAR_PREP_THROTTLE_GAP_SEC = 0.04


def ensure_smoke_vjoy_path(env: Any) -> dict[str, Any]:
    """Force the same feeder path as training (DriverControls gate + Controls feeder)."""
    from ac_workers.vjoy_session import enable_env_local_vjoy

    enable_env_local_vjoy(env)
    driver = getattr(getattr(env, "client", None), "controls", None)
    lc = getattr(driver, "local_controls", None) if driver is not None else None
    feeder_enabled = bool(
        driver is not None and getattr(driver, "local_vjoy_feeder_enabled", lambda: False)()
    )
    controls_feeder = bool(lc is not None and getattr(lc, "feeder_enabled", lambda: False)())
    return {
        "driver_feeder_enabled": feeder_enabled,
        "controls_feeder_enabled": controls_feeder,
        "local_controls_id": id(lc) if lc is not None else None,
    }


def apply_smoke_physical_controls(
    env: Any,
    *,
    steer: float = 0.0,
    throttle: float = THROTTLE_PEDAL_FRACTION,
    brake: float = 0.0,
    device_id: int | None = None,
) -> dict[str, Any]:
    """Write pedals to vJoy — multiple fallback paths to guarantee delivery.

    Path 1: apply_canonical_controls (same as training policy).
    Path 2: direct local_controls.set_controls + update (raw vJoy write).
    Path 3: env.set_actions (original path, works if feeder is enabled).

    The previous env.set_actions-only path left local_controls at neutral when
    ``_local_vjoy_enabled`` was false, producing commands_sent=True with
    vjoy acc_pedal=0 and result no_response.
    """
    from ac_workers.canonical_controls import (
        apply_canonical_controls,
        get_local_controls,
        physical_to_internal,
    )

    vjoy_info = ensure_smoke_vjoy_path(env)
    lc = get_local_controls(env)
    resolved = device_id
    if resolved is None:
        mapping = getattr(env, "_gear_shift_mapping", None) or {}
        resolved = mapping.get("vjoy_device_id")
    steer_i, acc_i, brake_i = physical_to_internal(steer, throttle, brake)

    result: dict[str, Any] = {
        "ok": False,
        "error": "",
        "vjoy_info": vjoy_info,
        "requested_internal": [float(steer_i), float(acc_i), float(brake_i)],
        "requested_physical": [float(steer), float(throttle), float(brake)],
        "path_used": "none",
        "canonical_controls_called": False,
        "update_vjd_result": False,
    }

    # --- Path 1: canonical (same as training) ---
    try:
        canon = apply_canonical_controls(
            lc,
            float(steer),
            float(throttle),
            float(brake),
            device_id=int(resolved) if resolved is not None else None,
        )
        result.update(canon)
        if canon.get("ok") and canon.get("update_vjd_result"):
            result["path_used"] = "canonical"
    except Exception as exc:
        result["error"] = "canonical: {0}".format(exc)

    # --- Path 2: direct local_controls write (bypasses DriverControls entirely) ---
    if result["path_used"] == "none" and lc is not None:
        try:
            lc.set_controls(steer=steer_i, acc=acc_i, brake=brake_i)
            if hasattr(lc, "update"):
                lc.update()
            result["path_used"] = "direct_local_controls"
            result["ok"] = True
        except Exception as exc:
            result["error"] += " | direct: {0}".format(exc)

    # --- Path 3: env.set_actions + apply (original path, last resort) ---
    if result["path_used"] == "none":
        try:
            action = np.array([steer_i, acc_i, brake_i], dtype=np.float64)
            env.set_actions(action)
            env.client.controls.apply_local_controls()
            result["path_used"] = "env_set_actions"
            result["ok"] = True
        except Exception as exc:
            result["error"] += " | set_actions: {0}".format(exc)

    # Keep env action buffers coherent for reporting / history.
    try:
        action = np.array([steer_i, acc_i, brake_i], dtype=np.float64)
        env.raw_actions = action.copy()
        env.current_actions = action.copy()
        env.actions = action.copy()
        if hasattr(env, "executed_policy_action"):
            env.executed_policy_action = action.copy()
        driver = getattr(env.client, "controls", None)
        if driver is not None:
            driver["steer"] = float(steer_i)
            driver["acc"] = float(acc_i)
            driver["brake"] = float(brake_i)
    except Exception:
        pass
    result["vjoy_sent"] = read_vjoy_sent_values(env) if lc is not None else {}
    return result


def prepare_smoke_first_gear(
    env: Any,
    *,
    sleep_fn: Callable[[float], None],
    device_id: int | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Engage 1ʳᵉ for the guided smoke test only (not used by training workers).

    Manual gearbox: pulse shift-up. AUTO_SHIFTER: brief light throttle until
    gear leaves N, then neutralize before the main 20 % motion window.
    """
    from AssettoCorsaEnv.gear_shift import (
        auto_shift_neutral_to_first,
        gear_raw,
        is_first,
        is_neutral,
    )
    from ac_workers.canonical_controls import neutralize_canonical, get_local_controls

    ensure_smoke_vjoy_path(env)
    gear_before = int(gear_raw(env.client.state))
    report: dict[str, Any] = {
        "gear_before": gear_before,
        "engaged": False,
        "method": None,
        "vjoy_path": ensure_smoke_vjoy_path(env),
    }
    if not is_neutral(gear_before):
        report["engaged"] = True
        report["method"] = "already_engaged"
        report["gear_after"] = gear_before
        return True, report

    ok, shift_report = auto_shift_neutral_to_first(env)
    report["shift"] = shift_report
    gear_mid = int(gear_raw(env.client.state))
    report["gear_after_shift"] = gear_mid
    if is_first(gear_mid) or not is_neutral(gear_mid):
        report["engaged"] = True
        report["method"] = "shift_pulse"
        report["gear_after"] = gear_mid
        return True, report

    # AUTO_SHIFTER (or failed pulse): light gas until first engages.
    last_write: dict[str, Any] = {}
    for _ in range(GEAR_PREP_THROTTLE_SAMPLES):
        last_write = apply_smoke_physical_controls(
            env, throttle=THROTTLE_PEDAL_FRACTION, device_id=device_id
        )
        try:
            env.step(action=None)
        except Exception:
            pass
        gear_now = int(gear_raw(env.client.state))
        if not is_neutral(gear_now):
            report["engaged"] = True
            report["method"] = "throttle_assist"
            report["gear_after"] = gear_now
            report["last_write"] = last_write
            neutralize_canonical(get_local_controls(env), device_id=device_id)
            return True, report
        sleep_fn(GEAR_PREP_THROTTLE_GAP_SEC)

    neutralize_canonical(get_local_controls(env), device_id=device_id)
    gear_after = int(gear_raw(env.client.state))
    report["gear_after"] = gear_after
    report["engaged"] = not is_neutral(gear_after)
    report["method"] = "throttle_assist_incomplete"
    report["last_write"] = last_write
    # Still allow the main 20 % window: auto_shifter may engage during it.
    return True, report


def telemetry_ready(runtime: Any, cfg: Any, env: Any | None = None) -> tuple[bool, str]:
    """Smoke-test gate: fresh motion telemetry + mgmt/vJoy/driving (not collector observation_complete)."""
    env = env or runtime.env
    ok, reason = infrastructure_ready(runtime, cfg)
    if not ok:
        return False, reason
    return motion_telemetry_ready(runtime, env)


def _sync_runtime_from_step(runtime: Any, env: Any, obs: Any, info: Any) -> None:
    runtime.last_telemetry_ts = time.time()
    runtime.last_observation = obs.ravel().tolist()
    speed = 0.0
    if isinstance(info, dict):
        speed = float(info.get("speed", 0.0))
    runtime.telemetry_snapshot = {
        "steer": float(env.client.state.get("steer", 0.0)),
        "gas": float(env.client.state.get("gas", 0.0)),
        "brake": float(env.client.state.get("brake", 0.0)),
        "speed": speed,
    }
    runtime._telemetry_ready = True
    runtime.touch_heartbeat()


def wait_for_smoke_observation(
    *,
    runtime: Any,
    env: Any,
    cfg: Any,
    report: dict[str, Any],
    publish_status: Callable[[], None],
    monotonic: Callable[[], float],
    sleep_fn: Callable[[float], None],
) -> tuple[bool, str]:
    """Collect neutral observations until motion telemetry is fresh (max MAX_OBS_WAIT_SEC)."""
    deadline = monotonic() + MAX_OBS_WAIT_SEC
    neutral = neutral_action(env)
    history = observation_history_status(env)
    report.update(history)
    report["phase"] = "control_smoke_waiting_observation"
    report["observation_wait_reason"] = ""
    runtime.set_phase("control_smoke_waiting_observation")
    runtime.control_smoke_test_report = dict(report)
    publish_status()

    last_reason = "motion telemetry not fresh after {0:.1f}s neutral wait".format(MAX_OBS_WAIT_SEC)
    iterations = 0

    while monotonic() < deadline and iterations < MAX_LOOP_ITERATIONS:
        iterations += 1
        ok, reason = infrastructure_ready(runtime, cfg)
        if not ok:
            report.update(observation_history_status(env))
            report["observation_wait_reason"] = reason
            runtime.control_smoke_test_report = dict(report)
            publish_status()
            return False, reason

        runtime.last_requested_action = [float(x) for x in neutral.ravel()[:3]]
        env.set_actions(neutral)
        env.client.controls.apply_local_controls()
        obs, _, _, info = env.step(neutral)
        _sync_runtime_from_step(runtime, env, obs, info)

        history = observation_history_status(env)
        report.update(history)
        ok, reason = motion_telemetry_ready(runtime, env)
        if ok:
            if history["history_ready"]:
                report["observation_wait_reason"] = ""
                report["motion_telemetry_ready"] = True
                runtime.control_smoke_test_report = dict(report)
                publish_status()
                return True, ""
            if monotonic() >= deadline:
                report["observation_wait_reason"] = ""
                report["motion_telemetry_ready"] = True
                report["history_optional_at_timeout"] = True
                runtime.control_smoke_test_report = dict(report)
                publish_status()
                return True, ""
            last_reason = "collecting neutral observations ({0}/{1})".format(
                history["observations_received"],
                history["observations_required"],
            )
        else:
            last_reason = reason or last_reason
        report["observation_wait_reason"] = last_reason
        runtime.control_smoke_test_report = dict(report)
        publish_status()
        sleep_fn(0.0)

    report.update(observation_history_status(env))
    ok, reason = motion_telemetry_ready(runtime, env)
    if ok:
        report["observation_wait_reason"] = ""
        report["motion_telemetry_ready"] = True
        if not report.get("history_ready"):
            report["history_optional_at_timeout"] = True
        runtime.control_smoke_test_report = dict(report)
        publish_status()
        return True, ""

    report["observation_wait_reason"] = last_reason
    report["motion_telemetry_ready"] = False
    runtime.control_smoke_test_report = dict(report)
    publish_status()
    return False, last_reason


def _neutralize(env: Any, neutral_vjoy_fn: Callable[[], None]) -> None:
    neutral_vjoy_fn()
    if env is not None:
        try:
            env.client.controls.set_defaults()
            env.client.controls.apply_local_controls()
        except Exception:
            pass


def _classify_result(
    before: dict[str, float | int | None],
    after: dict[str, float | int | None],
    *,
    max_speed: float,
    max_rpm: float,
    min_lap_distance: float | None,
    max_lap_distance: float | None,
    speed_high_samples: int,
) -> tuple[str, dict[str, Any]]:
    return evaluate_motion_outcome(
        before,
        after,
        max_speed=max_speed,
        max_rpm=max_rpm,
        min_lap_distance=min_lap_distance,
        max_lap_distance=max_lap_distance,
        speed_high_samples=speed_high_samples,
    )


def run_control_smoke_test(
    *,
    worker_id: int,
    runtime: Any,
    env: Any,
    cfg: Any,
    neutral_vjoy_fn: Callable[[], None],
    publish_status: Callable[[], None],
    force: bool = False,
    monotonic: Callable[[], float] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Run bounded vJoy smoke test; never starts a new episode or uses policy."""
    if worker_id != WORKER_ID_ONLY or (not force and not is_enabled(worker_id)):
        return {}

    monotonic = monotonic or time.monotonic
    sleep_fn = sleep_fn or time.sleep

    report: dict[str, Any] = {
        "enabled": True,
        "conversion": action_conversion_info(),
        "requested_action_internal": [float(x) for x in build_requested_internal_action()],
        "commands_sent": False,
        "result": "failed",
        "error": "",
        "observations_received": 0,
        "observations_required": int(PAST_ACTIONS_WINDOW),
        "observations_missing": int(PAST_ACTIONS_WINDOW),
        "history_ready": False,
        "motion_telemetry_ready": False,
        "observation_wait_reason": "",
    }

    ok, reason = infrastructure_ready(runtime, cfg)
    if not ok:
        report["error"] = reason or "infrastructure not ready"
        report["result"] = "failed"
        logger.warning("control smoke test skipped worker=%s: %s", worker_id, report["error"])
        runtime.control_smoke_test_report = report
        runtime.set_phase("running")
        return report

    ok, reason = wait_for_smoke_observation(
        runtime=runtime,
        env=env,
        cfg=cfg,
        report=report,
        publish_status=publish_status,
        monotonic=monotonic,
        sleep_fn=sleep_fn,
    )
    if not ok:
        report["error"] = reason or "observation wait failed"
        report["result"] = "failed"
        logger.warning("control smoke test skipped worker=%s: %s", worker_id, report["error"])
        runtime.control_smoke_test_report = report
        runtime.set_phase("running")
        return report

    ok, reason = telemetry_ready(runtime, cfg, env=env)
    if not ok:
        report["error"] = reason or "telemetry not ready"
        report["result"] = "failed"
        logger.warning("control smoke test skipped worker=%s: %s", worker_id, report["error"])
        runtime.control_smoke_test_report = report
        runtime.set_phase("running")
        return report

    sleep_fn(POST_DRIVE_SETTLE_SEC)
    report["vjoy_path"] = ensure_smoke_vjoy_path(env)
    device_id = int(getattr(cfg, "vjoy_device_id", 1) or 1)
    gear_ok, gear_prep = prepare_smoke_first_gear(
        env, sleep_fn=sleep_fn, device_id=device_id
    )
    report["gear_prep"] = gear_prep
    runtime.control_smoke_test_report = dict(report)
    publish_status()
    if not gear_ok:
        report["error"] = "first gear preparation failed"
        report["result"] = "failed"
        runtime.control_smoke_test_report = report
        runtime.set_phase("running")
        return report

    before = collect_stable_baseline(env, sleep_fn=sleep_fn)
    report["before"] = dict(before)
    action = build_requested_internal_action()
    last_vjoy: dict[str, float] = {}
    last_executed: list[float] = []
    last_write: dict[str, Any] = {}
    commands_sent = False
    max_speed = float(before.get("speed", 0.0))
    max_rpm = float(before.get("rpm", 0.0))
    min_lap_distance = before.get("lap_distance")
    max_lap_distance = before.get("lap_distance")
    speed_high_samples = 0
    report["samples"] = []

    runtime.set_phase("control_smoke_test")
    report["phase"] = "control_smoke_test"
    runtime.control_smoke_test_report = dict(report)
    publish_status()

    try:
        deadline = monotonic() + MAX_DURATION_SEC
        iterations = 0
        while monotonic() < deadline and iterations < MAX_LOOP_ITERATIONS:
            iterations += 1
            last_write = apply_smoke_physical_controls(
                env,
                throttle=THROTTLE_PEDAL_FRACTION,
                device_id=device_id,
            )
            runtime.last_requested_action = list(
                last_write.get("requested_internal") or [float(x) for x in action.ravel()[:3]]
            )
            last_executed = list(runtime.last_requested_action)
            last_vjoy = dict(last_write.get("vjoy_sent") or read_vjoy_sent_values(env))
            if float(last_vjoy.get("acc_pedal", 0.0)) >= (THROTTLE_PEDAL_FRACTION * 0.5):
                commands_sent = True
            elif bool(last_write.get("canonical_controls_called")):
                # Write attempted; still count as sent for diagnostics even if
                # pedal readback looks wrong (separates path failure from motion).
                commands_sent = True
            obs, _, _, info = env.step(action=None)
            _sync_runtime_from_step(runtime, env, obs, info)
            runtime.last_executed_action = list(last_executed)
            sample = read_motion_sample(env)
            report["samples"].append(
                {
                    "time": monotonic(),
                    "received": dict(sample),
                    "requested_internal": list(runtime.last_requested_action),
                    "vjoy_sent": dict(last_vjoy),
                    "canonical_ok": bool(last_write.get("ok")),
                    "update_vjd_result": bool(last_write.get("update_vjd_result")),
                }
            )
            spd = float(sample.get("speed", 0.0))
            rpm = float(sample.get("rpm", 0.0))
            max_speed = max(max_speed, spd)
            max_rpm = max(max_rpm, rpm)
            if spd > MIN_SPEED_KMH:
                speed_high_samples += 1
            lap = sample.get("lap_distance")
            if lap is not None:
                lap_f = float(lap)
                min_lap_distance = lap_f if min_lap_distance is None else min(float(min_lap_distance), lap_f)
                max_lap_distance = lap_f if max_lap_distance is None else max(float(max_lap_distance), lap_f)
            if monotonic() >= deadline:
                break
            sleep_fn(0.0)

        after = read_motion_sample(env)
        report["after"] = dict(after)
        try:
            import math
            report["displacement_m"] = math.hypot(
                float(after["world_position_x"]) - float(before["world_position_x"]),
                float(after["world_position_y"]) - float(before["world_position_y"]))
        except (KeyError, TypeError, ValueError):
            report["displacement_m"] = None
        report["executed_action_internal"] = last_executed
        report["vjoy_sent"] = last_vjoy
        report["last_canonical_write"] = last_write
        report["requested_action_internal"] = runtime.last_requested_action
        report["commands_sent"] = commands_sent
        pedal_ok = float(last_vjoy.get("acc_pedal", 0.0)) >= (THROTTLE_PEDAL_FRACTION * 0.5)
        if commands_sent and not pedal_ok:
            report["result"] = "failed"
            report["error"] = (
                "vJoy throttle not applied (acc_pedal={0}, expected ~{1}). "
                "Feeder/path issue — not just gear in N."
            ).format(last_vjoy.get("acc_pedal"), THROTTLE_PEDAL_FRACTION)
        elif commands_sent:
            result, motion_metrics = _classify_result(
                before,
                after,
                max_speed=max_speed,
                max_rpm=max_rpm,
                min_lap_distance=min_lap_distance,
                max_lap_distance=max_lap_distance,
                speed_high_samples=speed_high_samples,
            )
            report["result"] = result
            report.update(motion_metrics)
        else:
            report["result"] = "failed"
            report["error"] = "no commands applied before deadline"
    except Exception as exc:
        report["result"] = "failed"
        report["error"] = str(exc) or "control smoke test exception"
        logger.exception("control smoke test failed worker=%s", worker_id)
    finally:
        _neutralize(env, neutral_vjoy_fn)
        report["commands_sent"] = commands_sent
        if report["result"] == "failed" and not report.get("error"):
            report["error"] = "control smoke test failed without explicit cause"
        runtime.control_smoke_test_report = dict(report)
        runtime.control_smoke_test_result = report.get("result", "failed")
        runtime.set_phase("running")
        try:
            publish_status()
        except OSError as exc:
            logger.warning("control smoke test status write failed worker=%s: %s", worker_id, exc)

    logger.info(
        "control smoke test worker=%s result=%s before=%s after=%s vjoy=%s",
        worker_id,
        report.get("result"),
        report.get("before"),
        report.get("after"),
        report.get("vjoy_sent"),
    )
    return report
