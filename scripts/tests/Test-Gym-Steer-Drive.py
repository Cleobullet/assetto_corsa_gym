#!/usr/bin/env python3
"""Test securise direction + conduite lente via AssettoCorsaEnv (Phase C2C)."""

from __future__ import annotations

import csv
import json
import logging
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from omegaconf import OmegaConf

REPO_ROOT = Path(__file__).resolve().parents[2]
AI_ACS = REPO_ROOT / "AssettoCorsa-AI" / "assettocorsa" / "acs.exe"
GYM_ROOT = REPO_ROOT / "assetto_corsa_gym"
PLUGIN_DIR = GYM_ROOT / "AssettoCorsaPlugin" / "plugins" / "sensors_par"
WORK_DIR = REPO_ROOT / "outputs" / "steer_drive_test"
DEFAULT_TRACK = "monza"
DEFAULT_CAR = "lotus_exos_125"
TELEM_HOST = "127.0.0.1"
TELEM_PORT = 2345

SPEED_PRE_MAX_KMH = 0.5
SPEED_INITIAL_TARGET_KMH = 1.0
SPEED_REGULATOR_LOW_KMH = 0.8
SPEED_REGULATOR_COAST_MAX_KMH = 1.3
SPEED_REGULATOR_BRAKE_MAX_KMH = 1.8
SPEED_EMERGENCY_KMH = 2.5
SPEED_STOPPED_KMH = 0.5
ACC_PEDAL_MAX = 0.08
ACC_PEDAL_REG = 0.03
BRAKE_PEDAL_MAX = 0.35
BRAKE_PEDAL_REG_MIN = 0.03
BRAKE_PEDAL_REG_MAX = 0.08
BRAKE_PEDAL_REG_HIGH = 0.10
STEER_TARGET_LEFT_DEG = -90.0
STEER_TARGET_RIGHT_DEG = 90.0
STEER_HARD_CAP_DEG = 100.0
STEER_DETECT_LEFT_DEG = -75.0
STEER_DETECT_RIGHT_DEG = 75.0
STEER_CENTER_TOL_DEG = 3.0
STEER_HOLD_SEC = 2.0
STEER_CENTER_HOLD_SEC = 0.3
ACC_PHASE_MAX_SEC = 2.0
STEER_PHASE_MAX_SEC = 20.0
BRAKE_PHASE_MAX_SEC = 3.0
EMERGENCY_WAIT_SEC = 2.0
EMERGENCY_STOP_SAMPLES = 3
MIN_COMMAND_INTERVAL_SEC = 0.10
ACC_STATUS_ZERO = 0.01
ACC_DETECT_MIN = 0.03
BRAKE_DETECT_MIN = 0.015
NO_MOVE_CHECK_SEC = 1.0
NO_MOVE_SPEED_KMH = 0.3
TELEM_PROBE_SEC = 10.0
RELEASE_MAX_SEC = 3.0
GEAR_NEUTRAL = 1
GEAR_FIRST = 2
GEAR_SHIFT_WAIT_SEC = 1.0

sys.path.insert(0, str(GYM_ROOT))
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(REPO_ROOT))

from ac_workers.vjoy_session import ensure_vjoy_feeder

import AssettoCorsaEnv.assettoCorsa as assettoCorsa  # noqa: E402
import AssettoCorsaEnv.ac_env as ac_env  # noqa: E402

logger = logging.getLogger("Test-Gym-Steer-Drive")


def pedal_to_internal(p: float) -> float:
    return 2.0 * p - 1.0


def internal_to_pedal(x: float) -> float:
    return (x + 1.0) / 2.0


def load_steer_internal_per_deg(car: str) -> float:
    path = GYM_ROOT / "AssettoCorsaConfigs" / "cars" / car / "steer_map.csv"
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            action = float(row["action_steerAngle"])
            angle = float(row["steerAngle"])
            if angle > 0.0:
                return action / angle
    raise RuntimeError(f"steer_map.csv invalide: {path}")


STEER_INTERNAL_PER_DEG = load_steer_internal_per_deg(DEFAULT_CAR)
STEER_LEFT_INTERNAL = STEER_TARGET_LEFT_DEG * STEER_INTERNAL_PER_DEG
STEER_RIGHT_INTERNAL = STEER_TARGET_RIGHT_DEG * STEER_INTERNAL_PER_DEG

ACC_INTERNAL_OFF = pedal_to_internal(0.0)
ACC_INTERNAL_MAX = pedal_to_internal(ACC_PEDAL_MAX)
ACC_INTERNAL_REG = pedal_to_internal(ACC_PEDAL_REG)
BRAKE_INTERNAL_OFF = pedal_to_internal(0.0)
BRAKE_INTERNAL_REG_MIN = pedal_to_internal(BRAKE_PEDAL_REG_MIN)
BRAKE_INTERNAL_REG_MAX = pedal_to_internal(BRAKE_PEDAL_REG_MAX)
BRAKE_INTERNAL_REG_HIGH = pedal_to_internal(BRAKE_PEDAL_REG_HIGH)
BRAKE_INTERNAL_MAX = pedal_to_internal(BRAKE_PEDAL_MAX)
BRAKE_INTERNAL_FULL = pedal_to_internal(1.0)

PHASE_ORDER = (
    "baseline",
    "shift",
    "accel",
    "steer_left",
    "center_left",
    "steer_right",
    "center_right",
    "release",
    "brake",
)


@dataclass
class Sample:
    speed_kmh: float
    acc: float
    brake: float
    steer_deg: float
    gear: str | int | float
    acc_internal: float
    acc_pedal: float
    brake_internal: float
    brake_pedal: float
    steer_internal: float


