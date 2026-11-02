"""Safe limits and gear-shift helpers for SAC training sessions."""
import logging
import time

import numpy as np

from AssettoCorsaEnv.gear_shift import (
    GEAR_FIRST,
    GEAR_NEUTRAL,
    auto_shift_neutral_to_first as _auto_shift_neutral_to_first,
    gear_raw as _gear_raw,
    is_first,
    is_neutral,
)

logger = logging.getLogger(__name__)

SHIFT_HOLD_SEC = 0.15
GEAR_SHIFT_WAIT_SEC = 2.0
STEER_SAFE_DEG_DEFAULT = 90.0
STEER_HARD_FAIL_DEG = 100.0
SAFE_ACC_PEDAL_MAX_DEFAULT = 0.20
SAFE_BRAKE_PEDAL_MAX_DEFAULT = 0.50
SAFE_EXPLORATION_STEER_DEG_DEFAULT = 20.0
PEDAL_ACTIVE_THRESHOLD = 0.01
PEDAL_MUTEX_CLOSE_PCT = 0.05
ACC_INTERNAL_OFF = -1.0
BRAKE_INTERNAL_OFF = -1.0

EXPLORATION_ACC_FRACTION = 0.70
EXPLORATION_COAST_FRACTION = 0.20
EXPLORATION_BRAKE_FRACTION = 0.10
EXPLORATION_ACC_PEDAL_MIN = 0.05
EXPLORATION_ACC_PEDAL_MAX = 0.20
EXPLORATION_BRAKE_PEDAL_MIN = 0.05
EXPLORATION_BRAKE_PEDAL_MAX = 0.15
EXPLORATION_V3_CENTER_STEER_FRACTION = 0.70
EXPLORATION_V3_CORRECTION_DEG = 8.0


def internal_to_degrees(internal: float, norm_steer_at_max: float, max_steer_deg: float) -> float:
    """Map simulator internal steer to wheel degrees (linear steer_map)."""
    return internal * max_steer_deg / norm_steer_at_max


def degrees_to_internal(deg: float, norm_steer_at_max: float, max_steer_deg: float) -> float:
    """Map wheel degrees to simulator internal steer (linear steer_map)."""
    return deg * norm_steer_at_max / max_steer_deg


def policy_steer_to_internal(
    raw_steer: float,
    norm_steer_at_max: float,
    max_steer_deg: float,
    steer_limit_deg: float,
) -> float:
    """Map policy steer in [-1, 1] onto the usable wheel range (±steer_limit_deg).

    v3 limits treat the value as AC internal units (Lotus: ±0.4 = ±180°). If the
    SAC output ±1 is passed through unchanged, any |raw| > 0.078 already asks
    for the 35° safety stop — the policy cannot explore a gentle right turn.
    """
    raw = float(np.clip(raw_steer, -1.0, 1.0))
    return raw * degrees_to_internal(steer_limit_deg, norm_steer_at_max, max_steer_deg)


# Backward-compatible aliases
internal_to_steer_deg = internal_to_degrees
steer_deg_to_internal = degrees_to_internal


def pedal_to_internal(pedal: float) -> float:
    return 2.0 * pedal - 1.0


def internal_to_pedal(internal: float) -> float:
    return (internal + 1.0) / 2.0


def steer_internal_max_for_deg(norm_steer_at_max: float, max_steer_deg: float, deg: float) -> float:
    return norm_steer_at_max * (deg / max_steer_deg)


def resolve_pedal_mutex(acc_internal: float, brake_internal: float) -> tuple[float, float]:
    """Resolve simultaneous pedals using physical % (OFF=-1 internal, not 0.0)."""
    acc_pedal = internal_to_pedal(acc_internal)
    brake_pedal = internal_to_pedal(brake_internal)
    if acc_pedal <= PEDAL_ACTIVE_THRESHOLD or brake_pedal <= PEDAL_ACTIVE_THRESHOLD:
        return acc_internal, brake_internal
    diff = abs(acc_pedal - brake_pedal)
    if diff < PEDAL_MUTEX_CLOSE_PCT:
        return acc_internal, BRAKE_INTERNAL_OFF
    if acc_pedal > brake_pedal:
        return acc_internal, BRAKE_INTERNAL_OFF
    return ACC_INTERNAL_OFF, brake_internal


