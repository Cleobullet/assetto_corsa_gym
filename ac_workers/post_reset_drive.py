"""Wait for AC driving state after env.reset() via management plugin.

Driving confirmation delegates to the single truth source
(ac_workers/drive_truth.is_drive_really_active): sim live + brake released
(out of the menu) + vJoy readback + fresh telemetry. The old heuristics
(session_clock_advancing, controls_live probing, resume_drive trust) are gone;
they produced the cross-worker false positives.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

from ac_workers.drive_truth import is_drive_really_active

logger = logging.getLogger(__name__)

MAX_WAIT_SEC = 15.0
POLL_SEC = 0.25
RESUME_INTERVAL_SEC = 0.5
CONTROLS_NOT_LIVE_ERROR = "session control menu still open or controls are not live"
GEAR_NOT_ENGAGED_ERROR = "initial gear not engaged"
DRIVE_INPUT_NOT_DELIVERED_ERROR = "drive input not delivered by launcher"
# Legacy alias kept for callers that still import the old name.
DRIVE_MENU_STILL_OPEN_ERROR = GEAR_NOT_ENGAGED_ERROR


def driving_confirmed(
    state: dict[str, Any],
    *,
    gear_raw: int | None = None,
    require_non_neutral_gear: bool = True,
    require_controls_live: bool = True,  # deprecated: superseded by drive_truth
    vjoy_readback_ok: bool = True,
    telemetry_age_sec: float = 0.0,
) -> bool:
    """True only when the single truth source confirms the car is really driving.

    `require_controls_live` is accepted for backward compatibility but ignored:
    the plugin-side controls_live probe raced the collector's vJoy feeder and is
    no longer a truth signal. By default vjoy_readback_ok/telemetry_age default to
    "ok" so the live-attach path (which enforces the vJoy gate separately via
    run_vjoy_startup_validation) still gets the physical menu/live gate here.
    """
    active, _reason = is_drive_really_active(
        state,
        vjoy_readback_ok=vjoy_readback_ok,
        telemetry_age_sec=telemetry_age_sec,
    )
    if not active:
        return False
    if require_non_neutral_gear and gear_raw is not None:
        from AssettoCorsaEnv.gear_shift import is_neutral

        if is_neutral(int(gear_raw)):
            return False
    return True


def _failure_reason(
    last_state: dict[str, Any],
    *,
    gear: int | None,
    require_non_neutral_gear: bool,
    require_controls_live: bool,
    vjoy_readback_ok: bool = True,
    telemetry_age_sec: float = 0.0,
) -> str:
    active, reason = is_drive_really_active(
        last_state,
        vjoy_readback_ok=vjoy_readback_ok,
        telemetry_age_sec=telemetry_age_sec,
    )
    if not active:
        return reason or "driving not confirmed before timeout"
    if require_non_neutral_gear and gear is not None:
        from AssettoCorsaEnv.gear_shift import is_neutral

        if is_neutral(int(gear)):
            return GEAR_NOT_ENGAGED_ERROR
    return last_state.get("reason") or "driving not confirmed before timeout"


def wait_for_driving_after_reset(
    simulation_management: Any,
    *,
    sleep_fn: Callable[[float], None] | None = None,
    get_gear_fn: Callable[[], int] | None = None,
    max_wait_sec: float = MAX_WAIT_SEC,
    require_non_neutral_gear: bool = True,
    require_controls_live: bool = True,
    vjoy_readback_fn: Callable[[], bool] | None = None,
    telemetry_age_fn: Callable[[], float] | None = None,
    on_poll: Callable[[dict], None] | None = None,
) -> tuple[bool, dict]:
    """Poll management plugin until the single truth source confirms driving.

    `vjoy_readback_fn`/`telemetry_age_fn` are optional probes feeding the truth
    source; when omitted this performs the physical menu/live gate only (the vJoy
    gate is then enforced by run_vjoy_startup_validation right after).

    `on_poll(state)` is an optional observer callback invoked with each raw plugin
    state read; it is for diagnostics only and must never raise (its exceptions
    are swallowed). It does not influence the wait logic in any way.
    """

    def _observe(state: dict) -> None:
        if on_poll is None:
            return
        try:
            on_poll(state)
        except Exception:
            pass

    sleep_fn = sleep_fn or time.sleep
    logger.info("post-reset: waiting for driving state")
    deadline = time.monotonic() + max_wait_sec
    last_resume = 0.0
    last_state: dict = {}

    def _vjoy_ok() -> bool:
        return bool(vjoy_readback_fn()) if vjoy_readback_fn is not None else True

    def _telem_age() -> float:
        return float(telemetry_age_fn()) if telemetry_age_fn is not None else 0.0

    while time.monotonic() < deadline:
        last_state = simulation_management.get_driving_state()
        _observe(last_state)
        gear = int(get_gear_fn()) if get_gear_fn is not None else None
        if driving_confirmed(
            last_state,
            gear_raw=gear,
            require_non_neutral_gear=require_non_neutral_gear,
            require_controls_live=require_controls_live,
            vjoy_readback_ok=_vjoy_ok(),
            telemetry_age_sec=_telem_age(),
        ):
            logger.info("post-reset: driving state confirmed")
            if gear is not None:
                last_state = dict(last_state)
                last_state["gear_raw"] = gear
            return True, last_state
        now = time.monotonic()
        if now - last_resume >= RESUME_INTERVAL_SEC:
            simulation_management.send_resume_drive()
            last_resume = now
        sleep_fn(POLL_SEC)

    last_state = simulation_management.get_driving_state()
    _observe(last_state)
    gear = int(get_gear_fn()) if get_gear_fn is not None else None
    if gear is not None:
        last_state = dict(last_state)
        last_state["gear_raw"] = gear
    if driving_confirmed(
        last_state,
        gear_raw=gear,
        require_non_neutral_gear=require_non_neutral_gear,
        require_controls_live=require_controls_live,
        vjoy_readback_ok=_vjoy_ok(),
        telemetry_age_sec=_telem_age(),
    ):
        logger.info("post-reset: driving state confirmed on final poll")
        return True, last_state
    reason = _failure_reason(
        last_state,
        gear=gear,
        require_non_neutral_gear=require_non_neutral_gear,
        require_controls_live=require_controls_live,
        vjoy_readback_ok=_vjoy_ok(),
        telemetry_age_sec=_telem_age(),
    )
    last_state = dict(last_state)
    last_state["reason"] = reason
    logger.error("post-reset: waiting for driving state failed: %s", reason)
    return False, last_state