@dataclass
class Metrics:
    max_speed_kmh: float = 0.0
    final_speed_kmh: float = 0.0
    max_acc: float = 0.0
    max_brake: float = 0.0
    max_acc_pedal: float = 0.0
    min_steer_deg: float = 0.0
    max_steer_deg: float = 0.0
    max_steer_action: float = 0.0
    hold_time_left_sec: float = 0.0
    hold_time_right_sec: float = 0.0
    recover_car_calls: int = 0
    recover_used: bool = False
    emergency_active: bool = False
    safety_limit_exceeded: bool = False
    acc_detected: bool = False
    speed_increased: bool = False
    brake_detected: bool = False
    stopped_after_brake: bool = False
    acc_cut_before_brake: bool = False
    no_movement: bool = False
    no_acc_response: bool = False
    gear_shift_failed: bool = False
    shift_up_pulses: int = 0
    gear_before_shift: int | None = None
    gear_after_shift: int | None = None
    steer_left_detected: bool = False
    steer_center_after_left: bool = False
    steer_right_detected: bool = False
    steer_center_after_right: bool = False
    phase_left_executed: bool = False
    phase_center_left_executed: bool = False
    phase_right_executed: bool = False
    phase_center_right_executed: bool = False
    phase_order: list[str] = field(default_factory=list)
    baseline_speed_kmh: float = 0.0
    baseline_steer_deg: float = 0.0
    recover_car_reason: str = ""


@dataclass
class CommandContext:
    last_command_time: float = 0.0
    last_state_fp: tuple | None = None
    steer_target_deg: float | None = None


def normalize_path(path: str) -> str:
    return str(Path(path).resolve()).lower()


def speed_kmh(state: dict) -> float:
    return float(state.get("speed", 0.0)) * 3.6


def steer_angle_deg(state: dict) -> float:
    return float(state.get("steerAngle", 0.0))


def internal_acc(env: ac_env.AssettoCorsaEnv) -> float:
    return float(env.current_actions[1])


def internal_brake(env: ac_env.AssettoCorsaEnv) -> float:
    return float(env.current_actions[2])


def internal_steer(env: ac_env.AssettoCorsaEnv) -> float:
    return float(env.current_actions[0])


def state_fingerprint(state: dict) -> tuple:
    return (state.get("packetId"), state.get("currentTime"), state.get("steps"))


def gear_label(state: dict) -> str | int | float:
    for key in ("actualGear", "gear"):
        if key in state:
            return state[key]
    return "?"


def gear_value(state: dict) -> int | None:
    raw = gear_label(state)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def sample_from_state(state: dict, env: ac_env.AssettoCorsaEnv) -> Sample:
    acc_i = internal_acc(env)
    brake_i = internal_brake(env)
    return Sample(
        speed_kmh=speed_kmh(state),
        acc=float(state.get("accStatus", 0.0)),
        brake=float(state.get("brakeStatus", 0.0)),
        steer_deg=steer_angle_deg(state),
        gear=gear_label(state),
        acc_internal=acc_i,
        acc_pedal=internal_to_pedal(acc_i),
        brake_internal=brake_i,
        brake_pedal=internal_to_pedal(brake_i),
        steer_internal=internal_steer(env),
    )


def speed_regulator_targets(spd: float) -> tuple[float, float]:
    if spd < SPEED_REGULATOR_LOW_KMH:
        return ACC_INTERNAL_REG, BRAKE_INTERNAL_OFF
    elif spd <= SPEED_REGULATOR_COAST_MAX_KMH:
        return ACC_INTERNAL_OFF, BRAKE_INTERNAL_OFF
    elif spd <= SPEED_REGULATOR_BRAKE_MAX_KMH:
        span = SPEED_REGULATOR_BRAKE_MAX_KMH - SPEED_REGULATOR_COAST_MAX_KMH
        ratio = (spd - SPEED_REGULATOR_COAST_MAX_KMH) / span
        brake_pedal = BRAKE_PEDAL_REG_MIN + ratio * (BRAKE_PEDAL_REG_MAX - BRAKE_PEDAL_REG_MIN)
        return ACC_INTERNAL_OFF, pedal_to_internal(brake_pedal)
    else:
        return ACC_INTERNAL_OFF, BRAKE_INTERNAL_REG_HIGH


def log_sample(phase: str, s: Sample, step_dt: float, ctx: CommandContext) -> None:
    target = ctx.steer_target_deg
    logger.info(
        "[phase=%s] dt=%.3fs speed=%.2f km/h steer=%.2f deg cible_steer=%s "
        "acc_cmd=%.1f%% brake_cmd=%.1f%% steer_int=%.4f acc=%.3f brake=%.3f gear=%s",
        phase,
        step_dt,
        s.speed_kmh,
        s.steer_deg,
        f"{target:.1f}" if target is not None else "0.0",
        s.acc_pedal * 100.0,
        s.brake_pedal * 100.0,
        s.steer_internal,
        s.acc,
        s.brake,
        s.gear,
    )


def apply_neutral_pedals(env: ac_env.AssettoCorsaEnv) -> None:
    env.client.controls.set_controls(
        steer=0.0,
        acc=ACC_INTERNAL_OFF,
        brake=BRAKE_INTERNAL_OFF,
        enable_gear_shift=False,
        shift_up=False,
        shift_down=False,
    )
    env.client.respond_to_server()


def relative_delta_to_target(
    env: ac_env.AssettoCorsaEnv,
    axis: int,
    target_internal: float,
) -> float:
    current = float(env.current_actions[axis])
    rate = float(env.adjusted_controls_rate_limit[axis, 1])
    if rate == 0.0:
        return 0.0
    needed = target_internal - current
    return float(np.clip(needed / rate, -1.0, 1.0))


