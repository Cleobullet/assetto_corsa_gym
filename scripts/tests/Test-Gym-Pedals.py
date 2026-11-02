#!/usr/bin/env python3
"""Test securise accelerateur + frein via AssettoCorsaEnv (Phase C2B)."""

from __future__ import annotations

import json
import logging
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
WORK_DIR = REPO_ROOT / "outputs" / "pedal_test"
DEFAULT_TRACK = "monza"
DEFAULT_CAR = "lotus_exos_125"
TELEM_HOST = "127.0.0.1"
TELEM_PORT = 2345

SPEED_PRE_MAX_KMH = 0.5
SPEED_TARGET_KMH = 1.5
SPEED_EMERGENCY_KMH = 4.0
SPEED_STOPPED_KMH = 0.5
ACC_PEDAL_MAX = 0.08
BRAKE_PEDAL_MAX = 0.35
STEER_CENTER_MAX_DEG = 5.0
ACC_PHASE_MAX_SEC = 2.0
BRAKE_PHASE_MAX_SEC = 3.0
EMERGENCY_WAIT_SEC = 2.0
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

import AssettoCorsaEnv.assettoCorsa as assettoCorsa  # noqa: E402
import AssettoCorsaEnv.ac_env as ac_env  # noqa: E402

logger = logging.getLogger("Test-Gym-Pedals")


def pedal_to_internal(p: float) -> float:
    return 2.0 * p - 1.0


def internal_to_pedal(x: float) -> float:
    return (x + 1.0) / 2.0


ACC_INTERNAL_OFF = pedal_to_internal(0.0)
ACC_INTERNAL_MAX = pedal_to_internal(ACC_PEDAL_MAX)
BRAKE_INTERNAL_OFF = pedal_to_internal(0.0)
BRAKE_INTERNAL_MAX = pedal_to_internal(BRAKE_PEDAL_MAX)
BRAKE_INTERNAL_FULL = pedal_to_internal(1.0)


@dataclass
class Sample:
    speed_kmh: float
    acc: float
    brake: float
    gear: str | int | float
    acc_internal: float
    acc_pedal: float
    brake_internal: float
    brake_pedal: float


@dataclass
class Metrics:
    max_speed_kmh: float = 0.0
    max_acc: float = 0.0
    max_brake: float = 0.0
    max_acc_pedal: float = 0.0
    max_steer_action: float = 0.0
    recover_car_calls: int = 0
    recover_used: bool = False
    emergency_active: bool = False
    acc_samples: list[Sample] = field(default_factory=list)
    brake_samples: list[Sample] = field(default_factory=list)
    acc_detected: bool = False
    speed_increased: bool = False
    brake_detected: bool = False
    stopped_after_brake: bool = False
    no_steer_commands: bool = True
    no_movement: bool = False
    no_acc_response: bool = False
    gear_shift_failed: bool = False
    shift_up_pulses: int = 0
    gear_before_shift: int | None = None
    gear_after_shift: int | None = None
    baseline_speed_kmh: float = 0.0
    recover_car_reason: str = ""


@dataclass
class CommandContext:
    last_command_time: float = 0.0
    last_state_fp: tuple | None = None


def normalize_path(path: str) -> str:
    return str(Path(path).resolve()).lower()


def speed_kmh(state: dict) -> float:
    return float(state.get("speed", 0.0)) * 3.6


def internal_acc(env: ac_env.AssettoCorsaEnv) -> float:
    return float(env.current_actions[1])


def internal_brake(env: ac_env.AssettoCorsaEnv) -> float:
    return float(env.current_actions[2])


def state_fingerprint(state: dict) -> tuple:
    return (state.get("packetId"), state.get("currentTime"), state.get("steps"))


def gear_label(state: dict) -> str | int | float:
    for key in ("actualGear", "gear"):
        if key in state:
            return state[key]
    return "?"