def apply_safe_control_limits(
    actions: np.ndarray,
    norm_steer_at_max: float,
    max_steer_deg: float,
    steer_safe_deg: float = STEER_SAFE_DEG_DEFAULT,
    acc_pedal_max: float = SAFE_ACC_PEDAL_MAX_DEFAULT,
    brake_pedal_max: float = SAFE_BRAKE_PEDAL_MAX_DEFAULT,
) -> np.ndarray:
    out = actions.copy()
    steer_max = steer_internal_max_for_deg(norm_steer_at_max, max_steer_deg, steer_safe_deg)
    out[0] = np.clip(out[0], -steer_max, steer_max)

    acc_max = pedal_to_internal(acc_pedal_max)
    brake_max = pedal_to_internal(brake_pedal_max)
    out[1] = np.clip(out[1], ACC_INTERNAL_OFF, acc_max)
    out[2] = np.clip(out[2], BRAKE_INTERNAL_OFF, brake_max)

    out[1], out[2] = resolve_pedal_mutex(out[1], out[2])
    return out


def clip_safe_action_delta(
    action: np.ndarray,
    current_actions: np.ndarray,
    adjusted_rate_limit: np.ndarray,
    norm_steer_at_max: float,
    max_steer_deg: float,
    steer_safe_deg: float = STEER_SAFE_DEG_DEFAULT,
    acc_pedal_max: float = SAFE_ACC_PEDAL_MAX_DEFAULT,
    brake_pedal_max: float = SAFE_BRAKE_PEDAL_MAX_DEFAULT,
) -> np.ndarray:
    new_actions = current_actions + action * adjusted_rate_limit[:, 1]
    safe_actions = apply_safe_control_limits(
        new_actions,
        norm_steer_at_max,
        max_steer_deg,
        steer_safe_deg=steer_safe_deg,
        acc_pedal_max=acc_pedal_max,
        brake_pedal_max=brake_pedal_max,
    )
    max_delta = adjusted_rate_limit[:, 1]
    clipped = (safe_actions - current_actions) / max_delta
    return np.clip(clipped, -1.0, 1.0)


def sample_safe_exploration_action(
    rng: np.random.Generator,
    norm_steer_at_max: float,
    max_steer_deg: float,
    exploration_steer_deg: float = SAFE_EXPLORATION_STEER_DEG_DEFAULT,
) -> np.ndarray:
    """Absolute controls: 70% acc, 20% coast, 10% brake; steer +/-exploration_steer_deg."""
    acc_internal, brake_internal = sample_pedals_exploration(rng)
    steer_max = steer_internal_max_for_deg(norm_steer_at_max, max_steer_deg, exploration_steer_deg)
    steer = rng.uniform(-steer_max, steer_max)
    return np.array([steer, acc_internal, brake_internal], dtype=np.float64)


def sample_pedals_exploration(rng: np.random.Generator) -> tuple[float, float]:
    roll = rng.random()
    acc_internal = ACC_INTERNAL_OFF
    brake_internal = BRAKE_INTERNAL_OFF
    if roll < EXPLORATION_ACC_FRACTION:
        acc_internal = pedal_to_internal(rng.uniform(EXPLORATION_ACC_PEDAL_MIN, EXPLORATION_ACC_PEDAL_MAX))
    elif roll >= EXPLORATION_ACC_FRACTION + EXPLORATION_COAST_FRACTION:
        brake_internal = pedal_to_internal(rng.uniform(EXPLORATION_BRAKE_PEDAL_MIN, EXPLORATION_BRAKE_PEDAL_MAX))
    return acc_internal, brake_internal


def sample_safe_exploration_action_v3(
    rng: np.random.Generator,
    norm_steer_at_max: float,
    max_steer_deg: float,
) -> np.ndarray:
    """Absolute controls: 70/20/10 pedals; 70% steer center, 30% correction +/-8 deg."""
    acc_internal, brake_internal = sample_pedals_exploration(rng)
    if rng.random() < EXPLORATION_V3_CENTER_STEER_FRACTION:
        steer_deg = 0.0
    else:
        steer_deg = rng.uniform(-EXPLORATION_V3_CORRECTION_DEG, EXPLORATION_V3_CORRECTION_DEG)
    steer = degrees_to_internal(steer_deg, norm_steer_at_max, max_steer_deg)
    return np.array([steer, acc_internal, brake_internal], dtype=np.float64)