def drive_action(
    steer_delta: float = 0.0,
    acc_delta: float = 0.0,
    brake_delta: float = 0.0,
) -> np.ndarray:
    return np.array([steer_delta, acc_delta, brake_delta], dtype=np.float32)


def verify_ai_acs_running() -> None:
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='acs.exe'\" -ErrorAction SilentlyContinue"
        " | Select-Object -ExpandProperty ExecutablePath"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        text=True,
        check=False,
    )
    paths = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not paths:
        raise RuntimeError("acs.exe introuvable - lancez Hotlap depuis Content Manager AI.")
    if not any(normalize_path(p) == normalize_path(str(AI_ACS)) for p in paths):
        raise RuntimeError(f"acs.exe actif ne provient pas de la copie AI: {paths}")
    logger.info("OK acs.exe AI detecte")


def probe_telemetry_port() -> dict:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(1.0)
    deadline = time.time() + TELEM_PROBE_SEC
    last_payload: dict | None = None
    try:
        while time.time() < deadline:
            try:
                sock.sendto(b"connect", (TELEM_HOST, TELEM_PORT))
            except OSError as exc:
                raise RuntimeError(f"impossible d'envoyer connect vers {TELEM_HOST}:{TELEM_PORT}: {exc}") from exc
            try:
                data, _ = sock.recvfrom(65536)
            except socket.timeout:
                continue
            msg = data.decode("utf-8", errors="replace").strip()
            if msg == "identified":
                continue
            if msg and msg.startswith("{"):
                last_payload = json.loads(msg)
                break
        if last_payload is None:
            raise RuntimeError(
                f"telemetrie indisponible sur {TELEM_HOST}:{TELEM_PORT} (sensors_par actif ?)"
            )
        return last_payload
    finally:
        try:
            sock.sendto(b"disconnect", (TELEM_HOST, TELEM_PORT))
        except OSError:
            pass
        sock.close()


def check_preconditions(state: dict) -> None:
    spd = speed_kmh(state)
    if spd > SPEED_PRE_MAX_KMH:
        raise RuntimeError(f"vitesse initiale {spd:.2f} km/h > {SPEED_PRE_MAX_KMH} km/h")
    steer = abs(steer_angle_deg(state))
    if steer > STEER_CENTER_TOL_DEG:
        raise RuntimeError(f"direction hors centre: steerAngle={steer:.1f} deg")


def make_env(cfg) -> ac_env.AssettoCorsaEnv:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    module_cfg = assettoCorsa.ModuleConfig(cfg.AssettoCorsa)
    return ac_env.AssettoCorsaEnv(
        module_cfg.get_config(),
        output_path=str(WORK_DIR),
        ac_configs_path=module_cfg.ac_configs_path,
        max_episode_steps=module_cfg.max_episode_steps,
        save_observations=False,
    )


def track_steer_action(metrics: Metrics, action: np.ndarray) -> None:
    metrics.max_steer_action = max(metrics.max_steer_action, abs(float(action[0])))


def update_metrics(metrics: Metrics, state: dict, env: ac_env.AssettoCorsaEnv) -> Sample:
    s = sample_from_state(state, env)
    metrics.max_speed_kmh = max(metrics.max_speed_kmh, s.speed_kmh)
    metrics.final_speed_kmh = s.speed_kmh
    metrics.max_acc = max(metrics.max_acc, s.acc)
    metrics.max_brake = max(metrics.max_brake, s.brake)
    metrics.max_acc_pedal = max(metrics.max_acc_pedal, s.acc_pedal)
    metrics.min_steer_deg = min(metrics.min_steer_deg, s.steer_deg)
    metrics.max_steer_deg = max(metrics.max_steer_deg, s.steer_deg)
    if s.acc >= ACC_DETECT_MIN:
        metrics.acc_detected = True
    if s.brake >= BRAKE_DETECT_MIN:
        metrics.brake_detected = True
    if s.steer_deg <= STEER_DETECT_LEFT_DEG:
        metrics.steer_left_detected = True
    if s.steer_deg >= STEER_DETECT_RIGHT_DEG:
        metrics.steer_right_detected = True
    if abs(s.steer_deg) > STEER_HARD_CAP_DEG:
        metrics.safety_limit_exceeded = True
    return s


def recover_car_once(env: ac_env.AssettoCorsaEnv, metrics: Metrics, reason: str) -> None:
    if metrics.recover_used:
        logger.warning("recover_car deja utilise, ignore: %s", reason)
        return
    logger.warning("recover_car: %s", reason)
    metrics.recover_used = True
    metrics.recover_car_calls = 1
    metrics.recover_car_reason = reason
    env.recover_car()


def check_safety_limits(
    env: ac_env.AssettoCorsaEnv,
    metrics: Metrics,
    state: dict,
    label: str,
) -> None:
    if metrics.emergency_active:
        return
    spd = speed_kmh(state)
    if spd > SPEED_EMERGENCY_KMH:
        emergency_halt(
            env,
            metrics,
            f"vitesse {spd:.2f} km/h > {SPEED_EMERGENCY_KMH} km/h [{label}]",
        )
    steer = abs(steer_angle_deg(state))
    if steer > STEER_HARD_CAP_DEG:
        metrics.safety_limit_exceeded = True
        emergency_halt(
            env,
            metrics,
            f"steerAngle {steer:.2f} deg > {STEER_HARD_CAP_DEG} deg [{label}]",
        )