def sample_from_state(state: dict, env: ac_env.AssettoCorsaEnv) -> Sample:
    acc_i = internal_acc(env)
    brake_i = internal_brake(env)
    return Sample(
        speed_kmh=speed_kmh(state),
        acc=float(state.get("accStatus", 0.0)),
        brake=float(state.get("brakeStatus", 0.0)),
        gear=gear_label(state),
        acc_internal=acc_i,
        acc_pedal=internal_to_pedal(acc_i),
        brake_internal=brake_i,
        brake_pedal=internal_to_pedal(brake_i),
    )


def log_sample(phase: str, s: Sample, step_dt: float) -> None:
    logger.info(
        "[%s] dt=%.3fs speed=%.2f km/h acc=%.3f brake=%.3f "
        "acc_int=%.3f acc_ped=%.1f%% brake_int=%.3f brake_ped=%.1f%% gear=%s",
        phase,
        step_dt,
        s.speed_kmh,
        s.acc,
        s.brake,
        s.acc_internal,
        s.acc_pedal * 100.0,
        s.brake_internal,
        s.brake_pedal * 100.0,
        s.gear,
    )


def gear_value(state: dict) -> int | None:
    raw = gear_label(state)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


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


def run_shift_to_first_phase(
    env: ac_env.AssettoCorsaEnv,
    metrics: Metrics,
    ctx: CommandContext,
) -> bool:
    apply_neutral_pedals(env)
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
        logger.info("nombre total impulsions shift_up: %d (max 1)", metrics.shift_up_pulses)
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
        state, _dt = observe_step(env, metrics, "shift", skip_speed_check=True)
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


def pedal_action(acc_delta: float = 0.0, brake_delta: float = 0.0) -> np.ndarray:
    return np.array([0.0, acc_delta, brake_delta], dtype=np.float32)


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
    steer = abs(float(state.get("steerAngle", 0.0)))
    if steer > STEER_CENTER_MAX_DEG:
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
    if abs(float(action[0])) > 1e-9:
        metrics.no_steer_commands = False


def update_metrics(metrics: Metrics, state: dict, env: ac_env.AssettoCorsaEnv, phase: str) -> Sample:
    s = sample_from_state(state, env)
    metrics.max_speed_kmh = max(metrics.max_speed_kmh, s.speed_kmh)
    metrics.max_acc = max(metrics.max_acc, s.acc)
    metrics.max_brake = max(metrics.max_brake, s.brake)
    metrics.max_acc_pedal = max(metrics.max_acc_pedal, s.acc_pedal)
    if s.acc >= ACC_DETECT_MIN:
        metrics.acc_detected = True
    if s.brake >= BRAKE_DETECT_MIN:
        metrics.brake_detected = True
    if phase == "acc":
        metrics.acc_samples.append(s)
    elif phase == "brake":
        metrics.brake_samples.append(s)
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


def check_speed_limits(env: ac_env.AssettoCorsaEnv, metrics: Metrics, state: dict, label: str) -> None:
    if metrics.emergency_active:
        return
    if speed_kmh(state) > SPEED_EMERGENCY_KMH:
        emergency_halt(env, metrics, f"vitesse {speed_kmh(state):.2f} km/h > {SPEED_EMERGENCY_KMH} km/h [{label}]")


def step_pedal(
    env: ac_env.AssettoCorsaEnv,
    metrics: Metrics,
    action: np.ndarray,
    phase: str,
    *,
    skip_speed_check: bool = False,
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
    if not skip_speed_check:
        check_speed_limits(env, metrics, state, phase)
    log_sample(phase, update_metrics(metrics, state, env, phase), step_dt)
    return state, step_dt


def observe_step(
    env: ac_env.AssettoCorsaEnv,
    metrics: Metrics,
    phase: str,
    *,
    skip_speed_check: bool = False,
) -> tuple[dict, float]:
    return step_pedal(env, metrics, pedal_action(0.0, 0.0), phase, skip_speed_check=skip_speed_check)


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
    acc_target: float | None,
    brake_target: float | None,
    *,
    require_fresh: bool = False,
    skip_speed_check: bool = False,
) -> tuple[dict, float]:
    state = env.states[-1]

    if not command_interval_elapsed(ctx):
        return observe_step(env, metrics, phase, skip_speed_check=skip_speed_check)

    if require_fresh and not telemetry_is_fresh(ctx, state):
        return observe_step(env, metrics, phase, skip_speed_check=skip_speed_check)

    acc_delta = 0.0
    brake_delta = 0.0
    if acc_target is not None:
        acc_delta = relative_delta_to_target(env, 1, acc_target)
    if brake_target is not None:
        brake_delta = relative_delta_to_target(env, 2, brake_target)

    if acc_delta == 0.0 and brake_delta == 0.0:
        return observe_step(env, metrics, phase, skip_speed_check=skip_speed_check)

    state, step_dt = step_pedal(
        env,
        metrics,
        pedal_action(acc_delta, brake_delta),
        phase,
        skip_speed_check=skip_speed_check,
    )
    mark_command(ctx, state)
    return state, step_dt


