"""Reject observations that cannot belong to a single stationary car."""
import math


STATIONARY_SPEED_MS = 0.05
STATIONARY_MOVE_M = 0.01
STATIONARY_DT_SEC = 0.2
YAW_JUMP_RAD = 1.0
TYRES_JUMP = 3


def _angle_delta(previous, current):
    delta = (float(current) - float(previous) + math.pi) % (2.0 * math.pi) - math.pi
    return abs(delta)


def is_quasi_stationary(previous, state):
    try:
        dt = float(state["timestamp_ac"]) - float(previous["timestamp_ac"])
        dx = float(state["world_position_x"]) - float(previous["world_position_x"])
        dy = float(state["world_position_y"]) - float(previous["world_position_y"])
        speed = max(abs(float(state["speed"])), abs(float(previous["speed"])))
    except (KeyError, TypeError, ValueError):
        return False
    return (
        0.0 < dt < STATIONARY_DT_SEC
        and speed < STATIONARY_SPEED_MS
        and math.hypot(dx, dy) < STATIONARY_MOVE_M
    )


def stationary_signal_inconsistent(previous, state):
    """True when a parked car reports another instance's yaw or tyres-out."""
    if previous is None or state is None or not is_quasi_stationary(previous, state):
        return False
    if "yaw" in previous and "yaw" in state:
        try:
            if _angle_delta(previous["yaw"], state["yaw"]) > YAW_JUMP_RAD:
                return True
        except (TypeError, ValueError):
            return True
    try:
        tyres = abs(float(state.get("numberOfTyresOut", 0)) - float(previous.get("numberOfTyresOut", 0)))
    except (TypeError, ValueError):
        return False
    return tyres >= TYRES_JUMP