def step_drive(
    env: ac_env.AssettoCorsaEnv,
    metrics: Metrics,
    ctx: CommandContext,
    action: np.ndarray,
    phase: str,
    *,
    skip_safety_check: bool = False,
) -> tuple[dict, float]:
    track_steer_action(metrics, action)
    t0 = time.perf_counter()
    try:
        _obs, _rew, _done, _info = env.step(action)
    except (ConnectionResetError, OSError, socket.timeout) as exc:
        if not metrics.emergency_active:
            emergency_halt(env, metrics, f"telemetrie perdue [{phase}]: {exc}")
        else:
            recover_car_once(env, metrics, f"telemetrie perdue en urgence [{phase}]: {exc}")
            raise RuntimeError(str(exc)) from exc
    step_dt = time.perf_counter() - t0
    state = env.states[-1]
    if not skip_safety_check:
        check_safety_limits(env, metrics, state, phase)
    log_sample(phase, update_metrics(metrics, state, env), step_dt, ctx)
    return state, step_dt


def observe_step(
    env: ac_env.AssettoCorsaEnv,
    metrics: Metrics,
    ctx: CommandContext,
    phase: str,
    *,
    skip_safety_check: bool = False,
) -> tuple[dict, float]:
    return step_drive(
        env, metrics, ctx, drive_action(0.0, 0.0, 0.0), phase, skip_safety_check=skip_safety_check,
    )


def command_interval_elapsed(ctx: CommandContext) -> bool:
    return time.time() - ctx.last_command_time >= MIN_COMMAND_INTERVAL_SEC


def mark_command(ctx: CommandContext, state: dict) -> None:
    ctx.last_command_time = time.time()
    ctx.last_state_fp = state_fingerprint(state)


def telemetry_is_fresh(ctx: CommandContext, state: dict) -> bool:
    if ctx.last_state_fp is None:
        return True
    return state_fingerprint(state) != ctx.last_state_fp


def controlled_step(
    env: ac_env.AssettoCorsaEnv,
    metrics: Metrics,
    ctx: CommandContext,
    phase: str,
    *,
    steer_target: float | None = None,
    acc_target: float | None = None,
    brake_target: float | None = None,
    require_fresh: bool = False,
    skip_safety_check: bool = False,
) -> tuple[dict, float]:
    state = env.states[-1]

    if not command_interval_elapsed(ctx):
        return observe_step(env, metrics, ctx, phase, skip_safety_check=skip_safety_check)

    if require_fresh and not telemetry_is_fresh(ctx, state):
        return observe_step(env, metrics, ctx, phase, skip_safety_check=skip_safety_check)

    steer_delta = 0.0
    acc_delta = 0.0
    brake_delta = 0.0
    if steer_target is not None:
        steer_delta = relative_delta_to_target(env, 0, steer_target)
    if acc_target is not None:
        acc_delta = relative_delta_to_target(env, 1, acc_target)
    if brake_target is not None:
        brake_delta = relative_delta_to_target(env, 2, brake_target)

    if steer_delta == 0.0 and acc_delta == 0.0 and brake_delta == 0.0:
        return observe_step(env, metrics, ctx, phase, skip_safety_check=skip_safety_check)

    state, step_dt = step_drive(
        env,
        metrics,
        ctx,
        drive_action(steer_delta, acc_delta, brake_delta),
        phase,
        skip_safety_check=skip_safety_check,
    )
    mark_command(ctx, state)
    return state, step_dt


def regulated_step(
    env: ac_env.AssettoCorsaEnv,
    metrics: Metrics,
    ctx: CommandContext,
    phase: str,
    *,
    steer_target: float | None = None,
    require_fresh: bool = False,
) -> tuple[dict, float]:
    state = env.states[-1]
    spd = speed_kmh(state)
    acc_target, brake_target = speed_regulator_targets(spd)
    steer_cmd = steer_target
    if steer_target is not None and abs(internal_steer(env) - steer_target) < 1e-4:
        steer_cmd = None
    return controlled_step(
        env,
        metrics,
        ctx,
        phase,
        steer_target=steer_cmd,
        acc_target=acc_target,
        brake_target=brake_target,
        require_fresh=require_fresh and steer_cmd is not None,
    )


def wait_until_stopped_emergency(
    env: ac_env.AssettoCorsaEnv,
    metrics: Metrics,
    ctx: CommandContext,
) -> bool:
    consecutive = 0
    deadline = time.time() + EMERGENCY_WAIT_SEC
    while time.time() < deadline:
        try:
            observe_step(env, metrics, ctx, "urgence", skip_safety_check=True)
        except (ConnectionResetError, OSError, socket.timeout) as exc:
            recover_car_once(env, metrics, f"telemetrie perdue en urgence: {exc}")
            raise RuntimeError(str(exc)) from exc
        spd = speed_kmh(env.states[-1])
        if spd <= SPEED_STOPPED_KMH:
            consecutive += 1
            if consecutive >= EMERGENCY_STOP_SAMPLES:
                logger.info("Urgence: arret confirme (%d mesures <= %.1f km/h)", consecutive, SPEED_STOPPED_KMH)
                return True
        else:
            consecutive = 0
    return False


def emergency_halt(env: ac_env.AssettoCorsaEnv, metrics: Metrics, reason: str) -> None:
    if metrics.emergency_active:
        logger.warning("Urgence deja active, ignore: %s", reason)
        return

    metrics.emergency_active = True
    logger.error("ARRET SECURITE: %s", reason)
    ctx = CommandContext()

    try:
        env.client.controls.set_controls(steer=0.0, acc=ACC_INTERNAL_OFF, brake=BRAKE_INTERNAL_FULL)
        env.client.respond_to_server()
        env.current_actions[0] = 0.0
        env.current_actions[1] = ACC_INTERNAL_OFF
        env.current_actions[2] = BRAKE_INTERNAL_FULL
    except Exception as exc:
        recover_car_once(env, metrics, f"controles perdus apres urgence: {exc}")
        raise RuntimeError(reason) from exc

    stopped = wait_until_stopped_emergency(env, metrics, ctx)
    if not stopped:
        last_speed = speed_kmh(env.states[-1]) if env.states else 999.0
        recover_car_once(
            env,
            metrics,
            f"voiture non arretee ({last_speed:.2f} km/h > {SPEED_STOPPED_KMH} km/h) apres urgence",
        )

    raise RuntimeError(reason)