def emergency_halt(env: ac_env.AssettoCorsaEnv, metrics: Metrics, reason: str) -> None:
    if metrics.emergency_active:
        logger.warning("Urgence deja active, ignore: %s", reason)
        return

    metrics.emergency_active = True
    logger.error("ARRET SECURITE: %s", reason)

    try:
        env.client.controls.set_controls(steer=0.0, acc=ACC_INTERNAL_OFF, brake=BRAKE_INTERNAL_FULL)
        env.client.respond_to_server()
        env.current_actions[1] = ACC_INTERNAL_OFF
        env.current_actions[2] = BRAKE_INTERNAL_FULL
    except Exception as exc:
        recover_car_once(env, metrics, f"controles perdus apres urgence: {exc}")
        raise RuntimeError(reason) from exc

    deadline = time.time() + EMERGENCY_WAIT_SEC
    while time.time() < deadline:
        try:
            observe_step(env, metrics, "urgence", skip_speed_check=True)
        except (ConnectionResetError, OSError, socket.timeout) as exc:
            recover_car_once(env, metrics, f"telemetrie perdue en urgence: {exc}")
            raise RuntimeError(reason) from exc

    last_speed = speed_kmh(env.states[-1]) if env.states else 999.0
    if last_speed > SPEED_STOPPED_KMH:
        recover_car_once(
            env,
            metrics,
            f"voiture non arretee ({last_speed:.2f} km/h > {SPEED_STOPPED_KMH} km/h) apres urgence",
        )

    raise RuntimeError(reason)


def run_accel_phase(env: ac_env.AssettoCorsaEnv, metrics: Metrics, ctx: CommandContext) -> None:
    logger.info(
        "Phase accelerateur (max %.1f s, cible %.1f km/h, pedal max %.0f%%, interne max %.3f)...",
        ACC_PHASE_MAX_SEC,
        SPEED_TARGET_KMH,
        ACC_PEDAL_MAX * 100.0,
        ACC_INTERNAL_MAX,
    )
    t0 = time.time()
    peak_before_move = metrics.baseline_speed_kmh

    while time.time() - t0 < ACC_PHASE_MAX_SEC:
        state = env.states[-1]
        if speed_kmh(state) >= SPEED_TARGET_KMH:
            logger.info("Cible vitesse %.1f km/h atteinte.", SPEED_TARGET_KMH)
            return

        acc_internal = internal_acc(env)
        if acc_internal >= ACC_INTERNAL_MAX:
            acc_target = None
            require_fresh = False
        else:
            acc_target = ACC_INTERNAL_MAX
            require_fresh = True

        state, _dt = controlled_step(
            env,
            metrics,
            ctx,
            "acc",
            acc_target=acc_target,
            brake_target=BRAKE_INTERNAL_OFF,
            require_fresh=require_fresh,
        )

        if speed_kmh(state) >= SPEED_TARGET_KMH:
            logger.info("Cible vitesse %.1f km/h atteinte.", SPEED_TARGET_KMH)
            return

        elapsed = time.time() - t0
        if elapsed >= NO_MOVE_CHECK_SEC and metrics.max_speed_kmh <= peak_before_move + NO_MOVE_SPEED_KMH:
            if metrics.max_acc < ACC_DETECT_MIN:
                metrics.no_acc_response = True
                logger.warning(
                    "accStatus ne monte pas (max %.3f) - FAIL sans accelerer davantage (gear=%s).",
                    metrics.max_acc,
                    gear_label(state),
                )
            else:
                metrics.no_movement = True
                logger.warning(
                    "Voiture immobile apres %.1f s (gear=%s).",
                    elapsed,
                    gear_label(state),
                )
            return

    if metrics.max_acc < ACC_DETECT_MIN:
        metrics.no_acc_response = True
        logger.warning("accStatus ne monte pas apres %.1f s - FAIL.", ACC_PHASE_MAX_SEC)