def apply_steer_v3_control(
    target_internal: float,
    current_internal: float,
    norm_steer_at_max: float,
    max_steer_deg: float,
    steer_limit_deg: float,
    max_delta_deg: float,
    soft_zone_deg: float,
    center_deadband_deg: float,
    center_decay: float,
) -> float:
    cur_deg = internal_to_degrees(current_internal, norm_steer_at_max, max_steer_deg)
    tgt_deg = internal_to_degrees(target_internal, norm_steer_at_max, max_steer_deg)
    tgt_deg = float(np.clip(tgt_deg, -steer_limit_deg, steer_limit_deg))

    if abs(tgt_deg) < center_deadband_deg:
        tgt_deg = cur_deg * center_decay

    new_deg = cur_deg + float(np.clip(tgt_deg - cur_deg, -max_delta_deg, max_delta_deg))

    if soft_zone_deg > 0 and abs(new_deg) < soft_zone_deg:
        softened = float(np.sign(new_deg) * (abs(new_deg) ** 2 / soft_zone_deg))
        new_deg = cur_deg + float(np.clip(softened - cur_deg, -max_delta_deg, max_delta_deg))

    new_deg = float(np.clip(new_deg, -steer_limit_deg, steer_limit_deg))
    return degrees_to_internal(new_deg, norm_steer_at_max, max_steer_deg)


def apply_v3_control_limits(
    target_actions: np.ndarray,
    current_actions: np.ndarray,
    norm_steer_at_max: float,
    max_steer_deg: float,
    steer_limit_deg: float,
    max_delta_deg: float,
    soft_zone_deg: float,
    center_deadband_deg: float,
    center_decay: float,
    acc_pedal_max: float,
    brake_pedal_max: float,
    acc_max_delta_pedal: float = 0.0,
    brake_max_delta_pedal: float = 0.0,
) -> np.ndarray:
    out = target_actions.copy()
    out[0] = apply_steer_v3_control(
        out[0],
        current_actions[0],
        norm_steer_at_max,
        max_steer_deg,
        steer_limit_deg,
        max_delta_deg,
        soft_zone_deg,
        center_deadband_deg,
        center_decay,
    )
    acc_max = pedal_to_internal(acc_pedal_max)
    brake_max = pedal_to_internal(brake_pedal_max)
    if acc_max_delta_pedal > 0.0:
        cur_acc = internal_to_pedal(current_actions[1])
        tgt_acc = internal_to_pedal(out[1])
        tgt_acc = cur_acc + float(np.clip(tgt_acc - cur_acc, -acc_max_delta_pedal, acc_max_delta_pedal))
        out[1] = pedal_to_internal(tgt_acc)
    if brake_max_delta_pedal > 0.0:
        cur_brk = internal_to_pedal(current_actions[2])
        tgt_brk = internal_to_pedal(out[2])
        tgt_brk = cur_brk + float(np.clip(tgt_brk - cur_brk, -brake_max_delta_pedal, brake_max_delta_pedal))
        out[2] = pedal_to_internal(tgt_brk)
    out[1] = np.clip(out[1], ACC_INTERNAL_OFF, acc_max)
    out[2] = np.clip(out[2], BRAKE_INTERNAL_OFF, brake_max)
    out[1], out[2] = resolve_pedal_mutex(out[1], out[2])
    return out


def executed_action_for_buffer(internal_controls: np.ndarray) -> np.ndarray:
    """Replay-buffer action: exact executed internal [steer, acc, brake]."""
    return np.array(internal_controls, dtype=np.float64).copy()


def policy_action_from_internal(
    internal_controls: np.ndarray,
    norm_steer_at_max: float,
) -> np.ndarray:
    out = np.array([
        internal_controls[0] / norm_steer_at_max,
        internal_controls[1],
        internal_controls[2],
    ], dtype=np.float64)
    return np.clip(out, -1.0, 1.0)


def classify_exploration_action(acc_internal: float, brake_internal: float) -> str:
    acc_pedal = internal_to_pedal(acc_internal)
    brake_pedal = internal_to_pedal(brake_internal)
    acc_on = acc_pedal > PEDAL_ACTIVE_THRESHOLD
    brake_on = brake_pedal > PEDAL_ACTIVE_THRESHOLD
    if acc_on and not brake_on:
        return "acc"
    if brake_on and not acc_on:
        return "brake"
    if not acc_on and not brake_on:
        return "coast"
    return "both"


def gear_value(state) -> int:
    return _gear_raw(state)


def auto_shift_neutral_to_first(env) -> bool:
    ok, report = _auto_shift_neutral_to_first(env)
    env.last_gear_shift_report = report
    return ok