def run_shift_to_first_phase(
    env: ac_env.AssettoCorsaEnv,
    metrics: Metrics,
    ctx: CommandContext,
) -> bool:
    apply_neutral_pedals(env)
    env.current_actions[0] = 0.0
    env.current_actions[1] = ACC_INTERNAL_OFF
    env.current_actions[2] = BRAKE_INTERNAL_OFF

    state = env.states[-1]
    gear_before = gear_value(state)
    metrics.gear_before_shift = gear_before
    logger.info("gear avant impulsion: %s", gear_before)

    if gear_before == GEAR_FIRST:
        logger.info("Deja en premiere (gear=%d), pas d'impulsion shift_up.", GEAR_FIRST)
        metrics.gear_after_shift = GEAR_FIRST
        logger.info("nombre total impulsions shift_up: %d (max 1)", metrics.shift_up_pulses)
        return True

    if gear_before != GEAR_NEUTRAL:
        metrics.gear_shift_failed = True
        logger.error("gear=%s inattendu pour passage N->1ere.", gear_before)
        return False

    logger.info(
        "Impulsion shift_up envoyee (enable_gear_shift=True, shift_up=True, shift_down=False)"
    )
    env.client.controls.set_controls(
        steer=0.0,
        acc=ACC_INTERNAL_OFF,
        brake=BRAKE_INTERNAL_OFF,
        enable_gear_shift=True,
        shift_up=True,
        shift_down=False,
    )
    env.client.respond_to_server()
    metrics.shift_up_pulses = 1
    time.sleep(MIN_COMMAND_INTERVAL_SEC)

    env.client.controls.set_controls(
        steer=0.0,
        acc=ACC_INTERNAL_OFF,
        brake=BRAKE_INTERNAL_OFF,
        enable_gear_shift=False,
        shift_up=False,
        shift_down=False,
    )
    env.client.respond_to_server()
    logger.info("shift_up relache (shift_up=False, enable_gear_shift=False)")

    deadline = time.time() + GEAR_SHIFT_WAIT_SEC
    sample_count = 0
    gear_after = gear_before
    while time.time() < deadline:
        state, _dt = observe_step(env, metrics, ctx, "shift", skip_safety_check=True)
        sample_count += 1
        gear_after = gear_value(state)
        ctx.last_state_fp = state_fingerprint(state)
        if gear_after == GEAR_FIRST:
            break

    metrics.gear_after_shift = gear_after
    logger.info("gear apres impulsion: %s (%d echantillons frais)", gear_after, sample_count)
    logger.info("nombre total impulsions shift_up: %d (max 1)", metrics.shift_up_pulses)

    if gear_before == GEAR_NEUTRAL and gear_after == GEAR_FIRST:
        return True

    metrics.gear_shift_failed = True
    logger.error('FAIL "passage N vers premiere impossible"')
    return False


def gear_is_first(env: ac_env.AssettoCorsaEnv, metrics: Metrics) -> bool:
    if metrics.gear_after_shift == GEAR_FIRST:
        return True
    if env.states:
        return gear_value(env.states[-1]) == GEAR_FIRST
    return False


def gear_passed_neutral_to_first(metrics: Metrics) -> bool:
    return (
        metrics.gear_before_shift == GEAR_NEUTRAL
        and metrics.gear_after_shift == GEAR_FIRST
        and not metrics.gear_shift_failed
    )


def run_accel_phase(env: ac_env.AssettoCorsaEnv, metrics: Metrics, ctx: CommandContext) -> bool:
    logger.info(
        "Phase accel initiale (max %.1f s, cible %.1f km/h, pedal max %.0f%%)...",
        ACC_PHASE_MAX_SEC,
        SPEED_INITIAL_TARGET_KMH,
        ACC_PEDAL_MAX * 100.0,
    )
    metrics.phase_order.append("accel")
    ctx.steer_target_deg = 0.0
    t0 = time.time()
    peak_before_move = metrics.baseline_speed_kmh

    while time.time() - t0 < ACC_PHASE_MAX_SEC:
        state = env.states[-1]
        if speed_kmh(state) >= SPEED_INITIAL_TARGET_KMH:
            logger.info("Cible initiale %.1f km/h atteinte.", SPEED_INITIAL_TARGET_KMH)
            return True

        acc_internal = internal_acc(env)
        acc_target = None if acc_internal >= ACC_INTERNAL_MAX else ACC_INTERNAL_MAX
        state, _dt = controlled_step(
            env,
            metrics,
            ctx,
            "accel",
            steer_target=0.0,
            acc_target=acc_target,
            brake_target=BRAKE_INTERNAL_OFF,
            require_fresh=acc_target is not None,
        )

        if speed_kmh(state) >= SPEED_INITIAL_TARGET_KMH:
            return True

        elapsed = time.time() - t0
        if elapsed >= NO_MOVE_CHECK_SEC and metrics.max_speed_kmh <= peak_before_move + NO_MOVE_SPEED_KMH:
            if metrics.max_acc < ACC_DETECT_MIN:
                metrics.no_acc_response = True
            else:
                metrics.no_movement = True
            return False

    if metrics.max_acc < ACC_DETECT_MIN:
        metrics.no_acc_response = True
    return metrics.max_speed_kmh >= SPEED_INITIAL_TARGET_KMH