def run_release_phase(env: ac_env.AssettoCorsaEnv, metrics: Metrics, ctx: CommandContext) -> None:
    logger.info("Coupe accelerateur (cible interne %.1f)...", ACC_INTERNAL_OFF)
    deadline = time.time() + RELEASE_MAX_SEC
    while time.time() < deadline:
        state = env.states[-1]
        acc_status = float(state.get("accStatus", 0.0))
        if internal_acc(env) <= ACC_INTERNAL_OFF + 1e-6 and acc_status <= ACC_STATUS_ZERO:
            logger.info(
                "Accelerateur coupe: acc_int=%.3f acc_ped=%.1f%% accStatus=%.3f",
                internal_acc(env),
                internal_to_pedal(internal_acc(env)) * 100.0,
                acc_status,
            )
            return

        state, _dt = controlled_step(
            env,
            metrics,
            ctx,
            "release",
            acc_target=ACC_INTERNAL_OFF,
            brake_target=BRAKE_INTERNAL_OFF,
            require_fresh=False,
        )

    acc_status = float(env.states[-1].get("accStatus", 0.0)) if env.states else 1.0
    if acc_status > ACC_STATUS_ZERO:
        raise RuntimeError(f"accelerateur reel reste au-dessus de {ACC_STATUS_ZERO}: accStatus={acc_status:.3f}")


def run_brake_phase(env: ac_env.AssettoCorsaEnv, metrics: Metrics, ctx: CommandContext) -> None:
    logger.info(
        "Phase frein (max %.1f s, pedal max %.0f%%, interne max %.3f)...",
        BRAKE_PHASE_MAX_SEC,
        BRAKE_PEDAL_MAX * 100.0,
        BRAKE_INTERNAL_MAX,
    )
    t0 = time.time()

    while time.time() - t0 < BRAKE_PHASE_MAX_SEC:
        state = env.states[-1]
        acc_status = float(state.get("accStatus", 0.0))
        if acc_status > ACC_STATUS_ZERO:
            state, _dt = controlled_step(
                env,
                metrics,
                ctx,
                "brake",
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
            acc_target=ACC_INTERNAL_OFF,
            brake_target=brake_target,
            require_fresh=brake_target is not None,
        )

        if speed_kmh(state) <= SPEED_STOPPED_KMH:
            logger.info("Arret valide sous %.1f km/h.", SPEED_STOPPED_KMH)
            metrics.stopped_after_brake = True
            return

    if env.states and speed_kmh(env.states[-1]) <= SPEED_STOPPED_KMH:
        metrics.stopped_after_brake = True


def evaluate_success(metrics: Metrics) -> bool:
    if metrics.gear_shift_failed or metrics.no_movement or metrics.no_acc_response:
        return False
    metrics.speed_increased = metrics.max_speed_kmh > metrics.baseline_speed_kmh + 0.3
    if not metrics.stopped_after_brake and metrics.brake_samples:
        metrics.stopped_after_brake = metrics.brake_samples[-1].speed_kmh <= SPEED_STOPPED_KMH
    return (
        metrics.acc_detected
        and metrics.speed_increased
        and metrics.brake_detected
        and metrics.stopped_after_brake
        and metrics.no_steer_commands
    )


