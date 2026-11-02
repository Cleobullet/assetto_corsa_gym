"""Single deterministic truth source for "the car is really on track" (DRIVE).

This is the ONE place that decides whether the AI policy may control the car.
Every component feeds the same physical signals into this function so there is
no cross-worker drift and no false positives from clock / resume_drive / text
log heuristics:

    - in-game plugin  -> publishes the raw physical signals (sim_live, brake)
    - collector       -> confirms vJoy readback + telemetry freshness, then asks
                         is_drive_really_active() before declaring DRIVE
    - launcher        -> the Drive click is only a TRIGGER; it never confirms

TRUTH RULE (all must hold):
    sim is LIVE
    AND brake released  (physics.brake < 0.5  -> not parked in the Drive menu)
    AND vJoy input acknowledged (feeder readback / UpdateVJD succeeded)
    AND telemetry fresh

`speed` is deliberately NOT part of the entry rule: right after a teleport the
car sits motionless at the start line, so requiring speed > 0 to start the policy
would deadlock (policy must run first to apply throttle). Instead `speed` powers a
separate MOVEMENT WATCHDOG: if the policy commands sustained throttle and the car
still does not move, the vJoy input is not reaching AC.
"""
from __future__ import annotations

# In AC's pre-drive session menu the car is parked with the brake fully held so
# it cannot roll. The instant the player takes control the brake releases. This
# pinned brake is the only signal that reliably separates "menu" from "real
# drive", even in HOTLAP where the lap clock already ticks inside the menu.
MENU_BRAKE_HOLD_THRESHOLD = 0.5
MAX_TELEMETRY_AGE_SEC = 2.0

# AC_STATUS enum (see sensors_par/sim_info.py): AC_OFF=0, AC_REPLAY=1,
# AC_LIVE=2, AC_PAUSE=3. Used only as a fallback when an older plugin build does
# not yet publish the explicit `sim_live` boolean.
AC_LIVE = 2

# Movement watchdog (speed is in m/s on the telemetry channel).
WATCHDOG_THROTTLE_MIN = 0.25       # physical throttle [0..1] considered "real gas"
WATCHDOG_SPEED_EPS_MS = 0.5        # below this (~1.8 km/h) the car is "not moving"
WATCHDOG_MAX_STALLED_STEPS = 40    # steps of gas-without-motion before flagging

# Stable reason codes (used by launcher/collector logs and tests).
REASON_OK = ""
REASON_NO_STATE = "no_driving_state"
REASON_NOT_LIVE = "ac_status_not_live"
REASON_MENU_BRAKE_HOLD = "session_menu_brake_hold"
REASON_VJOY_NOT_ACK = "vjoy_input_not_acknowledged"
REASON_TELEMETRY_STALE = "telemetry_stale"

# Watchdog failure reason (recovery trigger, not a drive-entry reason).
REASON_VJOY_NOT_REACHING_CAR = "vjoy_input_not_reaching_car"


def _sim_live(driving_state) -> bool:
    if "sim_live" in driving_state:
        return bool(driving_state.get("sim_live"))
    try:
        return int(driving_state.get("ac_status", -1)) == AC_LIVE
    except (TypeError, ValueError):
        return False


def menu_brake_hold(brake) -> bool:
    """True when the brake is pinned -> car is still parked in the Drive menu."""
    try:
        return float(brake) >= MENU_BRAKE_HOLD_THRESHOLD
    except (TypeError, ValueError):
        # Unknown brake -> assume we are still in the menu (fail safe: never
        # let the policy start on a missing signal).
        return True


def is_drive_really_active(
    driving_state,
    *,
    vjoy_readback_ok: bool,
    telemetry_age_sec: float,
) -> tuple[bool, str]:
    """THE single source of truth. Returns (active, reason_if_not_active).

    driving_state: snapshot dict from the in-game plugin (get_driving_state);
        must carry `sim_live` (bool) and `physics_brake` (float).
    vjoy_readback_ok: collector confirmed the feeder writes the vJoy axes
        (UpdateVJD / axis readback succeeded) -> the input path is alive.
    telemetry_age_sec: age of the most recent telemetry packet, in seconds.
    """
    if not driving_state:
        return False, REASON_NO_STATE
    if not _sim_live(driving_state):
        return False, REASON_NOT_LIVE
    if menu_brake_hold(driving_state.get("physics_brake")):
        return False, REASON_MENU_BRAKE_HOLD
    if not bool(vjoy_readback_ok):
        return False, REASON_VJOY_NOT_ACK
    try:
        if float(telemetry_age_sec) > MAX_TELEMETRY_AGE_SEC:
            return False, REASON_TELEMETRY_STALE
    except (TypeError, ValueError):
        return False, REASON_TELEMETRY_STALE
    return True, REASON_OK


def pedal_from_state(state) -> float:
    """AC gas pedal [0..1] from a telemetry dict (plugin uses accStatus, not gas)."""
    if not state:
        return 0.0
    for key in ("accStatus", "gas"):
        if key not in state:
            continue
        try:
            return float(state.get(key) or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def throttle_commanded_but_unacked(raw_throttle, pedal_status) -> bool:
    """True when the policy presses the pedal (raw > 0) but AC reports ~0 gas."""
    try:
        raw = float(raw_throttle)
        pedal = float(pedal_status)
    except (TypeError, ValueError):
        return False
    return raw > 0.05 and pedal < 0.05


def is_gas_without_movement(throttle_physical, speed_ms) -> bool:
    """True when commanded throttle is real but the car is not moving (m/s)."""
    try:
        throttle = float(throttle_physical)
        speed = abs(float(speed_ms))
    except (TypeError, ValueError):
        return False
    return throttle >= WATCHDOG_THROTTLE_MIN and speed <= WATCHDOG_SPEED_EPS_MS


def movement_watchdog_tripped(stalled_steps) -> bool:
    """True once gas-without-movement has persisted long enough to be a fault."""
    try:
        return int(stalled_steps) >= WATCHDOG_MAX_STALLED_STEPS
    except (TypeError, ValueError):
        return False


def stationary_cause(state, requested_throttle):
    """Classify an immobile car from actual gear/pedals, before blaming vJoy."""
    gear = int(state.get("actualGear", state.get("gear", -1)))
    if gear in (0, 1):
        return "neutral_gear"
    if float(state.get("brakeStatus", 0.0)) > 0.1:
        return "braking_at_stop"
    if float(requested_throttle) > 0.05 and pedal_from_state(state) < 0.05:
        return "throttle_unacknowledged"
    return "car_stuck"


def should_reset_for_stationary(cause, state, requested_throttle) -> bool:
    """True when a parked car will not self-recover (break neutral/no-gas deadlock).

    Training used to ignore ``neutral_gear`` and wait for AUTO_SHIFTER. That only
    works if AC actually receives throttle. After a telemetry-stale safety cut, gas
    stays at 0, the car remains in N, and the learner keeps recording junk until a
    late stuck timer. Reset as soon as we know the drivetrain cannot engage.
    """
    cause = str(cause or "")
    if cause == "throttle_unacknowledged":
        return True
    if cause != "neutral_gear":
        return False
    try:
        throttle = float(requested_throttle)
    except (TypeError, ValueError):
        return False
    # Policy is not asking to move — leave it alone.
    if throttle <= 0.05:
        return False
    # Throttle intent while stuck in N: either gas never reaches AC, or AUTO_SHIFTER
    # failed to leave neutral despite pedal. Both need a teleport/reset.
    return True