def hold_with_regulator(
    env: ac_env.AssettoCorsaEnv,
    metrics: Metrics,
    ctx: CommandContext,
    phase: str,
    condition_fn,
    hold_sec: float,
    deadline: float,
    *,
    steer_target: float | None = None,
    steer_target_deg: float | None = None,
) -> tuple[bool, float]:
    hold_start: float | None = None
    held_sec = 0.0
    ctx.steer_target_deg = steer_target_deg

    while time.time() < deadline:
        state = env.states[-1]
        if condition_fn(state):
            if hold_start is None:
                hold_start = time.time()
            held_sec = time.time() - hold_start
            if held_sec >= hold_sec:
                return True, held_sec
        else:
            hold_start = None
            held_sec = 0.0

        regulated_step(
            env,
            metrics,
            ctx,
            phase,
            steer_target=steer_target,
            require_fresh=steer_target is not None,
        )

    return False, held_sec


def run_steer_hold_phase(
    env: ac_env.AssettoCorsaEnv,
    metrics: Metrics,
    ctx: CommandContext,
    *,
    phase: str,
    steer_target_internal: float,
    steer_target_deg: float,
) -> bool:
    logger.info(
        "Phase %s (cible %.1f deg, interne %.4f, maintien %.1f s apres cible)...",
        phase,
        steer_target_deg,
        steer_target_internal,
        STEER_HOLD_SEC,
    )
    metrics.phase_order.append(phase)
    ctx.steer_target_deg = steer_target_deg
    deadline = time.time() + STEER_PHASE_MAX_SEC

    while time.time() < deadline:
        state = env.states[-1]
        sa = steer_angle_deg(state)
        at_target = sa <= steer_target_deg if steer_target_deg < 0 else sa >= steer_target_deg
        if at_target and abs(internal_steer(env) - steer_target_internal) < 1e-3:
            break
        regulated_step(
            env,
            metrics,
            ctx,
            phase,
            steer_target=steer_target_internal,
            require_fresh=True,
        )

    hold_deadline = time.time() + STEER_PHASE_MAX_SEC
    if steer_target_deg < 0:
        ok, held = hold_with_regulator(
            env,
            metrics,
            ctx,
            phase,
            lambda s: steer_angle_deg(s) <= STEER_DETECT_LEFT_DEG,
            STEER_HOLD_SEC,
            hold_deadline,
            steer_target=steer_target_internal,
            steer_target_deg=steer_target_deg,
        )
        metrics.hold_time_left_sec = held
    else:
        ok, held = hold_with_regulator(
            env,
            metrics,
            ctx,
            phase,
            lambda s: steer_angle_deg(s) >= STEER_DETECT_RIGHT_DEG,
            STEER_HOLD_SEC,
            hold_deadline,
            steer_target=steer_target_internal,
            steer_target_deg=steer_target_deg,
        )
        metrics.hold_time_right_sec = held
    return ok


def run_steer_center_phase(
    env: ac_env.AssettoCorsaEnv,
    metrics: Metrics,
    ctx: CommandContext,
    *,
    phase: str,
    center_flag: str,
) -> bool:
    logger.info(
        "Phase %s (centre tol %.1f deg, maintien %.1f s)...",
        phase,
        STEER_CENTER_TOL_DEG,
        STEER_CENTER_HOLD_SEC,
    )
    metrics.phase_order.append(phase)
    ctx.steer_target_deg = 0.0
    deadline = time.time() + STEER_PHASE_MAX_SEC
    ok, _held = hold_with_regulator(
        env,
        metrics,
        ctx,
        phase,
        lambda s: abs(steer_angle_deg(s)) <= STEER_CENTER_TOL_DEG,
        STEER_CENTER_HOLD_SEC,
        deadline,
        steer_target=0.0,
        steer_target_deg=0.0,
    )
    if center_flag == "after_left":
        metrics.steer_center_after_left = ok
        metrics.phase_center_left_executed = ok
    elif center_flag == "after_right":
        metrics.steer_center_after_right = ok
        metrics.phase_center_right_executed = ok
    return ok


def run_release_phase(env: ac_env.AssettoCorsaEnv, metrics: Metrics, ctx: CommandContext) -> bool:
    logger.info("Coupe accelerateur avant freinage (accStatus <= %.2f)...", ACC_STATUS_ZERO)
    metrics.phase_order.append("release")
    ctx.steer_target_deg = 0.0
    deadline = time.time() + RELEASE_MAX_SEC
    while time.time() < deadline:
        state = env.states[-1]
        acc_status = float(state.get("accStatus", 0.0))
        if internal_acc(env) <= ACC_INTERNAL_OFF + 1e-6 and acc_status <= ACC_STATUS_ZERO:
            metrics.acc_cut_before_brake = True
            logger.info("Accelerateur coupe: accStatus=%.3f", acc_status)
            return True

        controlled_step(
            env,
            metrics,
            ctx,
            "release",
            steer_target=0.0,
            acc_target=ACC_INTERNAL_OFF,
            brake_target=BRAKE_INTERNAL_OFF,
            require_fresh=False,
        )

    acc_status = float(env.states[-1].get("accStatus", 0.0)) if env.states else 1.0
    if acc_status > ACC_STATUS_ZERO:
        logger.error("Accelerateur non coupe: accStatus=%.3f", acc_status)
        return False
    metrics.acc_cut_before_brake = True
    return True