def print_summary(metrics: Metrics, passed: bool) -> None:
    status = "PASS" if passed else "FAIL"
    print(f"\n=== {status} Test-Gym-Pedals ===")
    print(f"  max speed           : {metrics.max_speed_kmh:.2f} km/h")
    print(f"  max accStatus       : {metrics.max_acc:.3f}")
    print(f"  max brakeStatus     : {metrics.max_brake:.3f}")
    print(f"  max acc pedal cmd   : {metrics.max_acc_pedal * 100.0:.1f}%")
    print(f"  recover_car calls   : {metrics.recover_car_calls}")
    print(f"  acc detecte         : {metrics.acc_detected}")
    print(f"  vitesse monte       : {metrics.speed_increased}")
    print(f"  frein detecte       : {metrics.brake_detected}")
    print(f"  arret frein         : {metrics.stopped_after_brake}")
    print(f"  pas de steer        : {metrics.no_steer_commands} (max action steer={metrics.max_steer_action:.4f})")
    print(f"  shift_up pulses     : {metrics.shift_up_pulses} (max 1)")
    if metrics.gear_before_shift is not None:
        print(f"  gear avant shift    : {metrics.gear_before_shift}")
    if metrics.gear_after_shift is not None:
        print(f"  gear apres shift    : {metrics.gear_after_shift}")
    if metrics.gear_shift_failed:
        print('  FAIL                : passage N vers premiere impossible')
    if metrics.recover_car_reason:
        print(f"  recover_car reason  : {metrics.recover_car_reason}")
    if metrics.no_acc_response:
        print("  note                : accStatus n'a pas monte")
    if metrics.no_movement:
        print("  note                : voiture n'a pas bouge (rapport neutre ?)")


def stop_and_neutralize(env: ac_env.AssettoCorsaEnv, metrics: Metrics, ctx: CommandContext) -> None:
    try:
        env.client.controls.set_controls(steer=0.0, acc=ACC_INTERNAL_OFF, brake=BRAKE_INTERNAL_FULL)
        env.client.respond_to_server()
        env.current_actions[1] = ACC_INTERNAL_OFF
        env.current_actions[2] = BRAKE_INTERNAL_FULL
    except Exception as exc:
        recover_car_once(env, metrics, f"controles inaccessibles au nettoyage: {exc}")
        return

    deadline = time.time() + EMERGENCY_WAIT_SEC
    while time.time() < deadline:
        if env.states and speed_kmh(env.states[-1]) <= SPEED_STOPPED_KMH:
            break
        try:
            observe_step(env, metrics, "cleanup", skip_speed_check=True)
        except Exception:
            break

    if env.states and speed_kmh(env.states[-1]) > SPEED_STOPPED_KMH:
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


def final_cleanup(env: ac_env.AssettoCorsaEnv | None, metrics: Metrics, ctx: CommandContext) -> None:
    if env is None:
        return
    try:
        stop_and_neutralize(env, metrics, ctx)
    except Exception as exc:
        logger.warning("Nettoyage partiel: %s", exc)
    try:
        env.end()
    except Exception as exc:
        logger.warning("Fermeture env: %s", exc)


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

        logger.info("Env: track=%s car=%s (actions relatives)", DEFAULT_TRACK, DEFAULT_CAR)
        env = make_env(cfg)

        input("\nEntree pour demarrer le test accelerateur + frein...")

        env.reset()
        state = env.states[-1]
        check_preconditions(state)
        metrics.baseline_speed_kmh = speed_kmh(state)
        ctx.last_state_fp = state_fingerprint(state)
        log_sample("baseline", sample_from_state(state, env), 0.0)

        if run_shift_to_first_phase(env, metrics, ctx) and gear_is_first(env, metrics):
            run_accel_phase(env, metrics, ctx)
            if not metrics.no_movement and not metrics.no_acc_response:
                run_release_phase(env, metrics, ctx)
                run_brake_phase(env, metrics, ctx)
        elif not metrics.gear_shift_failed:
            metrics.gear_shift_failed = True
            logger.error('FAIL "passage N vers premiere impossible" (gear != 2)')

        passed = evaluate_success(metrics)
        print_summary(metrics, passed)
        exit_code = 0 if passed else 2

    except KeyboardInterrupt:
        logger.error("Ctrl+C - nettoyage securite.")
        exit_code = 130
    except RuntimeError as exc:
        logger.error("ECHEC: %s", exc)
        print_summary(metrics, False)
        exit_code = 1
    finally:
        final_cleanup(env, metrics, ctx)
        logger.info("Client libere, neutre restaure.")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