def run_brake_phase(env: ac_env.AssettoCorsaEnv, metrics: Metrics, ctx: CommandContext) -> bool:
    logger.info("Phase frein finale (max %.1f s, pedal max %.0f%%)...", BRAKE_PHASE_MAX_SEC, BRAKE_PEDAL_MAX * 100.0)
    metrics.phase_order.append("brake")
    ctx.steer_target_deg = 0.0
    t0 = time.time()

    while time.time() - t0 < BRAKE_PHASE_MAX_SEC:
        state = env.states[-1]
        acc_status = float(state.get("accStatus", 0.0))
        if acc_status > ACC_STATUS_ZERO:
            controlled_step(
                env,
                metrics,
                ctx,
                "brake",
                steer_target=0.0,
                acc_target=ACC_INTERNAL_OFF,
                brake_target=BRAKE_INTERNAL_OFF,
                require_fresh=False,
            )
            continue

        brake_internal = internal_brake(env)
        brake_target = BRAKE_INTERNAL_MAX if brake_internal < BRAKE_INTERNAL_MAX else None
        state, _dt = controlled_step(
            env,
            metrics,
            ctx,
            "brake",
            steer_target=0.0,
            acc_target=ACC_INTERNAL_OFF,
            brake_target=brake_target,
            require_fresh=brake_target is not None,
        )

        if speed_kmh(state) <= SPEED_STOPPED_KMH:
            metrics.stopped_after_brake = True
            metrics.final_speed_kmh = speed_kmh(state)
            return True

    if env.states and speed_kmh(env.states[-1]) <= SPEED_STOPPED_KMH:
        metrics.stopped_after_brake = True
        metrics.final_speed_kmh = speed_kmh(env.states[-1])
        return True
    return metrics.stopped_after_brake


def evaluate_success(metrics: Metrics) -> bool:
    if (
        metrics.gear_shift_failed
        or metrics.no_movement
        or metrics.no_acc_response
        or metrics.safety_limit_exceeded
        or metrics.emergency_active
    ):
        return False

    metrics.speed_increased = metrics.max_speed_kmh >= metrics.baseline_speed_kmh + 0.3

    return (
        gear_passed_neutral_to_first(metrics)
        and metrics.speed_increased
        and metrics.acc_cut_before_brake
        and metrics.steer_left_detected
        and metrics.steer_center_after_left
        and metrics.steer_right_detected
        and metrics.steer_center_after_right
        and metrics.brake_detected
        and metrics.final_speed_kmh < SPEED_STOPPED_KMH
        and not metrics.emergency_active
    )


def print_summary(metrics: Metrics, passed: bool) -> None:
    status = "PASS" if passed else "FAIL"
    print(f"\n=== {status} Test-Gym-Steer-Drive ===")
    print(f"  phases executees    : {', '.join(metrics.phase_order)}")
    print(f"  ordre attendu       : {', '.join(PHASE_ORDER)}")
    print(f"  angle gauche min    : {metrics.min_steer_deg:.2f} deg")
    print(f"  angle droit max     : {metrics.max_steer_deg:.2f} deg")
    print(f"  maintien gauche     : {metrics.hold_time_left_sec:.2f} s")
    print(f"  maintien droite     : {metrics.hold_time_right_sec:.2f} s")
    print(f"  vitesse max         : {metrics.max_speed_kmh:.2f} km/h")
    print(f"  vitesse finale      : {metrics.final_speed_kmh:.2f} km/h")
    print(f"  cible steer         : {STEER_TARGET_LEFT_DEG:.0f} / {STEER_TARGET_RIGHT_DEG:.0f} deg")
    print(f"  recover_car calls   : {metrics.recover_car_calls}")
    print(f"  gear 1->2           : {gear_passed_neutral_to_first(metrics)}")
    print(f"  vitesse monte       : {metrics.speed_increased}")
    print(f"  acc coupe avant frein: {metrics.acc_cut_before_brake}")
    print(f"  steer gauche        : {metrics.steer_left_detected} (<= {STEER_DETECT_LEFT_DEG:.0f} deg)")
    print(f"  centre apres gauche : {metrics.steer_center_after_left}")
    print(f"  steer droite        : {metrics.steer_right_detected} (>= {STEER_DETECT_RIGHT_DEG:.0f} deg)")
    print(f"  centre apres droite : {metrics.steer_center_after_right}")
    print(f"  frein detecte       : {metrics.brake_detected}")
    print(f"  urgence             : {metrics.emergency_active}")


def stop_and_neutralize(
    env: ac_env.AssettoCorsaEnv,
    metrics: Metrics,
    ctx: CommandContext,
    *,
    allow_recover: bool,
) -> None:
    try:
        env.client.controls.set_controls(steer=0.0, acc=ACC_INTERNAL_OFF, brake=BRAKE_INTERNAL_FULL)
        env.client.respond_to_server()
        env.current_actions[0] = 0.0
        env.current_actions[1] = ACC_INTERNAL_OFF
        env.current_actions[2] = BRAKE_INTERNAL_FULL
    except Exception as exc:
        if allow_recover:
            recover_car_once(env, metrics, f"controles inaccessibles au nettoyage: {exc}")
        return

    consecutive = 0
    deadline = time.time() + EMERGENCY_WAIT_SEC
    while time.time() < deadline:
        spd = speed_kmh(env.states[-1]) if env.states else 999.0
        if spd <= SPEED_STOPPED_KMH:
            consecutive += 1
            if consecutive >= EMERGENCY_STOP_SAMPLES:
                break
        else:
            consecutive = 0
        try:
            observe_step(env, metrics, ctx, "cleanup", skip_safety_check=True)
        except Exception:
            break

    if allow_recover and env.states and speed_kmh(env.states[-1]) > SPEED_STOPPED_KMH:
        recover_car_once(
            env,
            metrics,
            f"voiture non arretee ({speed_kmh(env.states[-1]):.2f} km/h) dans finally",
        )

    try:
        env.client.controls.set_defaults()
        env.client.controls.apply_local_controls()
    except Exception as exc:
        logger.warning("Neutre partiel: %s", exc)


def final_cleanup(
    env: ac_env.AssettoCorsaEnv | None,
    metrics: Metrics,
    ctx: CommandContext,
    *,
    test_passed: bool,
) -> None:
    if env is None:
        return
    try:
        stop_and_neutralize(env, metrics, ctx, allow_recover=not test_passed)
    except Exception as exc:
        logger.warning("Nettoyage partiel: %s", exc)
    try:
        env.end()
    except Exception as exc:
        logger.warning("Fermeture env: %s", exc)


def run_steer_scenario(env: ac_env.AssettoCorsaEnv, metrics: Metrics, ctx: CommandContext) -> bool:
    if not run_accel_phase(env, metrics, ctx):
        return False

    left_ok = run_steer_hold_phase(
        env,
        metrics,
        ctx,
        phase="steer_left",
        steer_target_internal=STEER_LEFT_INTERNAL,
        steer_target_deg=STEER_TARGET_LEFT_DEG,
    )
    metrics.phase_left_executed = left_ok
    if not left_ok:
        return False

    if not run_steer_center_phase(env, metrics, ctx, phase="center_left", center_flag="after_left"):
        return False

    right_ok = run_steer_hold_phase(
        env,
        metrics,
        ctx,
        phase="steer_right",
        steer_target_internal=STEER_RIGHT_INTERNAL,
        steer_target_deg=STEER_TARGET_RIGHT_DEG,
    )
    metrics.phase_right_executed = right_ok
    if not right_ok:
        return False

    if not run_steer_center_phase(env, metrics, ctx, phase="center_right", center_flag="after_right"):
        return False

    if not run_release_phase(env, metrics, ctx):
        return False

    return run_brake_phase(env, metrics, ctx)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    env: ac_env.AssettoCorsaEnv | None = None
    metrics = Metrics()
    ctx = CommandContext()
    exit_code = 1
    test_passed = False

    try:
        verify_ai_acs_running()
        probe = probe_telemetry_port()
        logger.info(
            "Telemetrie OK port %s (speed=%.2f km/h steer=%.1f)",
            TELEM_PORT,
            float(probe.get("speed", 0.0)) * 3.6,
            float(probe.get("steerAngle", 0.0)),
        )

        cfg = OmegaConf.load(str(REPO_ROOT / "legacy" / "configs" / "config.yml"))
        cfg.AssettoCorsa.screen_capture_enable = False
        cfg.AssettoCorsa.send_reset_at_start = True
        cfg.AssettoCorsa.recover_car_on_done = False
        cfg.AssettoCorsa.track = DEFAULT_TRACK
        cfg.AssettoCorsa.car = DEFAULT_CAR

        logger.info(
            "Env: track=%s car=%s steer interne/deg=%.6f cibles %.0f/%.0f deg "
            "(interne %.4f/%.4f) urgence >%.1f km/h",
            DEFAULT_TRACK,
            DEFAULT_CAR,
            STEER_INTERNAL_PER_DEG,
            STEER_TARGET_LEFT_DEG,
            STEER_TARGET_RIGHT_DEG,
            STEER_LEFT_INTERNAL,
            STEER_RIGHT_INTERNAL,
            SPEED_EMERGENCY_KMH,
        )
        env = make_env(cfg)
        # Standalone tests must activate the feeder normally enabled by the collector.
        ok, error, _ = ensure_vjoy_feeder(
            env.client.controls.local_controls,
            int(os.environ.get("ACGYM_VJOY_DEVICE", "1")),
            allow_global_reset=False,
        )
        if not ok:
            raise RuntimeError("Controleur vJoy indisponible: " + error)
        env.client.controls.enable_local_vjoy_feeder()
        logger.info("Controleur vJoy acquis, commandes locales activees")

        input("\nEntree pour demarrer le test direction + conduite lente...")

        env.reset()
        state = env.states[-1]
        check_preconditions(state)
        metrics.baseline_speed_kmh = speed_kmh(state)
        metrics.baseline_steer_deg = steer_angle_deg(state)
        metrics.min_steer_deg = metrics.baseline_steer_deg
        metrics.max_steer_deg = metrics.baseline_steer_deg
        ctx.last_state_fp = state_fingerprint(state)
        metrics.phase_order.append("baseline")
        log_sample("baseline", sample_from_state(state, env), 0.0, ctx)

        scenario_ok = False
        if run_shift_to_first_phase(env, metrics, ctx) and gear_is_first(env, metrics):
            metrics.phase_order.append("shift")
            scenario_ok = run_steer_scenario(env, metrics, ctx)
        elif not metrics.gear_shift_failed:
            metrics.gear_shift_failed = True
            logger.error('FAIL "passage N vers premiere impossible" (gear != 2)')

        test_passed = scenario_ok and evaluate_success(metrics)
        print_summary(metrics, test_passed)
        exit_code = 0 if test_passed else 2

    except KeyboardInterrupt:
        logger.error("Ctrl+C - nettoyage securite.")
        exit_code = 130
    except RuntimeError as exc:
        logger.error("ECHEC: %s", exc)
        print_summary(metrics, False)
        exit_code = 1
    finally:
        final_cleanup(env, metrics, ctx, test_passed=test_passed)
        logger.info("Client libere, neutre restaure.")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
